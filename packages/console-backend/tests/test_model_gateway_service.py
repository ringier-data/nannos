"""Tests for ModelGatewayService: request logging, and the model catalog's source fallback.

Two behaviours worth pinning:
- the catalog probes proxy endpoints that a given LiteLLM version may not expose, so that expected
  404 must not be logged as an error (`optional=True` downgrades it to debug);
- the catalog's primary source is an upstream JSON file nobody here controls (LITELLM_COSTMAP_REF,
  default `main`). It can be unreachable, undecodable, or simply reshaped — and none of those may end
  as "no catalog", because an empty catalog reads as "unreadable" downstream and blocks registrations.
  Each source is tried end to end, parsing included, and a bad payload falls through to the proxy's
  own bundled map.
"""

import logging

import httpx
import pytest
from console_backend.services.model_gateway_service import ModelGatewayError, ModelGatewayService

_LOGGER = "console_backend.services.model_gateway_service"


class _FakeClient:
    """Async-context httpx client stand-in that always returns a 404."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        return httpx.Response(404, text="Not Found", request=httpx.Request(method, url))


@pytest.fixture
def svc():
    return ModelGatewayService(base_url="http://gateway.test", master_key="k")


@pytest.mark.asyncio
async def test_optional_request_404_is_debug_not_error(svc, caplog, monkeypatch):
    monkeypatch.setattr("console_backend.services.model_gateway_service.httpx.AsyncClient", _FakeClient)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        with pytest.raises(ModelGatewayError):
            await svc._request("GET", "/get/litellm_model_cost_map", optional=True)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # no error noise
    assert any(r.levelno == logging.DEBUG and "404" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_non_optional_request_404_is_error(svc, caplog, monkeypatch):
    """A 404 on a required endpoint is still surfaced as an error."""
    monkeypatch.setattr("console_backend.services.model_gateway_service.httpx.AsyncClient", _FakeClient)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        with pytest.raises(ModelGatewayError):
            await svc._request("GET", "/model/info")

    assert any(r.levelno == logging.ERROR and "404" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_request_reuses_one_pooled_client(svc, monkeypatch):
    """Repeated calls share a single AsyncClient instead of opening one per request."""
    created = []

    class _Counting(_FakeClient):
        def __init__(self, *args, **kwargs):
            created.append(self)

        async def request(self, method, url, **kwargs):
            return httpx.Response(200, json={"data": []}, request=httpx.Request(method, url))

    monkeypatch.setattr("console_backend.services.model_gateway_service.httpx.AsyncClient", _Counting)
    await svc._request("GET", "/model/info")
    await svc._request("GET", "/model/info")
    assert len(created) == 1  # client created once, reused


@pytest.mark.asyncio
async def test_list_models_cached_and_invalidated_on_write(svc, monkeypatch):
    """list_models caches within _LIST_TTL; a write drops the cache so the next read re-fetches."""
    calls = {"n": 0}

    async def _fake_request(method, path, **kwargs):
        if path == "/model/info":
            calls["n"] += 1
            return {"data": [{"model_name": f"m{calls['n']}"}]}
        return {}

    monkeypatch.setattr(svc, "_request", _fake_request)

    first = await svc.list_models()
    second = await svc.list_models()
    assert calls["n"] == 1  # second read served from cache
    assert first == second

    await svc.delete_model("some-id")  # write invalidates the cache
    await svc.list_models()
    assert calls["n"] == 2  # re-fetched after invalidation


@pytest.mark.asyncio
async def test_update_model_recreates_deployment_to_persist_model_info(svc, monkeypatch):
    """update_model re-registers (so custom model_info like input_modes actually persists),
    then deletes the old deployment — it must NOT call LiteLLM's /model/update, which drops
    custom model_info keys. Register happens before delete so the alias is never without a
    live deployment."""
    calls: list[tuple[str, dict]] = []

    async def _fake_request(method, path, **kwargs):
        calls.append((path, kwargs.get("json") or {}))
        if path == "/model/new":
            return {"model_info": {"id": "new-id"}}
        return {}

    monkeypatch.setattr(svc, "_request", _fake_request)

    result = await svc.update_model(
        "old-id",
        "claude-sonnet-4-6",
        {"model": "eu.anthropic.claude-sonnet-4-6"},
        {"input_modes": ["text", "image", "file"], "mode": "chat"},
    )

    paths = [p for p, _ in calls]
    assert "/model/update" not in paths  # the whole point: /model/update can't persist model_info
    # The old deployment is read first (its probe record is carried over, nannos#318), then
    # register before delete so the alias is never without a live deployment.
    assert paths == ["/model/info", "/model/new", "/model/delete"]

    _, new_body = calls[1]
    assert new_body["model_name"] == "claude-sonnet-4-6"
    assert new_body["model_info"]["input_modes"] == ["text", "image", "file"]
    assert calls[2][1] == {"id": "old-id"}  # old deployment deleted by id
    assert result["model_info"]["id"] == "new-id"


@pytest.mark.asyncio
async def test_update_model_survives_failed_old_delete(svc, monkeypatch):
    """If deleting the old deployment fails, the re-registration still stands (it was created
    first) — update_model logs and returns rather than raising, so the edit isn't lost. It also
    signals the lingering old deployment via _stale_duplicate_deployment_id so the endpoint can
    report a partial success instead of a clean 'updated'."""

    async def _fake_request(method, path, **kwargs):
        if path == "/model/new":
            return {"model_info": {"id": "new-id"}}
        if path == "/model/delete":
            raise ModelGatewayError("gateway unreachable")
        return {}

    monkeypatch.setattr(svc, "_request", _fake_request)

    result = await svc.update_model("old-id", "m", {"model": "x"}, {"input_modes": ["file"]})
    assert result["model_info"]["id"] == "new-id"
    assert result["_stale_duplicate_deployment_id"] == "old-id"


@pytest.mark.asyncio
async def test_update_model_no_stale_signal_on_clean_delete(svc, monkeypatch):
    """On the happy path (old delete succeeds) no stale-duplicate marker is attached, so the
    endpoint reports a clean 'updated'."""

    async def _fake_request(method, path, **kwargs):
        if path == "/model/new":
            return {"model_info": {"id": "new-id"}}
        return {}

    monkeypatch.setattr(svc, "_request", _fake_request)

    result = await svc.update_model("old-id", "m", {"model": "x"}, {"input_modes": ["file"]})
    assert "_stale_duplicate_deployment_id" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "litellm_model,provider,expect_dimensions",
    [
        # Gemini / generic (Titan) accept the Matryoshka param → the ping carries it, so a model
        # that rejects it fails registration instead of mid-sync (the gap this closes).
        ("vertex_ai/gemini-embedding-2", "vertex_ai", True),
        ("bedrock/amazon.titan-embed-text-v2:0", "bedrock", True),
        # Cohere v3 rejects `dimensions`; the ping must match the runtime (no dimensions).
        ("bedrock/cohere.embed-english-v3", "bedrock", False),
    ],
)
async def test_embedding_test_ping_matches_runtime_dimensions_shape(
    svc, monkeypatch, litellm_model, provider, expect_dimensions
):
    captured: dict = {}

    async def _fake_request(method, path, **kwargs):
        if path == "/model/info":
            return {
                "data": [
                    {
                        "model_name": "emb",
                        "litellm_params": {"model": litellm_model},
                        "model_info": {"mode": "embedding", "litellm_provider": provider},
                    }
                ]
            }
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {"data": [{"embedding": [0.0]}]}

    monkeypatch.setattr(svc, "_request", _fake_request)

    await svc.test_model("emb")

    assert captured["path"] == "/v1/embeddings"
    assert ("dimensions" in captured["json"]) is expect_dimensions


# --- catalog sources: freshest first, proxy as the safety net, per-entry tolerance ---


def _cost_map(count: int = 60, **extra) -> dict:
    """A payload shaped like LiteLLM's cost map, big enough to pass the plausibility floor."""
    raw = {
        f"vendor.model-{i}": {"litellm_provider": "bedrock", "mode": "chat", "input_cost_per_token": 1e-6}
        for i in range(count)
    }
    raw.update(extra)
    return raw


