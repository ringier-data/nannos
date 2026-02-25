"""Unit tests for ToolsetSelectorMiddleware.

Tests cover:
- Phase 1 (server-level selection): triggered when total tools > TOOLSET_SELECTION_THRESHOLD
- Phase 2 (tool-level selection): triggered when remaining tools > TOOL_SELECTION_THRESHOLD
- Caching: _cached_selected_tools is used on subsequent calls within same invocation
- always_include: specified tool names are always added regardless of filtering
- Fallbacks: LLM failures fall back to full / truncated tool lists
- Edge cases: no tools, wrong context type, single-server (skip Phase 1)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool

from app.middleware.toolset_selector import (
    ServerSelection,
    ToolSelection,
    ToolsetSelectorMiddleware,
)
from app.models.config import AgentSettings, GraphRuntimeContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str, server: str | None = None) -> BaseTool:
    """Return a minimal mock BaseTool with an optional server_name metadata."""
    tool = MagicMock(spec=BaseTool)
    tool.name = name
    tool.description = f"Description for {name}"
    tool.metadata = {"server_name": server} if server else {}
    return tool


def _make_context(**kwargs) -> GraphRuntimeContext:
    """Return a minimal GraphRuntimeContext with sensible defaults."""
    defaults = {
        "user_id": "user-1",
        "user_sub": "sub-1",
        "name": "Test User",
        "email": "test@example.com",
    }
    defaults.update(kwargs)
    return GraphRuntimeContext(**defaults)


def _make_request(
    context: GraphRuntimeContext | None = None,
    tools: list | None = None,
    messages: list | None = None,
) -> MagicMock:
    """Return a mock ModelRequest."""
    request = MagicMock()
    request.runtime.context = context if context is not None else _make_context()
    request.tools = tools or []
    request.messages = messages or [HumanMessage(content="Do something useful")]
    # override() should return a copy with the new tools
    request.override = MagicMock(return_value=request)
    return request


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def middleware() -> ToolsetSelectorMiddleware:
    return ToolsetSelectorMiddleware()


@pytest.fixture
def middleware_with_always_include() -> ToolsetSelectorMiddleware:
    return ToolsetSelectorMiddleware(always_include=["essential_tool"])


@pytest.fixture
def mock_handler() -> AsyncMock:
    """Async callable that acts as the 'next' middleware handler."""
    handler = AsyncMock(return_value=AIMessage(content="model response"))
    return handler


# ---------------------------------------------------------------------------
# awrap_model_call — happy-path (no filtering needed)
# ---------------------------------------------------------------------------


class TestAwrapModelCallNoFiltering:
    @pytest.mark.asyncio
    async def test_passthrough_when_no_tools(self, middleware: ToolsetSelectorMiddleware, mock_handler: AsyncMock):
        """No tools → handler is called with the original request."""
        ctx = _make_context()
        request = _make_request(context=ctx, tools=[])
        result = await middleware.awrap_model_call(request, mock_handler)

        mock_handler.assert_called_once_with(request)
        assert result == mock_handler.return_value

    @pytest.mark.asyncio
    async def test_passthrough_when_context_is_wrong_type(
        self, middleware: ToolsetSelectorMiddleware, mock_handler: AsyncMock
    ):
        """Non-GraphRuntimeContext → handler is called unchanged with a warning."""
        request = _make_request()
        request.runtime.context = {"not": "a context"}  # wrong type

        result = await middleware.awrap_model_call(request, mock_handler)

        mock_handler.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_tools_below_threshold_skip_llm(self, middleware: ToolsetSelectorMiddleware, mock_handler: AsyncMock):
        """When total tools ≤ TOOLSET_SELECTION_THRESHOLD, no LLM is called."""
        ctx = _make_context()
        # Add a handful of tools (well below threshold of 50)
        tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(5)]
        request = _make_request(context=ctx, tools=tools)

        with (
            patch.object(middleware, "_llm_select_servers", new_callable=AsyncMock) as mock_s,
            patch.object(middleware, "_llm_select_tools", new_callable=AsyncMock) as mock_t,
        ):
            await middleware.awrap_model_call(request, mock_handler)

        mock_s.assert_not_called()
        mock_t.assert_not_called()
        mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_registry_tools_are_included(
        self, middleware: ToolsetSelectorMiddleware, mock_handler: AsyncMock
    ):
        """Tools from tool_registry are merged with request.tools before filtering."""
        ctx = _make_context()
        registry_tool = _make_tool("registry_tool", server="server-a")
        ctx.tool_registry = {"registry_tool": registry_tool}

        request_tool = _make_tool("request_tool")
        request = _make_request(context=ctx, tools=[request_tool])

        captured_tools: list = []

        async def capture_handler(req):
            captured_tools.extend(req.tools or [])
            return AIMessage(content="ok")

        # Patch override so it records the merged tool list
        def fake_override(**kwargs):
            req_copy = MagicMock()
            req_copy.tools = kwargs.get("tools", [])
            return req_copy

        request.override = fake_override

        await middleware.awrap_model_call(request, capture_handler)

        tool_names = {t.name for t in captured_tools if isinstance(t, MagicMock)}
        assert "registry_tool" in tool_names
        assert "request_tool" in tool_names


# ---------------------------------------------------------------------------
# awrap_model_call — caching
# ---------------------------------------------------------------------------


class TestCaching:
    @pytest.mark.asyncio
    async def test_cached_tools_used_on_second_call(
        self, middleware: ToolsetSelectorMiddleware, mock_handler: AsyncMock
    ):
        """When _cached_selected_tools is set, _select_tools is NOT called again."""
        some_tool = _make_tool("cached_tool")
        ctx = _make_context()
        ctx._cached_selected_tools = [some_tool]  # pre-populated cache

        tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(3)]
        request = _make_request(context=ctx, tools=tools)

        with patch.object(middleware, "_select_tools", new_callable=AsyncMock) as mock_select:
            await middleware.awrap_model_call(request, mock_handler)

        mock_select.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_is_written_on_first_call(self, middleware: ToolsetSelectorMiddleware, mock_handler: AsyncMock):
        """After first call, _cached_selected_tools is populated on the context."""
        ctx = _make_context()
        tools = [_make_tool(f"tool_{i}") for i in range(3)]
        request = _make_request(context=ctx, tools=tools)

        selected = [tools[0]]
        with patch.object(middleware, "_select_tools", new_callable=AsyncMock, return_value=selected):
            await middleware.awrap_model_call(request, mock_handler)

        assert ctx._cached_selected_tools == selected


# ---------------------------------------------------------------------------
# awrap_model_call — always_include
# ---------------------------------------------------------------------------


class TestAlwaysInclude:
    @pytest.mark.asyncio
    async def test_always_include_tool_added_when_not_in_filtered_set(self, mock_handler: AsyncMock):
        """An always_include tool that was filtered out is re-injected."""
        essential = _make_tool("essential_tool")
        other = _make_tool("other_tool")

        mw = ToolsetSelectorMiddleware(always_include=["essential_tool"])

        ctx = _make_context()
        request = _make_request(context=ctx, tools=[essential, other])

        # Simulate _select_tools returning only `other` (essential was filtered out)
        with patch.object(mw, "_select_tools", new_callable=AsyncMock, return_value=[other]):
            captured_tools: list = []

            def fake_override(**kwargs):
                captured_tools.extend(kwargs.get("tools", []))
                req_copy = MagicMock()
                req_copy.tools = kwargs.get("tools", [])
                return req_copy

            request.override = fake_override
            await mw.awrap_model_call(request, mock_handler)

        tool_names = {t.name for t in captured_tools if hasattr(t, "name")}
        assert "essential_tool" in tool_names
        assert "other_tool" in tool_names

    @pytest.mark.asyncio
    async def test_always_include_tool_not_duplicated(self, mock_handler: AsyncMock):
        """An always_include tool that is already in the filtered set is not duplicated."""
        essential = _make_tool("essential_tool")
        mw = ToolsetSelectorMiddleware(always_include=["essential_tool"])

        ctx = _make_context()
        request = _make_request(context=ctx, tools=[essential])

        with patch.object(mw, "_select_tools", new_callable=AsyncMock, return_value=[essential]):
            captured_tools: list = []

            def fake_override(**kwargs):
                captured_tools.extend(kwargs.get("tools", []))
                req_copy = MagicMock()
                req_copy.tools = list(kwargs.get("tools", []))
                return req_copy

            request.override = fake_override
            await mw.awrap_model_call(request, mock_handler)

        essential_count = sum(1 for t in captured_tools if hasattr(t, "name") and t.name == "essential_tool")
        assert essential_count == 1


# ---------------------------------------------------------------------------
# awrap_model_call — provider tool dicts are preserved
# ---------------------------------------------------------------------------


class TestProviderToolDicts:
    @pytest.mark.asyncio
    async def test_dict_tools_are_passed_through_unchanged(
        self, middleware: ToolsetSelectorMiddleware, mock_handler: AsyncMock
    ):
        """Provider-specific dict tools in request.tools survive filtering."""
        provider_dict = {"type": "function", "function": {"name": "special"}}
        base_tool = _make_tool("base_tool")

        ctx = _make_context()
        request = _make_request(context=ctx, tools=[base_tool, provider_dict])

        with patch.object(middleware, "_select_tools", new_callable=AsyncMock, return_value=[base_tool]):
            captured_tools: list = []

            def fake_override(**kwargs):
                captured_tools.extend(kwargs.get("tools", []))
                req_copy = MagicMock()
                req_copy.tools = kwargs.get("tools", [])
                return req_copy

            request.override = fake_override
            await middleware.awrap_model_call(request, mock_handler)

        assert provider_dict in captured_tools


# ---------------------------------------------------------------------------
# _get_last_user_message
# ---------------------------------------------------------------------------


class TestGetLastUserMessage:
    def test_returns_last_human_message(self, middleware: ToolsetSelectorMiddleware):
        first = HumanMessage(content="first")
        second = HumanMessage(content="second")
        ai = AIMessage(content="ai reply")
        result = middleware._get_last_user_message([first, ai, second])
        assert result is second

    def test_returns_none_when_no_human_message(self, middleware: ToolsetSelectorMiddleware):
        result = middleware._get_last_user_message([AIMessage(content="hello")])
        assert result is None

    def test_returns_none_for_empty_list(self, middleware: ToolsetSelectorMiddleware):
        result = middleware._get_last_user_message([])
        assert result is None


# ---------------------------------------------------------------------------
# _select_tools — phase thresholds
# ---------------------------------------------------------------------------


class TestSelectToolsPhases:
    @pytest.mark.asyncio
    async def test_phase1_triggered_above_server_threshold(self, middleware: ToolsetSelectorMiddleware):
        # Create more tools than TOOLSET_SELECTION_THRESHOLD
        threshold = AgentSettings.TOOLSET_SELECTION_THRESHOLD
        tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(threshold + 5)]

        with (
            patch.object(middleware, "_llm_select_servers", new_callable=AsyncMock, return_value=tools[:5]) as p1,
            patch.object(middleware, "_llm_select_tools", new_callable=AsyncMock, return_value=tools[:5]) as p2,
        ):
            messages = [HumanMessage(content="Do things")]
            await middleware._select_tools(tools, messages)

        p1.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase1_not_triggered_below_server_threshold(self, middleware: ToolsetSelectorMiddleware):
        threshold = AgentSettings.TOOLSET_SELECTION_THRESHOLD
        tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(threshold - 1)]

        with (
            patch.object(middleware, "_llm_select_servers", new_callable=AsyncMock) as p1,
            patch.object(middleware, "_llm_select_tools", new_callable=AsyncMock) as p2,
        ):
            messages = [HumanMessage(content="Do things")]
            await middleware._select_tools(tools, messages)

        p1.assert_not_called()

    @pytest.mark.asyncio
    async def test_phase2_triggered_after_phase1_if_still_above_tool_threshold(
        self, middleware: ToolsetSelectorMiddleware
    ):
        server_threshold = AgentSettings.TOOLSET_SELECTION_THRESHOLD
        tool_threshold = AgentSettings.TOOL_SELECTION_THRESHOLD

        # Many tools from a single server so Phase 1 won't reduce much
        many_tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(server_threshold + 5)]
        # Phase 1 returns more than TOOL_SELECTION_THRESHOLD
        phase1_result = many_tools[: tool_threshold + 5]

        with (
            patch.object(middleware, "_llm_select_servers", new_callable=AsyncMock, return_value=phase1_result),
            patch.object(
                middleware, "_llm_select_tools", new_callable=AsyncMock, return_value=phase1_result[:tool_threshold]
            ) as p2,
        ):
            await middleware._select_tools(many_tools, [HumanMessage(content="task")])

        p2.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase2_not_triggered_when_tools_within_tool_threshold(self, middleware: ToolsetSelectorMiddleware):
        tool_threshold = AgentSettings.TOOL_SELECTION_THRESHOLD
        tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(tool_threshold - 1)]

        with (
            patch.object(middleware, "_llm_select_servers", new_callable=AsyncMock, return_value=tools),
            patch.object(middleware, "_llm_select_tools", new_callable=AsyncMock) as p2,
        ):
            await middleware._select_tools(tools, [HumanMessage(content="task")])

        p2.assert_not_called()


# ---------------------------------------------------------------------------
# _llm_select_servers
# ---------------------------------------------------------------------------


class TestLlmSelectServers:
    @pytest.mark.asyncio
    async def test_single_server_skips_llm(self, middleware: ToolsetSelectorMiddleware):
        """With only one real server, no LLM call is made."""
        tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(3)]
        tools.append(_make_tool("base_tool"))  # no server

        with patch.object(middleware, "_create_selection_model") as mock_model_factory:
            result = await middleware._llm_select_servers(tools, [HumanMessage(content="task")])

        mock_model_factory.assert_not_called()
        assert result == tools  # unchanged

    @pytest.mark.asyncio
    async def test_multi_server_calls_llm_and_filters(self, middleware: ToolsetSelectorMiddleware):
        """With multiple servers the LLM is called and tools are filtered."""
        tools_a = [_make_tool(f"a_tool_{i}", server="server-a") for i in range(3)]
        tools_b = [_make_tool(f"b_tool_{i}", server="server-b") for i in range(3)]
        base = [_make_tool("base_tool")]  # no server metadata
        all_tools = tools_a + tools_b + base

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(return_value=ServerSelection(servers=["server-a"]))
        mock_llm.with_structured_output = MagicMock(return_value=mock_structured)

        with patch.object(middleware, "_create_selection_model", return_value=mock_llm):
            result = await middleware._llm_select_servers(all_tools, [HumanMessage(content="do a stuff")])

        # Should keep server-a tools + base tools; server-b tools dropped
        result_names = {t.name for t in result}
        for t in tools_a:
            assert t.name in result_names
        for t in base:
            assert t.name in result_names
        for t in tools_b:
            assert t.name not in result_names

    @pytest.mark.asyncio
    async def test_llm_failure_returns_all_tools(self, middleware: ToolsetSelectorMiddleware):
        """LLM error in Phase 1 falls back to returning all tools."""
        tools_a = [_make_tool(f"a_tool_{i}", server="server-a") for i in range(2)]
        tools_b = [_make_tool(f"b_tool_{i}", server="server-b") for i in range(2)]
        all_tools = tools_a + tools_b

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(side_effect=Exception("LLM error"))
        mock_llm.with_structured_output = MagicMock(return_value=mock_structured)

        with patch.object(middleware, "_create_selection_model", return_value=mock_llm):
            result = await middleware._llm_select_servers(all_tools, [HumanMessage(content="task")])

        assert result == all_tools

    @pytest.mark.asyncio
    async def test_no_user_message_returns_all_tools(self, middleware: ToolsetSelectorMiddleware):
        """If there is no user message, all tools are returned unchanged."""
        tools = [_make_tool(f"tool_{i}", server="server-a") for i in range(2)] + [
            _make_tool(f"tool_{i}", server="server-b") for i in range(2)
        ]

        result = await middleware._llm_select_servers(tools, [AIMessage(content="only ai messages")])

        assert result == tools


# ---------------------------------------------------------------------------
# _llm_select_tools
# ---------------------------------------------------------------------------


class TestLlmSelectTools:
    @pytest.mark.asyncio
    async def test_returns_subset_based_on_llm_response(self, middleware: ToolsetSelectorMiddleware):
        """LLM picks two specific tools and they are returned in order."""
        tools = [_make_tool(f"tool_{i}") for i in range(5)]
        selected_names = ["tool_2", "tool_0"]

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(return_value=ToolSelection(tools=selected_names))
        mock_llm.with_structured_output = MagicMock(return_value=mock_structured)

        with patch.object(middleware, "_create_selection_model", return_value=mock_llm):
            result = await middleware._llm_select_tools(tools, [HumanMessage(content="task")], max_tools=5)

        assert [t.name for t in result] == selected_names

    @pytest.mark.asyncio
    async def test_respects_max_tools_cap(self, middleware: ToolsetSelectorMiddleware):
        """Selected tools are capped at max_tools even if LLM returns more."""
        tools = [_make_tool(f"tool_{i}") for i in range(10)]
        llm_names = [f"tool_{i}" for i in range(8)]

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(return_value=ToolSelection(tools=llm_names))
        mock_llm.with_structured_output = MagicMock(return_value=mock_structured)

        with patch.object(middleware, "_create_selection_model", return_value=mock_llm):
            result = await middleware._llm_select_tools(tools, [HumanMessage(content="task")], max_tools=3)

        assert len(result) <= 3

    @pytest.mark.asyncio
    async def test_fallback_to_first_n_when_llm_returns_no_valid_tools(self, middleware: ToolsetSelectorMiddleware):
        """LLM returns unknown names → fallback to first max_tools tools."""
        tools = [_make_tool(f"tool_{i}") for i in range(5)]

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(return_value=ToolSelection(tools=["nonexistent_tool"]))
        mock_llm.with_structured_output = MagicMock(return_value=mock_structured)

        with patch.object(middleware, "_create_selection_model", return_value=mock_llm):
            result = await middleware._llm_select_tools(tools, [HumanMessage(content="task")], max_tools=3)

        assert result == tools[:3]

    @pytest.mark.asyncio
    async def test_fallback_on_llm_exception(self, middleware: ToolsetSelectorMiddleware):
        """LLM exception → fallback to first max_tools tools."""
        tools = [_make_tool(f"tool_{i}") for i in range(5)]

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(side_effect=Exception("LLM timeout"))
        mock_llm.with_structured_output = MagicMock(return_value=mock_structured)

        with patch.object(middleware, "_create_selection_model", return_value=mock_llm):
            result = await middleware._llm_select_tools(tools, [HumanMessage(content="task")], max_tools=2)

        assert result == tools[:2]

    @pytest.mark.asyncio
    async def test_no_user_message_truncates_to_max_tools(self, middleware: ToolsetSelectorMiddleware):
        """No user message → fall back to first max_tools tools immediately."""
        tools = [_make_tool(f"tool_{i}") for i in range(5)]

        result = await middleware._llm_select_tools(tools, [AIMessage(content="no human here")], max_tools=2)

        assert result == tools[:2]
