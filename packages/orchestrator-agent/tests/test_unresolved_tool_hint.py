"""A tool call nothing could resolve must be answered with the fix, not a dead end.

Regression for the risk-gate incident: under PTC the ``eval`` prompt advertises tools
as camelCase members, models lift one of those identifiers out of the prompt and emit
it as a *native* tool call, and dispatch answered with ``Error: Tool 'X' is not
available`` — which the model relayed to the user as "unavailable in this session",
against the standing instruction never to report a tool as missing.
"""

from types import SimpleNamespace

from agent_common.middleware.conditional_hitl import ConditionalHumanInTheLoopMiddleware

from agent_common.core.graph_utils import deep_agent_builtin_tools

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

    def test_unknown_name_under_ptc_points_at_the_eval_namespace(self, monkeypatch):
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        content = _unresolved_tool_content(
            "frobnicate_widget",
            _context({"github_search_issues": object(), "console_grep_mcp_tools": object()}),
        )
        assert "did not resolve" in content
        # Under PTC the callable surface is the eval namespace, not the native one.
        assert "`tools.*`" in content
        assert "eval" in content
        # The raw catalogue listers stay natively bound even under PTC.
        assert "console_grep_mcp_tools" in content
        # `tools.search` is pinned only in core-only mode — never promised from here.
        assert "tools.search" not in content

    def test_unknown_name_without_ptc_points_at_the_listers(self, monkeypatch):
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "0")
        content = _unresolved_tool_content(
            "frobnicate_widget",
            _context({"console_grep_mcp_tools": object(), "console_list_mcp_servers": object()}),
        )
        assert "console_grep_mcp_tools" in content
        assert "console_list_mcp_servers" in content
        # PTC is off: pointing the model into `eval` would be wrong advice.
        assert "eval" not in content

    def test_discovery_tools_are_only_named_when_the_user_has_them(self, monkeypatch):
        """Naming a tool the user does not have would be its own dead end."""
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "0")
        content = _unresolved_tool_content("frobnicate_widget", _context({"github_search_issues": object()}))
        assert "console_grep_mcp_tools" not in content
        assert "console_list_mcp_servers" not in content
        # Still actionable rather than a dead end.
        assert "re-issue" in content
        assert "task" in content

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
        # Without a complete list the gate must not conclude anything from absence.
        assert mw._platform_tools_are_exhaustive is False


class TestDeepAgentBuiltinsAreRegistered:
    """``create_deep_agent`` installs write_todos + the filesystem tools itself, so
    their instances exist only inside that call. The gate needs them: they are
    dispatchable, so absence of one must not read as "this name is invented", and
    their approval cards need a real args_schema.
    """

    def test_builtins_are_fetchable_and_the_set_is_marked_complete(self):
        mw = _create_hitl_middleware(deep_agent_builtin_tools(None), exhaustive=True)

        for name in ("write_todos", "ls", "read_file", "write_file", "edit_file", "glob", "grep"):
            assert mw._get_tool_instance(name, None) is not None, name
        assert mw._platform_tools_are_exhaustive is True

    def test_seeded_names_match_the_tools_the_graph_actually_installs(self):
        """Migration 090 seeds risk policy for these by name. If deepagents renames or
        adds one, the seed silently stops applying — so pin the list here.
        """
        names = {t.name for t in deep_agent_builtin_tools(None)}
        assert names == {
            "write_todos",
            "ls",
            "glob",
            "grep",
            "read_file",
            "write_file",
            "edit_file",
            "execute",
        }
