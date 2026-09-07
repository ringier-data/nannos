"""``eval`` self-correction: top-level ``return`` (prompt rule + IIFE retry) and ``tools.<x> is not a function`` hints."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

import agent_common.core.graph_utils as gu

RETURN_ERROR = (
    '<error type="SyntaxError">source parse error in &lt;eval&gt;: '
    "A 'return' statement can only be used within a function body.</error>"
)


class _Request:
    """Minimal ToolCallRequest stand-in with the immutable ``override`` used by the middleware."""

    def __init__(self, tool_call, runtime=None):
        self.tool_call = tool_call
        self.runtime = runtime
        self.tools = []
        self.state = {}

    def override(self, **kw):
        new = _Request(kw.get("tool_call", self.tool_call), self.runtime)
        return new


def _base_tool(name: str) -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(coroutine=_fn, name=name, description=name)


def _mw():
    return gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[_base_tool("read_file")],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=True,
        backend_supports_execution=False,
    )


def _msg(content: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="c1", name="eval")


@pytest.mark.asyncio
async def test_top_level_return_is_retried_wrapped_in_an_async_iife():
    mw = _mw()
    seen: list[str] = []

    async def handler(req):
        seen.append(req.tool_call["args"]["code"])
        return _msg(RETURN_ERROR) if len(seen) == 1 else _msg('<result kind="number">1</result>')

    req = _Request({"name": mw._tool_name, "args": {"code": "const x = 1;\nreturn x;"}, "id": "c1"})
    out = await mw.awrap_tool_call(req, handler)

    assert len(seen) == 2
    assert seen[0] == "const x = 1;\nreturn x;"
    assert seen[1] == "(async () => {\nconst x = 1;\nreturn x;\n})()"
    assert "<result" in out.content


@pytest.mark.asyncio
async def test_other_errors_and_successes_are_not_retried():
    mw = _mw()
    for content in ('<error type="ReferenceError">x is not defined</error>', '<result kind="number">1</result>'):
        handler = AsyncMock(return_value=_msg(content))
        req = _Request({"name": mw._tool_name, "args": {"code": "x"}, "id": "c1"})
        out = await mw.awrap_tool_call(req, handler)
        assert handler.await_count == 1
        assert out.content == content


@pytest.mark.asyncio
async def test_non_eval_tools_pass_straight_through():
    mw = _mw()
    handler = AsyncMock(return_value=_msg(RETURN_ERROR))
    req = _Request({"name": "some_other_tool", "args": {"code": "return 1"}, "id": "c1"})
    await mw.awrap_tool_call(req, handler)
    assert handler.await_count == 1


def test_detector_handles_block_content_and_non_messages():
    assert gu._is_top_level_return_parse_error(_msg(RETURN_ERROR))
    assert gu._is_top_level_return_parse_error(ToolMessage(content=[{"type": "text", "text": RETURN_ERROR}], tool_call_id="c"))
    assert not gu._is_top_level_return_parse_error(_msg('<error type="SyntaxError">unexpected token</error>'))
    assert not gu._is_top_level_return_parse_error(None)
    assert not gu._is_top_level_return_parse_error(SimpleNamespace(content=42))


def test_prompt_states_the_script_rule():
    mw = _mw()
    mw._ptc = None  # no PTC tools attached this turn → base prompt path
    prompt = mw._prepare_for_call(SimpleNamespace(state={}))
    assert "top-level `return` is a SyntaxError" in prompt


NOT_A_FUNCTION = '<error type="TypeError">not a function\n    at &lt;eval&gt; (eval_script:1:12)\n</error>'


def _mcp_tool(name: str) -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(coroutine=_fn, name=name, description=name, metadata={"server_name": "github"})


@pytest.mark.asyncio
async def test_not_a_function_hint_with_discovery_attached(monkeypatch):
    """Core-only mode (tools.search exposed): snake_case → camelCase, synonyms → search, unknown → search."""
    mw = _mw()
    monkeypatch.setattr(gu, "resolve_ptc_thread_id", lambda runtime: "t1")
    mw._ptc_tools_by_thread["t1"] = (_mcp_tool("github_get_me"), _mcp_tool("github_list_commits"), _mcp_tool("search"))
    handler = AsyncMock(return_value=_msg(NOT_A_FUNCTION))
    code = "const me = await tools.github_get_me({});\nawait tools.search_tools({query: 'x'});\nawait tools.githubListCommits({});\nawait tools.frobnicate({})"
    req = _Request({"name": mw._tool_name, "args": {"code": code}, "id": "c1"})

    out = await mw.awrap_tool_call(req, handler)

    assert handler.await_count == 1  # no retry, just annotation
    assert out.content.startswith(NOT_A_FUNCTION) and "<hint>" in out.content
    hint = out.content.split("<hint>")[1]
    assert "`tools.github_get_me` does not exist — tool names are camelCase here: use `tools.githubGetMe`" in hint
    assert "`tools.search_tools` does not exist — use `tools.search`" in hint
    assert "`tools.githubListCommits`" not in hint  # valid access not flagged
    assert "`tools.frobnicate` is not exposed in this eval" in hint and "tools.search({ query" in hint


@pytest.mark.asyncio
async def test_not_a_function_hint_without_discovery_lists_callables_and_points_to_task(monkeypatch):
    """Inline mode (the orchestrator): no tools.search — never suggest it; list what is callable."""
    mw = _mw()
    monkeypatch.setattr(gu, "resolve_ptc_thread_id", lambda runtime: "t1")
    mw._ptc_tools_by_thread["t1"] = (_mcp_tool("read_file"), _mcp_tool("get_current_time"))
    handler = AsyncMock(return_value=_msg(NOT_A_FUNCTION))
    code = "await tools.console_grep_mcp_tools({ query: 'github' });\nawait tools.github_get_me({});\nawait tools.search_tools({})"
    req = _Request({"name": mw._tool_name, "args": {"code": code}, "id": "c1"})

    out = await mw.awrap_tool_call(req, handler)

    hint = out.content.split("<hint>")[1]
    assert "tools.search(" not in hint, "must not advertise a discovery helper this eval does not have"
    assert "`tools.console_grep_mcp_tools` is not available inside eval. Call `console_grep_mcp_tools` as a regular tool call" in hint
    assert "delegate work that needs them with `task`" in hint
    assert "`tools.github_get_me` is not available inside eval." in hint
    assert "Callable here: `tools.getCurrentTime`, `tools.readFile`" in hint
    assert "`tools.search_tools` does not exist and this eval has no discovery helper" in hint


@pytest.mark.asyncio
async def test_not_a_function_without_tools_access_is_left_alone(monkeypatch):
    mw = _mw()
    monkeypatch.setattr(gu, "resolve_ptc_thread_id", lambda runtime: "t1")
    handler = AsyncMock(return_value=_msg(NOT_A_FUNCTION))
    req = _Request({"name": mw._tool_name, "args": {"code": "const f = 1; f()"}, "id": "c1"})
    out = await mw.awrap_tool_call(req, handler)
    assert out.content == NOT_A_FUNCTION


def test_raw_mcp_listers_are_never_in_the_eval_namespace():
    mw = _mw()
    grep = _mcp_tool("console_grep_mcp_tools")
    small = [grep, _mcp_tool("console_list_mcp_servers"), _mcp_tool("github_get_me")]
    names_small = {t.name for t in mw._collect_ptc_tools(SimpleNamespace(tools=small, state={}))}
    assert "console_grep_mcp_tools" not in names_small and "console_list_mcp_servers" not in names_small
    assert "github_get_me" in names_small, "only the listers are removed"

    big = [grep] + [_mcp_tool(f"github_tool_{i}") for i in range(gu.PTC_INLINE_RENDER_THRESHOLD + 1)]
    names_big = {t.name for t in mw._collect_ptc_tools(SimpleNamespace(tools=big, state={}))}
    assert "console_grep_mcp_tools" not in names_big
    assert {"search", "describe"} <= names_big, "core-only mode adds in-sandbox discovery"
