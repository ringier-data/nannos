"""PTC ``eval`` namespace filtering: keep it minimal on both the normal and resume paths.

Two related guarantees, both undermined before these fixes:

1. **Normal model call** — the ``eval`` ``tools.*`` namespace must reflect exactly
   what the agent carries on ``request.tools`` (for the GP agent, the
   ToolsetSelector-filtered subset). It must NOT silently union the full build-time
   baseline (``broaden_baseline_tools``), which would re-expose every tool the
   selector dropped.

2. **Interrupt resume** — the eval tool node replays without a model call, so
   ``request.tools`` is empty. The namespace is rebuilt from ``broaden_baseline_tools``
   filtered to the exposure set checkpointed by ``aafter_model`` on the original call,
   so the resume REPL mirrors the original (filtered) namespace rather than the full
   registry. Absent a checkpointed set, it safely falls back to the full baseline.

Plus the ``execute`` execution-gate: ``FilesystemMiddleware`` binds a dead ``execute``
even on non-sandbox backends; it must be kept out of the PTC namespace AND stripped
from the model's bound tools when the backend cannot run commands.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

import agent_common.core.graph_utils as gu


@pytest.fixture(autouse=True)
def _ptc_enabled(monkeypatch):
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")


def _tool(name: str) -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(coroutine=_fn, name=name, description=name)


def _mw(*, supports_execution: bool, static=None, baseline=None):
    return gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=static if static is not None else [_tool("read_file")],
        broaden_baseline_tools=baseline if baseline is not None else [],
        ptc_enabled=True,
        broaden_exposure=True,
        backend_supports_execution=supports_execution,
    )


def _req(**kwargs):
    return SimpleNamespace(**kwargs)


def test_normal_path_does_not_union_full_baseline():
    """On a model call, request.tools is authoritative; dropped baseline tools stay dropped."""
    mw = _mw(supports_execution=False, baseline=[_tool("mcp_a"), _tool("mcp_b")])
    names = {t.name for t in mw._collect_ptc_tools(_req(tools=[_tool("mcp_a")], state={}))}
    assert "mcp_a" in names
    assert "mcp_b" not in names, f"baseline tool re-exposed despite filtering; got {names}"
    assert "read_file" in names  # static fs baseline always present


def test_execute_not_exposed_on_non_sandbox_request_harvest():
    """A dead ``execute`` arriving via request.tools must not reach the PTC namespace."""
    mw = _mw(supports_execution=False)
    names = {t.name for t in mw._collect_ptc_tools(_req(tools=[_tool("execute"), _tool("mcp_a")], state={}))}
    assert "execute" not in names, f"dead execute leaked into PTC namespace; got {names}"
    assert "mcp_a" in names


def test_execute_exposed_once_on_sandbox():
    mw = _mw(supports_execution=True, static=[_tool("execute")])
    collected = mw._collect_ptc_tools(_req(tools=[_tool("execute")], state={}))
    names = [t.name for t in collected]
    assert names.count("execute") == 1, f"execute should be exposed exactly once; got {names}"


def test_resume_filters_baseline_by_checkpointed_set():
    """Resume (no request.tools) rebuilds from baseline filtered to the checkpointed set."""
    mw = _mw(supports_execution=False, baseline=[_tool("mcp_a"), _tool("mcp_b")])
    req = _req(state={gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY: ["mcp_a"]})
    names = {t.name for t in mw._collect_ptc_tools(req)}
    assert "mcp_a" in names
    assert "mcp_b" not in names, f"resume re-exposed a tool not in the checkpointed set; got {names}"
    assert "read_file" in names


def test_resume_without_checkpoint_falls_back_to_full_baseline():
    mw = _mw(supports_execution=False, baseline=[_tool("mcp_a"), _tool("mcp_b")])
    names = {t.name for t in mw._collect_ptc_tools(_req(state={}))}
    assert {"mcp_a", "mcp_b", "read_file"} <= names


def test_prompt_relays_exposure_and_strips_dead_execute():
    mw = _mw(supports_execution=False)
    mw._prepare_for_call = lambda req: "PROMPT"  # avoid heavy REPL setup
    _prompt, hidden = mw._ptc_prompt_and_hidden(_req(tools=[_tool("execute"), _tool("mcp_a")], state={}))
    # Dead execute is stripped from the model's bound tools even though not PTC-exposed.
    assert "execute" in hidden
    assert {"mcp_a", "read_file"} <= hidden
    # The relayed exposure set (for the checkpoint) excludes the dead execute.
    relayed = gu._ptc_exposed_names_var.get()
    assert relayed is not None
    assert "execute" not in relayed
    assert "mcp_a" in relayed


@pytest.mark.asyncio
async def test_aafter_model_checkpoints_relayed_exposure():
    mw = _mw(supports_execution=False)
    mw._prepare_for_call = lambda req: "PROMPT"
    mw._ptc_prompt_and_hidden(_req(tools=[_tool("mcp_a")], state={}))
    update = await mw.aafter_model({}, None)
    assert update == {gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY: gu._ptc_exposed_names_var.get()}
    assert "mcp_a" in update[gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY]


def test_state_key_matches_state_field():
    assert gu.PTC_EXPOSED_TOOL_NAMES_STATE_KEY in gu._PTCExposureState.__annotations__
