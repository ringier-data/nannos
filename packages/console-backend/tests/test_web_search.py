"""Tests for the console_web_search MCP tool: model resolution, citation parsing, result
formatting, and the endpoint's unavailable/failure/success paths.

Web search runs as one isolated, function-tool-free gateway call against a web-search-capable
model; the active model is the `search` default when capable, else the cheapest capable model.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import console_backend.routers.web_search_mcp_tools as endpoint
import console_backend.services.llm_gateway as llm_gateway
from console_backend.services.web_search import (
    format_web_search_result,
    pick_web_search_model,
    resolve_web_search_config,
    resolve_web_search_model,
)


def _raw(*entries):
    """Gateway list_models payload from (name, supports_web_search, input_cost[, model_id]) tuples."""
    out = []
    for e in entries:
        name, cap, cost = e[0], e[1], e[2]
        info = {"supports_web_search": cap, "input_cost_per_token": cost}
        if len(e) > 3:
            info["id"] = e[3]
        out.append({"model_name": name, "model_info": info})
    return out


def _caps(*entries):
    """{name: model_info} from (name, supports_web_search, input_cost) tuples."""
    return {n: {"supports_web_search": cap, "input_cost_per_token": cost} for n, cap, cost in entries}


# --- pick_web_search_model ----------------------------------------------------------------


class TestPickModel:
    def test_auto_cheapest_capable(self):
        info = _caps(("pro", True, 2e-6), ("flash", True, 5e-7), ("claude", None, 1e-6))
        assert pick_web_search_model(None, info) == ("flash", "auto")

    def test_selected_default_wins(self):
        info = _caps(("pro", True, 2e-6), ("flash", True, 5e-7))
        assert pick_web_search_model("pro", info) == ("pro", "selected")

    def test_selected_not_capable_falls_back_to_auto(self):
        info = _caps(("flash", True, 5e-7), ("claude", None, 1e-6))
        assert pick_web_search_model("claude", info) == ("flash", "auto")

    def test_none_when_no_capable(self):
        assert pick_web_search_model(None, _caps(("claude", None, 1e-6))) == (None, None)

    def test_unknown_cost_sorts_last(self):
        info = _caps(("known", True, 1e-6), ("unknown", True, None))
        assert pick_web_search_model(None, info) == ("known", "auto")

    def test_unreadable_gateway_trusts_explicit_default(self):
        assert pick_web_search_model("some-model", None) == ("some-model", "selected")

    def test_unreadable_gateway_no_default_is_none(self):
        assert pick_web_search_model(None, None) == (None, None)


# --- format_web_search_result -------------------------------------------------------------


class TestFormat:
    def test_answer_with_sources(self):
        out = format_web_search_result("Node 24 is LTS.", [{"title": "nodejs.org", "url": "https://nodejs.org"}])
        assert "Node 24 is LTS." in out
        assert "Sources:" in out
        assert "1. nodejs.org — https://nodejs.org" in out

    def test_answer_without_sources(self):
        assert format_web_search_result("Just an answer.", []) == "Just an answer."

    def test_empty_answer_placeholder(self):
        assert "no text answer" in format_web_search_result("", [])

    def test_caps_citations(self):
        cites = [{"title": f"s{i}", "url": f"https://e/{i}"} for i in range(20)]
        out = format_web_search_result("x", cites)
        assert "10. s9" in out
        assert "11. s10" not in out


# --- resolve_web_search_model -------------------------------------------------------------


def _request(list_models_return=None, list_models_raises=False, defaults=None):
    from console_backend.services.model_gateway_service import ModelGatewayError

    if list_models_raises:
        list_models = AsyncMock(side_effect=ModelGatewayError("down"))
    else:
        list_models = AsyncMock(return_value=list_models_return or [])
    state = SimpleNamespace(
        model_gateway_service=SimpleNamespace(list_models=list_models),
        model_defaults_service=SimpleNamespace(get_all=AsyncMock(return_value=defaults or {})),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


class TestResolveModel:
    @pytest.mark.asyncio
    async def test_resolves_cheapest_capable(self):
        raw = [
            {"model_name": "pro", "model_info": {"supports_web_search": True, "input_cost_per_token": 2e-6}},
            {"model_name": "flash", "model_info": {"supports_web_search": True, "input_cost_per_token": 5e-7}},
        ]
        assert await resolve_web_search_model(_request(raw), db=None) == "flash"

    @pytest.mark.asyncio
    async def test_honors_search_default(self):
        raw = [
            {"model_name": "pro", "model_info": {"supports_web_search": True, "input_cost_per_token": 2e-6}},
            {"model_name": "flash", "model_info": {"supports_web_search": True, "input_cost_per_token": 5e-7}},
        ]
        assert await resolve_web_search_model(_request(raw, defaults={"search": "pro"}), db=None) == "pro"

    @pytest.mark.asyncio
    async def test_none_when_gateway_unreadable(self):
        assert await resolve_web_search_model(_request(list_models_raises=True), db=None) is None

    @pytest.mark.asyncio
    async def test_none_when_no_capable(self):
        raw = [{"model_name": "claude", "model_info": {}}]
        assert await resolve_web_search_model(_request(raw), db=None) is None


# --- resolve_web_search_config (backend-owned picker state) -------------------------------


class TestResolveConfig:
    def test_auto_marks_cheapest_active_and_sorts_cheapest_first(self):
        cfg = resolve_web_search_config(
            _raw(("pro", True, 2e-6, "id-pro"), ("flash", True, 5e-7, "id-flash"), ("claude", None, 1e-6, "id-c")),
            search_default=None,
        )
        assert cfg.available is True
        assert cfg.source == "auto"
        assert cfg.active_model_id == "id-flash"
        assert cfg.active_model_name == "flash"
        # Only capable models, cheapest first; flash is both cheapest and active.
        assert [m.model_name for m in cfg.models] == ["flash", "pro"]
        assert cfg.models[0].is_cheapest and cfg.models[0].is_active
        assert not cfg.models[1].is_cheapest and not cfg.models[1].is_active

    def test_selected_default_is_active_but_cheapest_still_marked(self):
        cfg = resolve_web_search_config(
            _raw(("pro", True, 2e-6, "id-pro"), ("flash", True, 5e-7, "id-flash")),
            search_default="pro",
        )
        assert cfg.source == "selected"
        assert cfg.active_model_id == "id-pro"
        active = [m for m in cfg.models if m.is_active]
        assert [m.model_name for m in active] == ["pro"]
        # The cheapest badge tracks the cheapest model, not the active one.
        assert [m.model_name for m in cfg.models if m.is_cheapest] == ["flash"]

    def test_non_capable_default_falls_back_to_auto(self):
        cfg = resolve_web_search_config(
            _raw(("flash", True, 5e-7, "id-flash"), ("claude", None, 1e-6, "id-c")),
            search_default="claude",
        )
        assert cfg.source == "auto"
        assert cfg.active_model_name == "flash"
        # The non-capable default is never offered as an option.
        assert [m.model_name for m in cfg.models] == ["flash"]

    def test_unavailable_when_none_capable(self):
        cfg = resolve_web_search_config(_raw(("claude", None, 1e-6, "id-c")), search_default=None)
        assert cfg.available is False
        assert cfg.source is None
        assert cfg.active_model_id is None
        assert cfg.models == []


# --- gateway_web_search citation parsing --------------------------------------------------


class TestGatewayWebSearch:
    @pytest.mark.asyncio
    async def test_parses_content_and_dedupes_citations_and_sends_options(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": "Latest LTS is Node 24.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {"title": "nodejs.org", "url": "https://nodejs.org"},
                                },
                                {"type": "url_citation", "url_citation": {"title": "dup", "url": "https://nodejs.org"}},
                                {"type": "other", "url_citation": {"url": "https://skip"}},
                            ],
                        }
                    }
                ]
            }
        )
        fake_client = SimpleNamespace(post=AsyncMock(return_value=resp))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            answer, citations = await llm_gateway.gateway_web_search("node lts?", model="gemini-search")

        assert answer == "Latest LTS is Node 24."
        assert citations == [{"title": "nodejs.org", "url": "https://nodejs.org"}]  # deduped, type-filtered

        body = fake_client.post.call_args.kwargs["json"]
        assert body["model"] == "gemini-search"
        assert body["web_search_options"] == {"search_context_size": "medium"}
        assert "tools" not in body  # isolated, function-tool-free call

    @pytest.mark.asyncio
    async def test_parses_vertex_gemini_grounding_metadata(self):
        # Gemini grounds via top-level vertex_ai_grounding_metadata and emits NO annotations;
        # parsing only annotations would drop every citation. Sources live in groundingChunks[].web.
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={
                "choices": [{"message": {"content": "Node 24 is the active LTS."}}],
                "vertex_ai_grounding_metadata": [
                    {
                        "groundingChunks": [
                            {"web": {"uri": "https://herodevs.com/a", "title": "herodevs.com"}},
                            {"web": {"uri": "https://nodejs.org/b", "title": "nodejs.org"}},
                            {"web": {"uri": "https://herodevs.com/a", "title": "dup"}},  # deduped
                        ]
                    }
                ],
            }
        )
        fake_client = SimpleNamespace(post=AsyncMock(return_value=resp))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            answer, citations = await llm_gateway.gateway_web_search("node lts?", model="gemini-3-flash-preview")

        assert answer == "Node 24 is the active LTS."
        assert citations == [
            {"title": "herodevs.com", "url": "https://herodevs.com/a"},
            {"title": "nodejs.org", "url": "https://nodejs.org/b"},
        ]

    @pytest.mark.asyncio
    async def test_empty_choices_returns_empty_answer_not_indexerror(self):
        # A 2xx with no choices (content-filter / moderation) must not raise IndexError.
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"choices": []})
        fake_client = SimpleNamespace(post=AsyncMock(return_value=resp))
        with patch.object(llm_gateway._client, "get", return_value=fake_client):
            answer, citations = await llm_gateway.gateway_web_search("q", model="m")
        assert answer == "" and citations == []


# --- endpoint -----------------------------------------------------------------------------


def _req(headers: dict | None = None):
    """A minimal request whose .headers.get(...) works (FastAPI Request stand-in)."""
    return SimpleNamespace(headers=headers or {})


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_unavailable_when_no_model(self):
        with patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value=None)):
            out = await endpoint.web_search_mcp(_req(), query="q", db=None, user=SimpleNamespace(sub="u1"))
        assert "Web search is unavailable" in out

    @pytest.mark.asyncio
    async def test_success_formats_answer_and_sources(self):
        with (
            patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value="gemini-flash")),
            patch.object(
                endpoint,
                "gateway_web_search",
                AsyncMock(return_value=("Answer.", [{"title": "t", "url": "https://u"}])),
            ),
        ):
            out = await endpoint.web_search_mcp(_req(), query="q", db=None, user=SimpleNamespace(sub="u1"))
        assert "Answer." in out and "https://u" in out

    @pytest.mark.asyncio
    async def test_failure_is_caught(self):
        with (
            patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value="m")),
            patch.object(endpoint, "gateway_web_search", AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            out = await endpoint.web_search_mcp(_req(), query="q", db=None, user=SimpleNamespace(sub="u1"))
        # Failure is caught and returned as a readable message, but the exception detail is NOT
        # leaked to the calling agent (CodeQL: information exposure through an exception).
        assert out.startswith("Web search failed") and "boom" not in out

    @pytest.mark.asyncio
    async def test_forwards_conversation_id_and_overrides_user_sub(self):
        # The orchestrator stamps attribution on the MCP request; the endpoint forwards it to the
        # gateway call but always uses the authenticated user_sub (never the client-supplied one).
        header = json.dumps({"conversation_id": "conv-123", "sub_agent_id": "sa-9", "user_sub": "spoofed"})
        gw = AsyncMock(return_value=("Answer.", []))
        with (
            patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value="gemini-flash")),
            patch.object(endpoint, "gateway_web_search", gw),
        ):
            await endpoint.web_search_mcp(
                _req({"x-nannos-context": header}),
                query="q",
                db=None,
                user=SimpleNamespace(sub="real-user"),
            )
        meta = gw.call_args.kwargs["metadata"]
        assert meta["conversation_id"] == "conv-123"
        assert meta["sub_agent_id"] == "sa-9"
        assert meta["user_sub"] == "real-user"  # authenticated identity wins over the header

    @pytest.mark.asyncio
    async def test_malformed_attribution_header_is_ignored(self):
        gw = AsyncMock(return_value=("Answer.", []))
        with (
            patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value="m")),
            patch.object(endpoint, "gateway_web_search", gw),
        ):
            await endpoint.web_search_mcp(
                _req({"x-nannos-context": "not-json"}),
                query="q",
                db=None,
                user=SimpleNamespace(sub="u1"),
            )
        assert gw.call_args.kwargs["metadata"] == {"user_sub": "u1"}
