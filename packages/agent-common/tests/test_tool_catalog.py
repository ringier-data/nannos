"""Unit tests for the native lazy tool-catalog surface (tool_catalog.py).

Covers the three meta-tools (search_tools/describe_tool/call_tool), the
call_tool -> real-tool request rewrite in ToolCatalogMiddleware, and the
DynamicLocalAgentRunnable catalog-mode seams (bound tools, exposed catalog,
runtime context).
"""

from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable
from agent_common.core.tool_catalog import (
    CATALOG_CALL_TOOL_NAME,
    CATALOG_DESCRIBE_TOOL_NAME,
    CATALOG_SEARCH_TOOL_NAME,
    ToolCatalogMiddleware,
)


class _CampaignArgs(BaseModel):
    campaign_id: str = Field(description="Cockpit campaign id")


def _make_tool(name: str, description: str) -> StructuredTool:
    async def _impl(campaign_id: str) -> str:
        return f"{name}:{campaign_id}"

    return StructuredTool.from_function(
        coroutine=_impl,
        name=name,
        description=description,
        args_schema=_CampaignArgs,
    )


@pytest.fixture
def catalog() -> dict[str, StructuredTool]:
    return {
        "cockpit_get_campaign": _make_tool(
            "cockpit_get_campaign", "Fetch one Cockpit campaign by id."
        ),
        "cockpit_list_campaigns": _make_tool(
            "cockpit_list_campaigns", "List Cockpit campaigns for an advertiser."
        ),
        "gam_get_report": _make_tool("gam_get_report", "Fetch a GAM delivery report."),
    }


@pytest.fixture
def middleware(catalog) -> ToolCatalogMiddleware:
    return ToolCatalogMiddleware(catalog)


def _meta_tool(middleware: ToolCatalogMiddleware, name: str):
    return next(t for t in middleware.tools if t.name == name)


class TestMetaTools:
    def test_contributes_three_meta_tools(self, middleware):
        assert {t.name for t in middleware.tools} == {
            CATALOG_SEARCH_TOOL_NAME,
            CATALOG_DESCRIBE_TOOL_NAME,
            CATALOG_CALL_TOOL_NAME,
        }

    @pytest.mark.asyncio
    async def test_search_ranks_name_matches_first(self, middleware):
        search = _meta_tool(middleware, CATALOG_SEARCH_TOOL_NAME)
        hits = await search.coroutine(query="list campaigns")
        names = [h["name"] for h in hits]
        assert names[0] == "cockpit_list_campaigns"
        assert "gam_get_report" not in names

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_nothing(self, middleware):
        search = _meta_tool(middleware, CATALOG_SEARCH_TOOL_NAME)
        assert await search.coroutine(query="???") == []

    @pytest.mark.asyncio
    async def test_describe_returns_parameters_schema(self, middleware):
        describe = _meta_tool(middleware, CATALOG_DESCRIBE_TOOL_NAME)
        text = await describe.coroutine(name="cockpit_get_campaign")
        assert "cockpit_get_campaign" in text
        assert "campaign_id" in text

    @pytest.mark.asyncio
    async def test_describe_unknown_suggests_and_points_to_search(self, middleware):
        describe = _meta_tool(middleware, CATALOG_DESCRIBE_TOOL_NAME)
        text = await describe.coroutine(name="cockpit_campaign")
        assert "No tool named" in text
        assert CATALOG_SEARCH_TOOL_NAME in text
        # Close names are suggested
        assert "cockpit_get_campaign" in text

    @pytest.mark.asyncio
    async def test_call_stub_fails_loudly_without_middleware(self, middleware):
        call = _meta_tool(middleware, CATALOG_CALL_TOOL_NAME)
        with pytest.raises(RuntimeError, match="ToolCatalogMiddleware"):
            await call.coroutine(
                name="cockpit_get_campaign", args={"campaign_id": "c1"}
            )


def _request(tool_call: dict) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call=tool_call, tool=None, state={}, runtime=MagicMock()
    )


