"""A tool call nothing could resolve must be answered with the fix, not a dead end.

Regression for the risk-gate incident: under PTC the ``eval`` prompt advertises tools
as camelCase members, models lift one of those identifiers out of the prompt and emit
it as a *native* tool call, and dispatch answered with ``Error: Tool 'X' is not
available`` — which the model relayed to the user as "unavailable in this session",
against the standing instruction never to report a tool as missing.
"""

from types import SimpleNamespace

from agent_common.middleware.conditional_hitl import ConditionalHumanInTheLoopMiddleware

from app.core.graph_factory import _create_hitl_middleware
from app.middleware.dynamic_tool_dispatch import _unresolved_tool_content


def _context(registry: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_registry=registry)


class TestUnresolvedToolContent:
    def test_camel_identifier_points_into_eval_under_ptc(self, monkeypatch):
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        content = _unresolved_tool_content(
            "consoleCreateBugReport", _context({"console_create_bug_report": object()})
        )
        assert "eval" in content
        assert "tools.consoleCreateBugReport" in content
        assert "console_create_bug_report" in content

    def test_camel_identifier_points_at_the_snake_name_without_ptc(self, monkeypatch):
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "0")
        content = _unresolved_tool_content("githubSearchIssues", _context({"github_search_issues": object()}))
        assert "github_search_issues" in content
        # PTC is off: pointing the model into `eval` would be wrong advice.
        assert "eval" not in content

    def test_unknown_name_points_at_discovery(self, monkeypatch):
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        content = _unresolved_tool_content("frobnicate_widget", _context({"github_search_issues": object()}))
        assert "did not resolve" in content
        # Never a dead end: it must say how to find the real name.
        assert "console_grep_mcp_tools" in content or "tools.search" in content

    def test_never_reports_the_tool_as_missing(self, monkeypatch):
        """The old wording ("is not available") is what leaked to end users."""
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        for name in ("consoleCreateBugReport", "frobnicate_widget"):
            content = _unresolved_tool_content(name, _context({"console_create_bug_report": object()}))
            assert "is not available" not in content


class TestHitlMiddlewareStaticTools:
    """The orchestrator's static tools are dispatched from ToolNode / ``static_tools``,
    not from the per-user registry, so the gate can only fetch their schemas if they
    are handed to it — and a call it cannot fetch is never classified.
    """

    def test_static_tools_are_registered_with_the_gate(self):
        from langchain_core.tools import StructuredTool

        tool = StructuredTool.from_function(
            func=lambda message: message, name="FinalResponseSchema", description="Final response."
        )
        mw = _create_hitl_middleware([tool])

        assert isinstance(mw, ConditionalHumanInTheLoopMiddleware)
        assert mw._get_tool_instance("FinalResponseSchema", None) is tool

    def test_no_static_tools_keeps_the_previous_behaviour(self):
        mw = _create_hitl_middleware()
        assert mw._get_tool_instance("FinalResponseSchema", None) is None
