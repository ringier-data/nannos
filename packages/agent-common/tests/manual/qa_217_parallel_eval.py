#!/usr/bin/env python
"""Manual QA repro for #217 — two concurrent ``eval`` calls on one thread collide.

Runs the real PTC stack (``_PTCToleranceCodeInterpreterMiddleware`` + the
``ptc_guard`` wrapper + a real QuickJS REPL) against a *scripted* model, so the one
thing the model is not guaranteed to do — emit two ``eval`` tool calls in a single
assistant message — is forced deterministically. No LLM gateway, no network, no
credentials.

Usage (from packages/agent-common):

    uv run python tests/manual/qa_217_parallel_eval.py            # all scenarios
    uv run python tests/manual/qa_217_parallel_eval.py sequential # one scenario

Scenarios and what each proves:

  sequential     CONTROL. The same two programs, one ``eval`` per assistant
                 message. Must pass on a healthy build and on a buggy one — if this
                 fails, the harness or the environment is broken, not #217.

  parallel-repl  The ``langchain_quickjs`` REPL slot. Both ``eval`` calls resolve
                 the same ``thread_id``, so ``_repl_for_eval`` hands them the *same*
                 QuickJS context; in ``mode="call"`` the first to finish runs
                 ``reset_repl`` in its ``finally``, closing the context the other is
                 still executing in.

  parallel-hitl  The ``ptc_guard`` turn collector and HITL batching. Both ``eval``
                 calls make a high-risk inner call, so both need approval.
                 ``begin_ptc_turn`` is keyed by ``thread_id`` alone, so the second
                 call replaced the first call's turn, and ``take_ptc_pending``
                 drained one shared list — the approvals ended up in whichever
                 ``interrupt()`` fired first.

Both parallel scenarios also cover the ``tool_call_history`` write-back: two eval
tasks writing that channel in one superstep need a reducer, or LangGraph raises
``InvalidUpdateError`` (the issue calls this "last writer wins" — it is not, it is a
hard failure of the turn).

Expected on this branch (the fix is in): all three scenarios PASS, exit code 0.

Before the fix, on ``origin/main`` @ ``1c422039``::

    sequential       PASS
    parallel-repl    RAISED ValueError: already closed
    parallel-hitl    RAISED InvalidHandleError: handle's owning context is closed

Both are the same root cause — the wasmtime store behind the QuickJS context torn
down mid-execution by the other eval's ``reset_repl`` — and which one surfaces
depends on race timing. It is raised *out of the tool node*, so the whole agent turn
dies; in production that is a user-visible hard failure of the conversation, not a
degraded tool result.

A ``NoDefaultModelError`` logged by the tool-call summarizer during ``parallel-hitl``
is expected and harmless — no model is configured here, so the approval prompt falls
back to raw args.

Exit code is 0 when every selected scenario reached its expected outcome for a FIXED
build, 1 otherwise. Set ``QA217_TRACEBACK=1`` for full tracebacks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

# The agent builder, the JS programs and the result reader are imported from the
# pytest module rather than copied: this script is not run in CI, so a forked copy
# would rot silently the next time the middleware constructor, ``wrap_tool_for_ptc``
# or the resume shape changes. Everything unique to manual QA — the scenarios, the
# diagnostics, the control case — lives here.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_ptc_parallel_eval import build_eval_agent, eval_results  # noqa: E402

VERBOSE = os.environ.get("QA217_TRACEBACK") == "1"

# Wall-clock budget for one agent run. A collision can also manifest as a hang
# (the losing eval waits on a closed context), so never run without a deadline.
RUN_TIMEOUT_S = 60.0


async def _run(agent: Any, payload: Any, config: dict) -> Any:
    return await asyncio.wait_for(agent.ainvoke(payload, config), RUN_TIMEOUT_S)


def _report(name: str, evals: dict[str, str], executed: list[str], history: Any) -> bool:
    """Print the observed evidence and judge it against the fixed-build contract."""
    print(f"  eval call-a result : {evals.get('call-a', '<MISSING>')}")
    print(f"  eval call-b result : {evals.get('call-b', '<MISSING>')}")
    print(f"  inner tool executed: {sorted(executed)}")
    print(f"  tool_call_history  : {history}")

    ok = True
    for tag in ("a", "b"):
        content = evals.get(f"call-{tag}", "")
        want = f"ok:{tag.upper()}"
        if want not in content:
            print(f"  FAIL: eval call-{tag} did not return {want!r} (sandbox closed or clobbered)")
            ok = False
        if f'"ownerAfterAwait":"{tag.upper()}"' not in content.replace(" ", ""):
            print(f"  FAIL: eval call-{tag} lost its own REPL global across the await (shared sandbox)")
            ok = False
    if sorted(executed) != ["A", "B"]:
        print(f"  FAIL: expected both inner calls to run exactly once, got {executed}")
        ok = False
    # The loop guard records program-made calls under ``eval:<inner tool>``; both
    # evals' inner calls must survive the write-back, not just the last writer's.
    inner_hashes = (history or {}).get("eval:probe", [])
    if len(inner_hashes) != 2:
        print(
            f"  FAIL: tool_call_history['eval:probe'] kept {len(inner_hashes)} inner call(s), "
            "expected 2 (a turn was swapped out, or one task's write-back was lost)"
        )
        ok = False
    print(f"  => {name}: {'PASS' if ok else 'FAIL'}")
    return ok


async def scenario_sequential() -> bool:
    """CONTROL: one eval per assistant message. Must pass on every build."""
    agent, executed = build_eval_agent(0.1, parallel=False)
    config = {"configurable": {"thread_id": "qa-217-sequential"}}
    result = await _run(agent, {"messages": [HumanMessage("go")]}, config)
    state = await agent.aget_state(config)
    return _report(
        "sequential",
        eval_results(result),
        executed,
        state.values.get("tool_call_history"),
    )


async def scenario_parallel_repl() -> bool:
    """Two evals in one step, both low risk: the shared QuickJS slot collides."""
    agent, executed = build_eval_agent(0.1, parallel=True)
    config = {"configurable": {"thread_id": "qa-217-parallel-repl"}}
    result = await _run(agent, {"messages": [HumanMessage("go")]}, config)
    state = await agent.aget_state(config)
    return _report(
        "parallel-repl",
        eval_results(result),
        executed,
        state.values.get("tool_call_history"),
    )


async def scenario_parallel_hitl() -> bool:
    """Two evals in one step, both high risk: the turn collector and HITL batch collide."""
    agent, executed = build_eval_agent(0.95, parallel=True)
    config = {"configurable": {"thread_id": "qa-217-parallel-hitl"}}

    first = await _run(agent, {"messages": [HumanMessage("go")]}, config)
    interrupts = first.get("__interrupt__") or []
    asked = [len(getattr(i, "value", {}).get("action_requests", []) or []) for i in interrupts]
    print(f"  interrupts raised  : {len(interrupts)} (action_requests each: {asked})")
    if sum(asked) != 2:
        print("  FAIL: expected 2 approval asks in total, one per eval — approvals were batched onto one turn")

    # Approve everything that was actually asked, keyed by interrupt id. Once the
    # evals no longer share a turn each raises its OWN interrupt, and LangGraph >=1.2
    # rejects a bare blanket resume while more than one is pending — so this mirrors
    # what the orchestrator does for real (executor._build_interrupt_resume_map).
    resume_map = {
        intr.id: {"decisions": [{"type": "approve"} for _ in (getattr(intr, "value", {}).get("action_requests") or [])]}
        for intr in interrupts
    }
    resumed = await _run(agent, Command(resume=resume_map), config)
    state = await agent.aget_state(config)
    ok = _report(
        "parallel-hitl",
        eval_results(resumed),
        executed,
        state.values.get("tool_call_history"),
    )
    return ok and sum(asked) == 2


SCENARIOS = {
    "sequential": scenario_sequential,
    "parallel-repl": scenario_parallel_repl,
    "parallel-hitl": scenario_parallel_hitl,
}


async def main(names: list[str]) -> int:
    outcomes: dict[str, str] = {}
    for name in names:
        print(f"\n=== {name} " + "=" * (60 - len(name)))
        try:
            outcomes[name] = "PASS" if await SCENARIOS[name]() else "FAIL"
        except TimeoutError:
            print(f"  HUNG: no result within {RUN_TIMEOUT_S:.0f}s — a losing eval is stuck on a closed sandbox")
            outcomes[name] = "HUNG"
        except Exception as exc:  # noqa: BLE001 - the crash IS the observation
            if VERBOSE:
                traceback.print_exc()
            print(f"  RAISED: {type(exc).__name__}: {exc}")
            if not VERBOSE:
                print("         (re-run with QA217_TRACEBACK=1 for the full traceback)")
            outcomes[name] = f"RAISED {type(exc).__name__}"

    print("\n=== summary " + "=" * 56)
    for name in names:
        print(f"  {name:<16} {outcomes[name]}")
    failed = [n for n, o in outcomes.items() if o != "PASS"]
    if failed:
        print(f"\n#217 reproduced — not clean: {', '.join(failed)}")
        return 1
    print("\nAll scenarios clean — #217 does not reproduce on this build.")
    return 0


if __name__ == "__main__":
    requested = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in requested if n not in SCENARIOS]
    if unknown:
        sys.exit(f"unknown scenario(s): {', '.join(unknown)}; pick from {', '.join(SCENARIOS)}")
    sys.exit(asyncio.run(main(requested)))
