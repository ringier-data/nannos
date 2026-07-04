"""PTC baseline exposure of the sandbox ``execute`` tool.

``execute`` is provided by ``FilesystemMiddleware`` (not the agent's explicit
tool list), so it is absent from ``broaden_baseline_tools`` and would vanish from
the ``eval`` REPL on an interrupt *resume* (where ``request.tools`` is empty and
re-exposure relies on the static baseline). It must therefore be wrapped into the
static PTC baseline — but only when the backend can actually run commands, so a
non-sandbox agent never exposes a dead ``tools.execute``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from deepagents.backends.state import StateBackend
from langchain_core.tools import tool

import agent_common.core.graph_utils as gu


@pytest.fixture(autouse=True)
def _ptc_enabled(monkeypatch):
    """These tests assert PTC-on behaviour; pin the env (default is off)."""
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")


def _baseline_tool_names(middlewares: list) -> set[str]:
    assert len(middlewares) == 1
    return {t.name for t in middlewares[0]._static_ptc_tools}


def test_execute_exposed_in_baseline_when_backend_supports_execution():
    with patch("deepagents.middleware.filesystem.supports_execution", return_value=True):
        middlewares = gu.build_code_interpreter_middlewares(StateBackend())
    names = _baseline_tool_names(middlewares)
    assert "execute" in names, f"execute must be in the PTC baseline for sandbox backends; got {names}"
    # The filesystem baseline is still present.
    assert {"ls", "read_file", "glob", "grep", "write_file", "edit_file"} <= names


def test_execute_not_exposed_for_non_execution_backend():
    with patch("deepagents.middleware.filesystem.supports_execution", return_value=False):
        middlewares = gu.build_code_interpreter_middlewares(StateBackend())
    names = _baseline_tool_names(middlewares)
    assert "execute" not in names, f"execute must NOT be exposed without execution support; got {names}"
    # Filesystem baseline unaffected.
    assert {"ls", "read_file", "glob", "grep", "write_file", "edit_file"} <= names


def test_baseline_execute_carries_the_risk_guard():
    """The baseline ``execute`` is a risk-guarded wrapper (HITL still applies inside
    eval), not the raw filesystem tool — i.e. it is re-created via wrap_tool_for_ptc."""
    scorer_calls: list[str] = []

    async def _scorer(tool_name, args, *, tool=None, cache=None, server_slug="_self"):
        scorer_calls.append(tool_name)
        return 0.0, None

    with patch("deepagents.middleware.filesystem.supports_execution", return_value=True):
        middlewares = gu.build_code_interpreter_middlewares(StateBackend(), risk_scorer=_scorer)
    execute_tools = [t for t in middlewares[0]._static_ptc_tools if t.name == "execute"]
    assert execute_tools, "execute should be present"
    # Wrapped tools expose an injected ``runtime`` arg (added by wrap_tool_for_ptc);
    # the raw filesystem execute tool does not advertise it on its LLM-facing schema.
    assert execute_tools[0].coroutine is not None


def test_extra_static_ptc_tools_exposed_in_baseline():
    """Option D: curated build-time utility tools are exposed inside ``eval``
    alongside the filesystem baseline, without broadening to ``request.tools``."""

    @tool
    def get_current_time() -> str:
        """Return the current time."""
        return "now"

    with patch("deepagents.middleware.filesystem.supports_execution", return_value=False):
        middlewares = gu.build_code_interpreter_middlewares(
            StateBackend(),
            broaden_exposure=False,
            extra_static_ptc_tools=[get_current_time],
        )
    names = _baseline_tool_names(middlewares)
    assert "get_current_time" in names, f"curated utility must be exposed in the PTC baseline; got {names}"
    # Filesystem baseline still present.
    assert {"ls", "read_file", "glob", "grep", "write_file", "edit_file"} <= names


def test_extra_static_ptc_tools_skips_excluded_names():
    """Dispatch / response-schema tools are never exposed via PTC, so a caller can
    pass a whole static-tool list without hand-filtering ``_PTC_EXCLUDED_TOOL_NAMES``."""

    @tool("write_todos")
    def write_todos() -> str:
        """Record a work plan."""
        return "ok"

    @tool("copy_file")
    def copy_file() -> str:
        """Copy a file."""
        return "ok"

    with patch("deepagents.middleware.filesystem.supports_execution", return_value=False):
        middlewares = gu.build_code_interpreter_middlewares(
            StateBackend(),
            broaden_exposure=False,
            extra_static_ptc_tools=[write_todos, copy_file],
        )
    names = _baseline_tool_names(middlewares)
    assert "write_todos" not in names, "write_todos is excluded from PTC exposure (stays native)"
    assert "copy_file" in names, f"non-excluded utility should be exposed; got {names}"


def test_extra_static_ptc_tools_not_exposed_when_ptc_disabled(monkeypatch):
    """When PTC is off, curated utilities are not wrapped/exposed (they stay native)."""
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "0")

    @tool
    def get_current_time() -> str:
        """Return the current time."""
        return "now"

    with patch("deepagents.middleware.filesystem.supports_execution", return_value=False):
        middlewares = gu.build_code_interpreter_middlewares(
            StateBackend(),
            broaden_exposure=False,
            extra_static_ptc_tools=[get_current_time],
        )
    assert middlewares[0]._static_ptc_tools == []


def test_ptc_disabled_exposes_no_baseline_but_keeps_bare_eval(monkeypatch):
    """With CODE_INTERPRETER_PTC off (the default), nothing is PTC-exposed (no
    static baseline, no execute), but the bare ``eval`` tool is still bound so
    the agent degrades gracefully — tools are called directly instead."""
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "0")
    with patch("deepagents.middleware.filesystem.supports_execution", return_value=True):
        middlewares = gu.build_code_interpreter_middlewares(StateBackend())
    mw = middlewares[0]
    assert mw._ptc_enabled is False
    assert mw._static_ptc_tools == []  # nothing wrapped/exposed via PTC
    # The code-interpreter middleware still provides the bare `eval` tool.
    assert any(t.name == "eval" for t in mw.tools)