def _catalog_svc(monkeypatch, *, public, proxy):
    """A service whose two catalog sources are stubbed. Callables raise; values are returned."""
    svc = ModelGatewayService(base_url="http://gateway.test", master_key="k")

    async def _public(self=None):
        if callable(public):
            return public()
        return public

    async def _proxy(self=None):
        if callable(proxy):
            return proxy()
        return proxy

    monkeypatch.setattr(svc, "_fetch_public_cost_map", _public)
    monkeypatch.setattr(svc, "_fetch_proxy_cost_map", _proxy)
    return svc


@pytest.mark.asyncio
async def test_catalog_prefers_the_upstream_map(monkeypatch):
    """Upstream is the fresher view (a model released today is there before the proxy image has it)."""
    svc = _catalog_svc(
        monkeypatch,
        public=_cost_map(60, **{"brand.new-model": {"litellm_provider": "bedrock", "mode": "chat"}}),
        proxy=_cost_map(60),
    )

    ids = {c["model_id"] for c in await svc.get_catalog()}
    assert "brand.new-model" in ids


@pytest.mark.asyncio
async def test_unreachable_upstream_falls_back_to_the_proxy(monkeypatch, caplog):
    def _boom():
        raise httpx.ConnectError("no egress")

    svc = _catalog_svc(monkeypatch, public=_boom, proxy=_cost_map(60, **{"proxy.only": {"litellm_provider": "bedrock", "mode": "chat"}}))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        ids = {c["model_id"] for c in await svc.get_catalog()}

    assert "proxy.only" in ids  # the picker still works offline
    assert any("trying the next source" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_undecodable_upstream_falls_back_to_the_proxy(monkeypatch):
    """A JSON decode error is not an httpx error — catching only HTTP failures let this through."""

    def _bad_json():
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    svc = _catalog_svc(monkeypatch, public=_bad_json, proxy=_cost_map(60))
    assert len(await svc.get_catalog()) == 60


@pytest.mark.asyncio
async def test_reshaped_upstream_falls_back_instead_of_reporting_a_tiny_catalog(monkeypatch):
    """The subtle one: valid JSON we simply don't understand (a wrapper object, renamed keys). Parsing
    it yields a near-empty catalog, which downstream reads as "catalog unreadable" and turns into 502s
    on registration — so an implausible payload must be rejected as a SOURCE, not published as fact."""
    svc = _catalog_svc(
        monkeypatch,
        public={"data": _cost_map(60)},  # same models, one level deeper
        proxy=_cost_map(60),
    )

    catalog = await svc.get_catalog()
    assert len(catalog) == 60  # served by the proxy, not parsed as "1 model" or "0 models"


@pytest.mark.asyncio
async def test_one_unparseable_entry_does_not_cost_the_whole_catalog(monkeypatch):
    """Upstream changes a field's type on a single model now and then; that must not blank the picker."""

    class _Hostile(dict):
        """An entry that explodes when read, mimicking an unexpected value type."""

        def get(self, *args, **kwargs):
            raise TypeError("unexpected value type")

    svc = _catalog_svc(monkeypatch, public=_cost_map(60, **{"evil.model": _Hostile(litellm_provider="bedrock")}), proxy=None)

    assert len(await svc.get_catalog()) == 60  # the 60 good ones survive, the odd one is skipped


@pytest.mark.asyncio
async def test_no_usable_source_returns_empty_not_a_partial_catalog(monkeypatch, caplog):
    """[] is the honest answer AND the signal registration uses for "catalog unreadable" (502)."""

    def _boom():
        raise httpx.ConnectError("no egress")

    svc = _catalog_svc(monkeypatch, public=_boom, proxy=None)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert await svc.get_catalog() == []

    assert any("any source" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_catalog_is_cached_across_calls(monkeypatch):
    calls = []

    def _count():
        calls.append(1)
        return _cost_map(60)

    svc = _catalog_svc(monkeypatch, public=_count, proxy=None)
    await svc.get_catalog()
    await svc.get_catalog()
    assert len(calls) == 1


# --- registration probe (nannos#318) ---------------------------------------------------------
# The chat test is no longer a ping: it replays the harness's request shapes (defined once in
# ringier_a2a_sdk.model_capabilities) and records what the model accepts on its deployment.
# Pinned here: the refuse/record split, where the record goes, and what the admin gets back.


def _chat_deployment(model_id="dep-1", db_model=True, **info):
    return {
        "data": [
            {
                "model_name": "m",
                "litellm_params": {"model": "bedrock/eu.anthropic.claude-sonnet-5-5"},
                "model_info": {"mode": "chat", "id": model_id, "db_model": db_model, **info},
            }
        ]
    }


def _probe_gateway(svc, monkeypatch, *, reject=None, deployment=None):
    """Fake the management API (list + patch) and the inference API the probe hits."""
    from ringier_a2a_sdk.model_capabilities import ProbeCallError

    calls: dict = {"patched": [], "probe_bodies": [], "deployment": deployment}

    async def _fake_request(method, path, **kwargs):
        if path == "/model/info":
            return calls["deployment"] or _chat_deployment()
        if method == "PATCH":
            calls["patched"].append((path, kwargs.get("json")))
            if calls.get("patch_fails"):
                raise ModelGatewayError("Gateway returned 500", status_code=500)
            return {}
        raise AssertionError(f"unexpected management call {method} {path}")

    async def _fake_probe_call(body):
        calls["probe_bodies"].append(body)
        reason = reject(body) if reject else None
        if reason:
            raise ProbeCallError(reason, status=calls.get("reject_status", 400))
        if body.get("stream"):
            return 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\ndata: [DONE]\n'
        # A model that honours what it accepts: the probe grades the reply, not just the 200.
        message: dict = {"role": "assistant", "content": "ok"}
        tc = body.get("tool_choice")
        if tc == "required" or isinstance(tc, dict):
            call = {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
        elif body.get("response_format"):
            message["content"] = '{"answer": "yes", "confidence": 0.9}'
        return {"choices": [{"message": message}], "usage": {}}

    monkeypatch.setattr(svc, "_request", _fake_request)
    monkeypatch.setattr(svc, "_probe_call", _fake_probe_call)
    return calls


@pytest.mark.asyncio
async def test_chat_test_records_the_probe_on_the_deployment_and_returns_the_report(svc, monkeypatch):
    calls = _probe_gateway(svc, monkeypatch)

    result = await svc.test_model("m")

    assert result["probe"]["rejected"] == []
    assert result["recorded"] is True
    caps = result["probe"]["capabilities"]
    assert caps["forced_tool_choice"] is True and caps["response_format"] is True
    # Stored via the merging PATCH, keyed on the deployment id, with a probe timestamp.
    (path, body), = calls["patched"]
    assert path == "/model/dep-1/update"
    assert body["model_info"]["id"] == "dep-1"
    assert body["model_info"]["nannos_capabilities"]["forced_tool_choice"] is True
    assert body["model_info"]["nannos_capabilities"]["probed_at"]
    # Every probe request names the alias under test and carries no credentials.
    assert {b["model"] for b in calls["probe_bodies"]} == {"m"}
    assert not any("api_key" in b for b in calls["probe_bodies"])


@pytest.mark.asyncio
async def test_all_chains_are_declared_in_one_config_write(svc, monkeypatch):
    """Not LiteLLM's per-entry /fallback endpoints: they read-modify-write the row holding
    every chain through a 60 s cache they never invalidate, so a quick second edit worked on
    a stale copy. /config/update replaces the whole `fallbacks` key from our table."""
    calls: list = []

    async def _fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return {}

    monkeypatch.setattr(svc, "_request", _fake_request)
    await svc.set_all_fallbacks({"claude": ["gpt", "vertex"], "flash": []})
    assert calls == [("POST", "/config/update", {"router_settings": {"fallbacks": [{"claude": ["gpt", "vertex"]}]}})]


@pytest.mark.asyncio
async def test_progress_starts_with_the_plan_and_reports_each_shape(svc, monkeypatch):
    _probe_gateway(svc, monkeypatch, deployment=_chat_deployment(supports_reasoning=False))
    events: list[dict] = []

    result = await svc.test_model("m", on_progress=events.append)

    assert events[0]["type"] == "plan"
    planned = [s["shape"] for s in events[0]["shapes"]]
    assert "thinking_replay" not in planned  # not declared to think
    assert [e["shape"] for e in events if e["type"] == "result"] == planned
    assert [r["shape"] for r in result["probe"]["results"]] == planned
    assert all(e["label"] for e in events if e["type"] == "step")


@pytest.mark.asyncio
async def test_always_thinking_aliases_come_from_the_record_with_the_lowest_level(svc, monkeypatch):
    listing = {
        "data": [
            {"model_name": "gem", "model_info": {"supports_reasoning": True, "nannos_capabilities": {"thinking_off": "always_on"}}},
            {"model_name": "cla", "model_info": {"supports_reasoning": True, "nannos_capabilities": {"thinking_off": "between_tools"}}},
            {"model_name": "unprobed", "model_info": {"supports_reasoning": True}},
        ]
    }

    async def _fake_request(method, path, **kwargs):
        return listing

    monkeypatch.setattr(svc, "_request", _fake_request)
    assert await svc.always_thinking_aliases() == {"gem": "low"}


@pytest.mark.asyncio
async def test_a_routable_limitation_registers_with_the_limitation_recorded(svc, monkeypatch):
    """Sonnet 5.5's case: forced tool_choice and response_format 400. The model must still
    register — the record is what lets the hook and the app route around it."""

    def _reject(body):
        if body.get("tool_choice") not in (None, "auto") or body.get("response_format"):
            return 'tool_choice: type "tool" and "any" are not supported for this model'
        return None

    calls = _probe_gateway(svc, monkeypatch, reject=_reject)
    result = await svc.test_model("m")
    caps = calls["patched"][0][1]["model_info"]["nannos_capabilities"]
    assert caps["forced_tool_choice"] is False and caps["response_format"] is False
    assert {r["shape"] for r in result["probe"]["results"] if not r["ok"]} == {
        "forced_tool_choice",
        "named_tool_choice",
        "response_format",
    }


@pytest.mark.asyncio
async def test_an_unavoidable_shape_failing_refuses_registration_with_the_providers_reason(svc, monkeypatch):
    calls = _probe_gateway(svc, monkeypatch, reject=lambda b: "streaming is not supported" if b.get("stream") else None)
    with pytest.raises(ModelGatewayError, match="streaming_tools: streaming is not supported"):
        await svc.test_model("m")
    assert calls["patched"] == []  # nothing recorded on a refused model


@pytest.mark.asyncio
async def test_a_config_defined_deployment_is_probed_but_not_written(svc, monkeypatch):
    """LiteLLM rejects /model/update on config-defined deployments; the report still
    reaches the admin, and the absence of a record keeps the hook on its heuristics."""
    calls = _probe_gateway(svc, monkeypatch, deployment=_chat_deployment(db_model=False))
    result = await svc.test_model("m")
    assert "probe" in result and result["recorded"] is None
    assert calls["patched"] == []


@pytest.mark.asyncio
async def test_thinking_shapes_follow_the_admins_reasoning_declaration(svc, monkeypatch):
    calls = _probe_gateway(svc, monkeypatch, deployment=_chat_deployment(supports_reasoning=True))
    await svc.test_model("m")
    assert any(b.get("reasoning_effort") == "low" for b in calls["probe_bodies"])

    calls = _probe_gateway(svc, monkeypatch, deployment=_chat_deployment(supports_reasoning=False))
    await svc.test_model("m")
    assert not any(b.get("reasoning_effort") == "low" for b in calls["probe_bodies"])


@pytest.mark.asyncio
async def test_probe_call_surfaces_the_provider_reason_and_raw_stream_text(svc, monkeypatch):
    """The one place the probe touches the wire: JSON for a plain body, raw SSE text for a
    stream, and the provider's message (never a credential-bearing body) on a 4xx."""
    import httpx
    import json
    from ringier_a2a_sdk.model_capabilities import ProbeCallError

    def _handler(request):
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, text="data: {}\ndata: [DONE]\n")
        if body.get("tool_choice") == "required":
            return httpx.Response(400, json={"error": {"message": "forced tool use rejected"}})
        return httpx.Response(200, json={"choices": []})

    monkeypatch.setattr(svc, "_client", httpx.AsyncClient(transport=httpx.MockTransport(_handler)))
    assert await svc._probe_call({"model": "m", "stream": True}) == "data: {}\ndata: [DONE]\n"
    assert await svc._probe_call({"model": "m"}) == {"choices": []}
    with pytest.raises(ProbeCallError, match="forced tool use rejected") as e:
        await svc._probe_call({"model": "m", "tool_choice": "required"})
    assert e.value.status == 400


@pytest.mark.asyncio
async def test_a_transient_failure_on_an_unavoidable_shape_is_inconclusive_and_records_nothing(svc, monkeypatch):
    """A 429 on the streaming shape says nothing about the model: the test fails as
    'inconclusive' so the admin re-runs it, and no record is written — never a rejection."""
    calls = _probe_gateway(svc, monkeypatch, reject=lambda b: "throttled" if b.get("stream") else None)
    calls["reject_status"] = 429
    with pytest.raises(ModelGatewayError, match="inconclusive"):
        await svc.test_model("m")
    assert calls["patched"] == []


@pytest.mark.asyncio
async def test_a_failed_record_write_does_not_fail_a_passing_model(svc, monkeypatch):
    """The console rolls a failed test back; a PATCH hiccup must not delete a model that
    accepted every shape. The report says so via ``recorded``."""
    calls = _probe_gateway(svc, monkeypatch)
    calls["patch_fails"] = True
    result = await svc.test_model("m")
    assert result["recorded"] is False
    assert result["probe"]["rejected"] == []


@pytest.mark.asyncio
async def test_the_record_is_written_to_the_pinned_deployment_id(svc, monkeypatch):
    """Register/edit hand the new deployment's id to the test, so the record lands on the
    live deployment even when the listing still shows the one about to be deleted."""
    listing = {"data": _chat_deployment()["data"] + _chat_deployment(model_id="dep-new")["data"]}
    calls = _probe_gateway(svc, monkeypatch, deployment=listing)
    await svc.test_model("m", model_id="dep-new")
    assert calls["patched"][0][0] == "/model/dep-new/update"


@pytest.mark.asyncio
async def test_a_model_missing_from_the_listing_is_retried_then_still_probed(svc, monkeypatch):
    """One replica's /model/info can lag a just-registered alias; the probe still runs
    against the alias, and the pinned id lets the record be written."""
    listings = iter([{"data": []}, {"data": []}, _chat_deployment()])
    calls = _probe_gateway(svc, monkeypatch)
    real_request = svc._request

    async def _lagging(method, path, **kwargs):
        if path == "/model/info":
            return next(listings, _chat_deployment())
        return await real_request(method, path, **kwargs)

    monkeypatch.setattr(svc, "_request", _lagging)
    monkeypatch.setattr("console_backend.services.model_gateway_service.asyncio.sleep", _noop_sleep)
    result = await svc.test_model("m", model_id="dep-1")
    assert result["recorded"] is True and calls["patched"]


async def _noop_sleep(_):
    return None


@pytest.mark.asyncio
async def test_an_edit_carries_the_record_over_unless_the_model_changed(svc, monkeypatch):
    """The edit form knows nothing of the probe's record; re-registering from it would drop
    the record and make every reader treat the model as unprobed."""
    registered: list[dict] = []

    async def _fake_request(method, path, **kwargs):
        if path == "/model/info":
            return {
                "data": [
                    {
                        "model_name": "m",
                        "litellm_params": {"model": "bedrock/eu.anthropic.claude-sonnet-5-5"},
                        "model_info": {"id": "old", "db_model": True, "nannos_capabilities": {"response_format": False}},
                    }
                ]
            }
        if path == "/model/new":
            registered.append(kwargs["json"])
            return {"model_info": {"id": "new"}}
        if path == "/model/delete":
            return {}
        raise AssertionError(path)

    monkeypatch.setattr(svc, "_request", _fake_request)
    await svc.update_model("old", "m", {"model": "bedrock/eu.anthropic.claude-sonnet-5-5"}, {"mode": "chat"})
    assert registered[-1]["model_info"]["nannos_capabilities"] == {"response_format": False}
    assert registered[-1]["model_info"]["mode"] == "chat"

    await svc.update_model("old", "m", {"model": "bedrock/eu.anthropic.claude-sonnet-5"}, {"mode": "chat"})
    assert "nannos_capabilities" not in registered[-1]["model_info"]


@pytest.mark.asyncio
async def test_an_inconclusive_re_probe_keeps_the_flags_already_recorded(svc, monkeypatch):
    """Recorded {response_format: false, thinking_off: between_tools}; on re-test
    response_format gets a 429. The known `false` must survive — dropping it would let the
    model into the utility tiers, and noise must not erase knowledge (ADR-0015)."""
    calls = _probe_gateway(
        svc,
        monkeypatch,
        reject=lambda b: "throttled" if b.get("response_format") else None,
        deployment=_chat_deployment(nannos_capabilities={"response_format": False, "thinking_off": "between_tools", "probed_at": "old"}),
    )
    calls["reject_status"] = 429
    result = await svc.test_model("m")
    assert result["recorded"] is True
    written = calls["patched"][0][1]["model_info"]["nannos_capabilities"]
    assert written["response_format"] is False  # kept from the prior record
    assert written["thinking_off"] == "disabled"  # measured this time, overwrites
    assert written["probed_at"] != "old"
    assert "response_format" in result["probe"]["inconclusive"]


@pytest.mark.asyncio
async def test_a_pinned_id_reads_the_deployments_own_model_info(svc, monkeypatch):
    """After an edit the alias may still list the old deployment; the thinking shapes must
    follow the NEW deployment's declaration, and the record goes to it."""
    listing = {
        "data": [
            {"model_name": "m", "litellm_params": {"model": "x"}, "model_info": {"mode": "chat", "id": "old", "db_model": True, "supports_reasoning": False}},
            {"model_name": "m", "litellm_params": {"model": "x"}, "model_info": {"mode": "chat", "id": "new", "db_model": True, "supports_reasoning": True}},
        ]
    }
    calls = _probe_gateway(svc, monkeypatch, deployment=listing)
    await svc.test_model("m", model_id="new")
    assert any(b.get("reasoning_effort") == "low" for b in calls["probe_bodies"])
    assert calls["patched"][0][0] == "/model/new/update"


@pytest.mark.asyncio
async def test_an_edit_that_moves_region_does_not_inherit_the_record(svc, monkeypatch):
    registered: list[dict] = []

    async def _fake_request(method, path, **kwargs):
        if path == "/model/info":
            return {
                "data": [
                    {
                        "model_name": "m",
                        "litellm_params": {"model": "bedrock/eu.anthropic.claude-sonnet-5-5", "aws_region_name": "eu-central-1"},
                        "model_info": {"id": "old", "db_model": True, "nannos_capabilities": {"response_format": False}},
                    }
                ]
            }
        if path == "/model/new":
            registered.append(kwargs["json"])
            return {"model_info": {"id": "new"}}
        return {}

    monkeypatch.setattr(svc, "_request", _fake_request)
    await svc.update_model("old", "m", {"model": "bedrock/eu.anthropic.claude-sonnet-5-5", "aws_region_name": "us-east-1"}, {})
    assert "nannos_capabilities" not in registered[-1]["model_info"]


@pytest.mark.asyncio
async def test_a_pinned_config_defined_deployment_is_not_written(svc, monkeypatch):
    """The Test button passes the row's id for every row; a config-file deployment has an id
    but no writable record, so the probe reports and stops — no failed PATCH, no warning."""
    calls = _probe_gateway(svc, monkeypatch, deployment=_chat_deployment(db_model=False))
    result = await svc.test_model("m", model_id="dep-1")
    assert result["recorded"] is None and calls["patched"] == []  # None: not recordable, no warning


@pytest.mark.asyncio
async def test_a_shape_not_attempted_is_dropped_from_the_record(svc, monkeypatch):
    """Reasoning turned off in an edit: the replay shape is no longer attempted, so a stale
    `thinking_replay: false` must not be carried forever (only inconclusive shapes keep theirs)."""
    calls = _probe_gateway(
        svc,
        monkeypatch,
        deployment=_chat_deployment(supports_reasoning=False, nannos_capabilities={"thinking_replay": False, "response_format": True}),
    )
    await svc.test_model("m")
    written = calls["patched"][0][1]["model_info"]["nannos_capabilities"]
    assert "thinking_replay" not in written


@pytest.mark.asyncio
async def test_an_inconclusive_probe_with_an_unreadable_prior_record_does_not_write(svc, monkeypatch):
    """Replica lag: the pinned deployment is unlisted, a shape is inconclusive — writing the
    partial dict would erase the flags the edit carried over, so nothing is written."""
    calls = _probe_gateway(svc, monkeypatch, reject=lambda b: "throttled" if b.get("response_format") else None)
    calls["reject_status"] = 429
    monkeypatch.setattr("console_backend.services.model_gateway_service.asyncio.sleep", _noop_sleep)
    result = await svc.test_model("m", model_id="unlisted")
    assert result["recorded"] is False and calls["patched"] == []


@pytest.mark.asyncio
async def test_the_prior_record_read_does_not_mutate_the_cached_listing(svc, monkeypatch):
    calls = _probe_gateway(svc, monkeypatch, deployment=_chat_deployment(nannos_capabilities={"probed_at": "old"}))
    calls["patch_fails"] = True
    await svc.test_model("m")
    listed = (await svc.get_model("m"))["model_info"]["nannos_capabilities"]
    assert listed == {"probed_at": "old"}


@pytest.mark.asyncio
async def test_a_probe_run_on_the_alias_fallback_keeps_the_pinned_deployments_whole_record(svc, monkeypatch):
    """The pinned deployment stays unlisted through the retries, so the probe ran on the old
    deployment's declaration (no reasoning); by the write the new one is listed. Only the
    target's own declaration may decide what was 'not attempted', so the whole prior stays."""
    old = {"model_name": "m", "litellm_params": {"model": "x"}, "model_info": {"mode": "chat", "id": "old", "db_model": True, "supports_reasoning": False}}
    new = {"model_name": "m", "litellm_params": {"model": "x"}, "model_info": {"mode": "chat", "id": "new", "db_model": True, "supports_reasoning": True, "nannos_capabilities": {"thinking_replay": False, "response_format": True}}}
    calls = _probe_gateway(svc, monkeypatch, deployment={"data": [old]})
    monkeypatch.setattr("console_backend.services.model_gateway_service.asyncio.sleep", _noop_sleep)
    real = svc._request

    async def _request(method, path, **kwargs):
        if path == "/model/info" and calls["probe_bodies"]:  # listed only once the probe has run
            return {"data": [old, new]}
        return await real(method, path, **kwargs)

    monkeypatch.setattr(svc, "_request", _request)
    result = await svc.test_model("m", model_id="new")
    assert result["recorded"] is True
    written = calls["patched"][0][1]["model_info"]["nannos_capabilities"]
    assert written["thinking_replay"] is False


@pytest.mark.asyncio
async def test_no_deployment_found_at_all_is_a_failed_write_not_nothing_to_write(svc, monkeypatch):
    calls = _probe_gateway(svc, monkeypatch, deployment={"data": []})
    monkeypatch.setattr("console_backend.services.model_gateway_service.asyncio.sleep", _noop_sleep)
    result = await svc.test_model("m")
    assert result["recorded"] is False and calls["patched"] == []


@pytest.mark.asyncio
async def test_a_stale_id_resolving_to_a_config_deployment_on_re_read_is_not_recordable(svc, monkeypatch, caplog):
    cfg = {"model_name": "m", "litellm_params": {"model": "x"}, "model_info": {"mode": "chat", "id": "cfg", "db_model": False}}
    dbdep = _chat_deployment()["data"][0]
    calls = _probe_gateway(svc, monkeypatch, deployment={"data": [dbdep]})
    monkeypatch.setattr("console_backend.services.model_gateway_service.asyncio.sleep", _noop_sleep)
    real = svc._request

    async def _request(method, path, **kwargs):
        if path == "/model/info" and calls["probe_bodies"]:  # listed only once the probe has run
            return {"data": [dbdep, cfg]}
        return await real(method, path, **kwargs)

    monkeypatch.setattr(svc, "_request", _request)
    with caplog.at_level("INFO"):
        result = await svc.test_model("m", model_id="cfg")
    assert result["recorded"] is None and calls["patched"] == []
    assert "is not a DB deployment; not recorded" in caplog.text
