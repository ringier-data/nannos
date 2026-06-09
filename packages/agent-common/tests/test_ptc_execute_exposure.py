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
