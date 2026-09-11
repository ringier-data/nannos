#!/usr/bin/env python
"""Manual QA repro for #217 — two concurrent ``eval`` calls on one thread collide.

Runs the real PTC stack (``_PTCToleranceCodeInterpreterMiddleware`` + the
``ptc_guard`` wrapper + a real QuickJS REPL) against a *scripted* model, so the
one thing the model is not guaranteed to do — emit two ``eval`` tool calls in a
single assistant message — is forced deterministically. No LLM gateway, no
network, no credentials.

Usage (from packages/agent-common):

    uv run python tests/manual/qa_217_parallel_eval.py            # all scenarios
    uv run python tests/manual/qa_217_parallel_eval.py sequential # one scenario

Scenarios and what each proves:

  sequential     CONTROL. The same two programs, one ``eval`` per assistant
                 message. Must pass on a healthy build and on a buggy one — if
                 this fails, the harness or the environment is broken, not #217.

  parallel-repl  Layer 1, the ``langchain_quickjs`` REPL slot. Both ``eval``
                 calls resolve the same ``thread_id``, so ``_repl_for_eval``
                 hands them the *same* QuickJS context; in ``mode="call"`` the
                 first to finish runs ``reset_repl(thread_id)`` in its
                 ``finally``, which closes the context the other is still
                 executing in.

  parallel-hitl  Layers 2 and 3, the ``ptc_guard`` turn collector and HITL
                 batching. Both ``eval`` calls make a high-risk inner call, so
                 both need approval. ``begin_ptc_turn`` is keyed by
                 ``thread_id`` alone, so the second call replaces the first
                 call's turn, and ``take_ptc_pending`` drains one shared list —
                 the two evals' approvals end up in whichever ``interrupt()``
                 fires first. Both parallel scenarios also cover the
                 ``tool_call_history`` write-back: two eval tasks writing that
                 channel in one superstep need a reducer, or LangGraph raises
                 ``InvalidUpdateError`` (the issue calls this "last writer wins"
                 — it is not, it is a hard failure of the turn).

Expected on this branch (the fix is in): all three scenarios PASS, exit code 0.

Before the fix, on ``origin/main`` @ ``1c422039``::

    sequential       PASS
    parallel-repl    RAISED ValueError: already closed
    parallel-hitl    RAISED InvalidHandleError: handle's owning context is closed

Both are the same root cause — the wasmtime store behind the QuickJS context torn
down mid-execution by the other eval's ``reset_repl`` — and which one surfaces
depends on race timing. It is raised *out of the tool node*, so the whole agent
turn dies; in production that is a user-visible hard failure of the conversation,
not a degraded tool result.

A ``NoDefaultModelError`` logged by the tool-call summarizer during
``parallel-hitl`` is expected and harmless — no model is configured here, so the
approval prompt falls back to raw args.

Exit code is 0 when every selected scenario reached its expected outcome for a
FIXED build, 1 otherwise. Set ``QA217_TRACEBACK=1`` for full tracebacks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from collections import deque
from typing import Any

from langchain.agents.factory import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent_common.core.graph_utils import _PTCToleranceCodeInterpreterMiddleware
from agent_common.middleware.loop_detection_middleware import RepeatedToolCallMiddleware
from agent_common.middleware.ptc_guard import wrap_tool_for_ptc

VERBOSE = os.environ.get("QA217_TRACEBACK") == "1"

# Wall-clock budget for one agent run. A collision can also manifest as a hang
# (the losing eval waits on a closed context), so never run without a deadline.
RUN_TIMEOUT_S = 60.0

# How long eval A's inner tool sleeps before returning. It only has to outlast
# eval B's whole program to make the race deterministic; the bug does not depend
# on the delay, the delay just removes the coin flip.
SLOW_MS = 400


class _ProbeArgs(BaseModel):
    tag: str = Field(description="marker echoed back in the result")
    delay_ms: int = Field(default=0, description="sleep before returning")


class _ScriptedModel(BaseChatModel):
    """Replays pre-built AIMessages; accepts ``bind_tools`` like a real model."""

    responses: deque = deque()

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> "_ScriptedModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.responses.popleft())])


def _scorer(score: float):
    """Constant risk scorer — below 0.8 runs inside ``eval``, above asks HITL."""

    async def _fn(tool_name, args, *, tool=None, cache=None, server_slug="_self"):
        return score, None

    return _fn


# Each program tags a global, awaits its inner tool, then reads the global back.
# With one REPL per eval (the contract of ``mode="call"``) each program must see
# only its own tag. Seeing the other's tag means the sandbox was shared.
_PROGRAM = """
globalThis.__owner = '{tag}';
const r = await tools.probe({{tag: '{tag}', delayMs: {delay}}});
JSON.stringify({{result: r, ownerAfterAwait: globalThis.__owner}})
"""

CODE_A = _PROGRAM.format(tag="A", delay=SLOW_MS)
CODE_B = _PROGRAM.format(tag="B", delay=0)


def _build(*, risk: float, parallel: bool) -> tuple[Any, list[str]]:
    """Build an agent whose single model step emits both ``eval`` calls (or not)."""
    executed: list[str] = []

    async def _probe(tag: str, delay_ms: int = 0) -> str:
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        executed.append(tag)
        return f"ok:{tag}"

    inner = StructuredTool.from_function(
        coroutine=_probe, name="probe", description="probe tool", args_schema=_ProbeArgs
    )
    # The real stack's loop guard. Present so the guard records each inner call on
    # the turn's ``tool_call_history`` (under ``eval:probe``) and
    # ``_with_tool_call_history`` writes it back to graph state — that write-back
    # is the observable for layer 2.
    loop_detection = RepeatedToolCallMiddleware()
    wrapped = wrap_tool_for_ptc(
        inner,
        risk_scorer=_scorer(risk),
        default_risk_threshold=0.8,
        loop_detection=loop_detection,
    )
    middleware = _PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[wrapped],
        risk_scorer=_scorer(risk),
        default_risk_threshold=0.8,
        loop_detection=loop_detection,
    )

    call_a = {"id": "call-a", "name": "eval", "args": {"code": CODE_A}}
    call_b = {"id": "call-b", "name": "eval", "args": {"code": CODE_B}}
    if parallel:
        steps = [AIMessage(content="", id="ai-1", tool_calls=[call_a, call_b])]
    else:
        steps = [
            AIMessage(content="", id="ai-1", tool_calls=[call_a]),
            AIMessage(content="", id="ai-2", tool_calls=[call_b]),
        ]

    model = _ScriptedModel()
    model.responses = deque([*steps, AIMessage(content="done", id="ai-final")])
    agent = create_agent(
        model=model,
        tools=[],
        middleware=[loop_detection, middleware],
        checkpointer=InMemorySaver(),
    )
    return agent, executed


def _eval_results(result: dict) -> dict[str, str]:
    return {
        getattr(m, "tool_call_id", "?"): str(m.content)
        for m in result["messages"]
        if getattr(m, "name", None) == "eval"
    }


async def _run(agent, payload, config):
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
            "expected 2 (turn swapped out / stale-seed write-back)"
        )
        ok = False
    print(f"  => {name}: {'PASS' if ok else 'FAIL'}")
    return ok


async def scenario_sequential() -> bool:
    """CONTROL: one eval per assistant message. Must pass on every build."""
    agent, executed = _build(risk=0.1, parallel=False)
    config = {"configurable": {"thread_id": "qa-217-sequential"}}
    result = await _run(agent, {"messages": [HumanMessage("go")]}, config)
    state = await agent.aget_state(config)
    return _report(
        "sequential",
        _eval_results(result),
        executed,
        state.values.get("tool_call_history"),
    )


async def scenario_parallel_repl() -> bool:
    """Two evals in one step, both low risk: the shared QuickJS slot collides."""
    agent, executed = _build(risk=0.1, parallel=True)
    config = {"configurable": {"thread_id": "qa-217-parallel-repl"}}
    result = await _run(agent, {"messages": [HumanMessage("go")]}, config)
    state = await agent.aget_state(config)
    return _report(
        "parallel-repl",
        _eval_results(result),
        executed,
        state.values.get("tool_call_history"),
    )


async def scenario_parallel_hitl() -> bool:
    """Two evals in one step, both high risk: the turn collector and HITL batch collide."""
    agent, executed = _build(risk=0.95, parallel=True)
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
        _eval_results(resumed),
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
