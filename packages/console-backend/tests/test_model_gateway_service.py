"""Tests for ModelGatewayService._request log behavior.

The model catalog probes an internal proxy endpoint that newer LiteLLM versions don't
expose (it falls back to the pinned public cost map). That expected 404 must not be logged
as an error — `optional=True` downgrades it to debug.
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
    assert paths == ["/model/new", "/model/delete"]  # register first, then delete old

    _, new_body = calls[0]
    assert new_body["model_name"] == "claude-sonnet-4-6"
    assert new_body["model_info"]["input_modes"] == ["text", "image", "file"]
    assert calls[1][1] == {"id": "old-id"}  # old deployment deleted by id
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