class TestCallToolDispatch:
    @pytest.mark.asyncio
    async def test_rewrites_to_real_tool_and_args(self, middleware, catalog):
        request = _request(
            {
                "name": CATALOG_CALL_TOOL_NAME,
                "args": {"name": "cockpit_get_campaign", "args": {"campaign_id": "c1"}},
                "id": "call-1",
            }
        )
        seen: list[ToolCallRequest] = []

        async def handler(req):
            seen.append(req)
            return ToolMessage(
                content="ok",
                tool_call_id=req.tool_call["id"],
                name=req.tool_call["name"],
            )

        result = await middleware.awrap_tool_call(request, handler)
        assert len(seen) == 1
        rewritten = seen[0]
        # Inner middlewares (HITL, retry) must see the REAL tool call
        assert rewritten.tool is catalog["cockpit_get_campaign"]
        assert rewritten.tool_call["name"] == "cockpit_get_campaign"
        assert rewritten.tool_call["args"] == {"campaign_id": "c1"}
        # The provider matches on tool_call_id, which must be preserved
        assert rewritten.tool_call["id"] == "call-1"
        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_other_tools_pass_through_untouched(self, middleware):
        request = _request({"name": "get_current_time", "args": {}, "id": "call-2"})

        async def handler(req):
            assert req is request
            return ToolMessage(
                content="12:00", tool_call_id="call-2", name="get_current_time"
            )

        result = await middleware.awrap_tool_call(request, handler)
        assert result.content == "12:00"

    @pytest.mark.asyncio
    async def test_unknown_inner_name_returns_error_with_suggestions(self, middleware):
        request = _request(
            {
                "name": CATALOG_CALL_TOOL_NAME,
                "args": {"name": "cockpit_get_campaig", "args": {}},
                "id": "call-3",
            }
        )

        async def handler(req):  # pragma: no cover - must not be reached
            raise AssertionError("handler must not run for unknown catalog tools")

        result = await middleware.awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == "call-3"
        assert "cockpit_get_campaign" in result.content

    @pytest.mark.asyncio
    async def test_non_dict_args_returns_error(self, middleware):
        request = _request(
            {
                "name": CATALOG_CALL_TOOL_NAME,
                "args": {"name": "cockpit_get_campaign", "args": "campaign_id=c1"},
                "id": "call-4",
            }
        )

        async def handler(req):  # pragma: no cover - must not be reached
            raise AssertionError("handler must not run for malformed args")

        result = await middleware.awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert CATALOG_DESCRIBE_TOOL_NAME in result.content


class TestDynamicAgentCatalogMode:
    @pytest.fixture
    def config(self):
        return LocalLangGraphSubAgentConfig(
            type="langgraph",
            name="general-purpose",
            description="GP agent",
            system_prompt="You are the general-purpose agent.",
        )

    def _essential(self, name: str) -> StructuredTool:
        async def _impl(campaign_id: str) -> str:
            return name

        return StructuredTool.from_function(
            coroutine=_impl,
            name=name,
            description=f"{name} essential",
            args_schema=_CampaignArgs,
        )

    def test_effective_tools_bind_essentials_only(self, config, catalog):
        essentials = [
            self._essential("get_current_time"),
            self._essential("docstore_search"),
        ]
        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=MagicMock(),
            orchestrator_tools=essentials + [self._essential("not_essential")],
            tool_catalog=catalog,
        )
        tools = runnable._get_effective_tools()
        assert {t.name for t in tools} == {"get_current_time", "docstore_search"}

    def test_exposed_catalog_excludes_context_gated_tools(self, config, catalog):
        gated = dict(catalog)
        gated["read_personal_file"] = _make_tool(
            "read_personal_file", "Read a personal workspace file."
        )
        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=MagicMock(),
            tool_catalog=gated,
        )
        exposed = runnable._exposed_catalog()
        assert "read_personal_file" not in exposed
        assert set(exposed) == set(catalog)

    def test_exposed_catalog_none_without_catalog(self, config):
        runnable = DynamicLocalAgentRunnable(config=config, model=MagicMock())
        assert runnable._exposed_catalog() is None

    def _build_graph_kwargs(self, config, catalog, monkeypatch, ptc_enabled: bool) -> dict:
        """Call _build_graph with a patched builder and return its kwargs."""
        import agent_common.agents.dynamic_agent as da

        monkeypatch.setattr(da, "code_interpreter_ptc_enabled", lambda: ptc_enabled)
        captured: dict = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            graph = MagicMock()
            graph.with_config.return_value = graph
            return graph

        monkeypatch.setattr(da, "build_sub_agent_graph", fake_build)
        runnable = DynamicLocalAgentRunnable(config=config, model=MagicMock(), tool_catalog=catalog)
        runnable._cached_tools = []
        runnable._build_graph()
        return captured

    def test_build_graph_forwards_context_registry_flag(self, config, catalog, monkeypatch):
        kwargs = self._build_graph_kwargs(config, catalog, monkeypatch, ptc_enabled=True)
        assert kwargs["expose_context_registry"] is True
        # Under PTC the native meta-tool middleware must NOT be added (eval covers it)
        assert not any(isinstance(m, ToolCatalogMiddleware) for m in kwargs["extra_middlewares"])

    def test_build_graph_adds_catalog_middleware_without_ptc(self, config, catalog, monkeypatch):
        kwargs = self._build_graph_kwargs(config, catalog, monkeypatch, ptc_enabled=False)
        assert any(isinstance(m, ToolCatalogMiddleware) for m in kwargs["extra_middlewares"])

    def test_build_graph_server_map_covers_catalog(self, config, monkeypatch):
        tool = _make_tool("cockpit_get_campaign", "Fetch one Cockpit campaign by id.")
        tool.metadata = {"server_name": "alloy-riad-stg"}
        kwargs = self._build_graph_kwargs(
            config, {"cockpit_get_campaign": tool}, monkeypatch, ptc_enabled=True
        )
        assert kwargs["tool_server_map"] == {"cockpit_get_campaign": "alloy-riad-stg"}
