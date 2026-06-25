"""Tests for the console_web_search MCP tool: model resolution, citation parsing, result
formatting, and the endpoint's unavailable/failure/success paths.

Web search runs as one isolated, function-tool-free gateway call against a web-search-capable
model; the active model is the `search` default when capable, else the cheapest capable model.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import console_backend.routers.web_search_mcp_tools as endpoint
import console_backend.services.llm_gateway as llm_gateway
from console_backend.services.web_search import (
    format_web_search_result,
    pick_web_search_model,
    resolve_web_search_model,
)


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
                                {"type": "url_citation", "url_citation": {"title": "nodejs.org", "url": "https://nodejs.org"}},
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


# --- endpoint -----------------------------------------------------------------------------


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_unavailable_when_no_model(self):
        with patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value=None)):
            out = await endpoint.web_search_mcp(
                SimpleNamespace(), query="q", db=None, user=SimpleNamespace(sub="u1")
            )
        assert "Web search is unavailable" in out

    @pytest.mark.asyncio
    async def test_success_formats_answer_and_sources(self):
        with patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value="gemini-flash")), patch.object(
            endpoint, "gateway_web_search", AsyncMock(return_value=("Answer.", [{"title": "t", "url": "https://u"}]))
        ):
            out = await endpoint.web_search_mcp(
                SimpleNamespace(), query="q", db=None, user=SimpleNamespace(sub="u1")
            )
        assert "Answer." in out and "https://u" in out

    @pytest.mark.asyncio
    async def test_failure_is_caught(self):
        with patch.object(endpoint, "resolve_web_search_model", AsyncMock(return_value="m")), patch.object(
            endpoint, "gateway_web_search", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            out = await endpoint.web_search_mcp(
                SimpleNamespace(), query="q", db=None, user=SimpleNamespace(sub="u1")
            )
        assert out.startswith("Web search failed:") and "boom" in out
