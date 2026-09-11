"""Two ``eval`` tool calls in ONE assistant message must not collide (#217).

Nothing guarantees a model step emits at most one ``eval`` call, and ``ToolNode``
runs the calls of one message concurrently — but every piece of per-``eval``
bookkeeping on the PTC path is keyed by ``thread_id`` alone. Before
``serialized_eval`` the two calls shared a QuickJS context (the first to finish
closed it under the other, killing the agent turn with ``already closed``), shared
one ``_PTC_TURNS`` entry, and shared one HITL collector.

These tests script the model so the two-``eval`` message is emitted
deterministically; everything under it is the production stack. The manual QA
harness in ``tests/manual/qa_217_parallel_eval.py`` covers the same ground with
more diagnostics.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import pytest

from langchain.agents.factory import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent_common.core.graph_utils import _PTCToleranceCodeInterpreterMiddleware
from agent_common.middleware.loop_detection_middleware import (
    RepeatedToolCallMiddleware,
    history_delta,
    merge_tool_call_history,
)
from agent_common.middleware.ptc_guard import wrap_tool_for_ptc

# Eval A's inner call outlasts eval B's whole program, so "B finishes while A is
# still inside its sandbox" happens every run instead of on a coin flip.
_SLOW_MS = 400


class _ProbeArgs(BaseModel):
    tag: str = Field(description="marker echoed back")
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
    async def _fn(tool_name, args, *, tool=None, cache=None, server_slug="_self"):
        return score, None

    return _fn


# Tag a REPL global, await the inner tool, read the global back. With one REPL per
# eval, each program must see only its own tag; seeing the other's means the
# sandbox was shared.
_PROGRAM = """
globalThis.__owner = '{tag}';
const r = await tools.probe({{tag: '{tag}', delayMs: {delay}}});
JSON.stringify({{result: r, ownerAfterAwait: globalThis.__owner}})
"""


def build_eval_agent(risk: float, *, parallel: bool = True) -> tuple[Any, list[str]]:
    """An agent that emits two ``eval`` calls, in one model step or in two.

    Shared with the manual QA harness (``tests/manual/qa_217_parallel_eval.py``) so
    the two cannot drift apart; ``parallel=False`` is that harness's control case.
    """
    executed: list[str] = []

    async def _probe(tag: str, delay_ms: int = 0) -> str:
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        executed.append(tag)
        return f"ok:{tag}"

    inner = StructuredTool.from_function(
        coroutine=_probe, name="probe", description="probe tool", args_schema=_ProbeArgs
    )
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
    call_a = {"id": "call-a", "name": "eval", "args": {"code": _PROGRAM.format(tag="A", delay=_SLOW_MS)}}
    call_b = {"id": "call-b", "name": "eval", "args": {"code": _PROGRAM.format(tag="B", delay=0)}}
    steps = (
        [AIMessage(content="", id="ai-1", tool_calls=[call_a, call_b])]
        if parallel
        else [
            AIMessage(content="", id="ai-1", tool_calls=[call_a]),
            AIMessage(content="", id="ai-2", tool_calls=[call_b]),
        ]
    )
    model = _ScriptedModel()
    model.responses = deque([*steps, AIMessage(content="done", id="ai-final")])
    agent = create_agent(model=model, tools=[], middleware=[loop_detection, middleware], checkpointer=InMemorySaver())
    return agent, executed


def eval_results(result: dict) -> dict[str, str]:
    return {
        getattr(m, "tool_call_id", "?"): str(m.content)
        for m in result["messages"]
        if getattr(m, "name", None) == "eval"
    }


def _assert_both_evals_intact(evals: dict[str, str], executed: list[str], history: Any) -> None:
    for call_id, tag in (("call-a", "A"), ("call-b", "B")):
        content = evals[call_id]
        # Its own inner result came back — the sandbox was not closed under it.
        assert f"ok:{tag}" in content, f"{call_id} lost its result: {content}"
        # Its own REPL global survived the await — the sandbox was not shared.
        assert f'"ownerAfterAwait":"{tag}"' in content.replace(" ", ""), f"{call_id} saw a foreign REPL: {content}"
    assert sorted(executed) == ["A", "B"], f"inner tool ran {executed}, expected once per eval"
    # Both evals' inner calls survive the write-back, not just the last writer's.
    assert len(history.get("eval:probe", [])) == 2, f"write-back dropped an eval's records: {history}"


async def test_two_evals_in_one_step_do_not_collide():
    """Low-risk inner calls: both programs run to completion with their own sandbox."""
    agent, executed = build_eval_agent(risk=0.1)
    config = {"configurable": {"thread_id": "ptc-parallel-low"}}

    result = await asyncio.wait_for(agent.ainvoke({"messages": [HumanMessage("go")]}, config), 60)

    state = await agent.aget_state(config)
    _assert_both_evals_intact(eval_results(result), executed, state.values.get("tool_call_history") or {})


async def test_two_evals_in_one_step_each_get_their_own_approval():
    """High-risk inner calls: one interrupt per eval, and each honours its own decision.

    Before the fix both evals recorded onto one thread-keyed collector, so the
    approvals were batched into whichever ``interrupt()`` fired first and the other
    eval's call was never asked about.
    """
    agent, executed = build_eval_agent(risk=0.95)
    config = {"configurable": {"thread_id": "ptc-parallel-high"}}

    first = await asyncio.wait_for(agent.ainvoke({"messages": [HumanMessage("go")]}, config), 60)

    interrupts = first.get("__interrupt__") or []
    asked = [len(getattr(i, "value", {}).get("action_requests") or []) for i in interrupts]
    assert sum(asked) == 2, f"expected one approval ask per eval, got {asked}"
    # Nothing ran yet: both calls are blocked pending approval.
    assert executed == []

    # LangGraph >=1.2 requires an id-keyed resume while more than one interrupt is
    # pending — the same shape the orchestrator builds (_build_interrupt_resume_map).
    resume_map = {
        intr.id: {"decisions": [{"type": "approve"} for _ in (getattr(intr, "value", {}).get("action_requests") or [])]}
        for intr in interrupts
    }
    resumed = await asyncio.wait_for(agent.ainvoke(Command(resume=resume_map), config), 60)

    state = await agent.aget_state(config)
    _assert_both_evals_intact(eval_results(resumed), executed, state.values.get("tool_call_history") or {})


@pytest.mark.parametrize(
    ("current", "update", "expected"),
    [
        # A WHOLE-VALUE update replaces, exactly as LangGraph's LastValue always did.
        # This is the model-boundary writer's path and it must not change.
        ({}, {"t": ["a"]}, {"t": ["a"]}),
        (None, {"t": ["a"]}, {"t": ["a"]}),
        ({"t": ["a"]}, {"t": ["a", "b"]}, {"t": ["a", "b"]}),
        # Including when it shrinks a key — a trim must never be undone.
        ({"t": ["a", "b", "c"]}, {"t": ["b", "c"]}, {"t": ["b", "c"]}),
        # Whole-value replace drops keys the update omits, like LastValue.
        ({"t": ["a"], "u": ["x"]}, {"t": ["a", "b"]}, {"t": ["a", "b"]}),
    ],
)
def test_merge_tool_call_history_whole_value_replaces(current, update, expected):
    assert merge_tool_call_history(current, update) == expected


def test_merge_tool_call_history_delta_appends_and_caps():
    """A delta appends the writer's own hashes, then applies its window."""
    current = {"eval:probe": ["h0", "h1"], "other": ["z"]}

    merged = merge_tool_call_history(current, history_delta({"eval:probe": ["h2"]}, {"eval:probe": 10}))

    assert merged["eval:probe"] == ["h0", "h1", "h2"]
    # Keys the delta does not mention are untouched — unlike a whole-value write.
    assert merged["other"] == ["z"]


def test_merge_tool_call_history_delta_cap_is_applied_once():
    full = {"k": [f"h{i}" for i in range(10)]}

    capped = merge_tool_call_history(full, history_delta({"k": ["x"]}, {"k": 10}))
    uncapped = merge_tool_call_history(full, history_delta({"k": ["x"]}, {"k": None}))

    assert capped["k"] == [*[f"h{i}" for i in range(1, 10)], "x"]
    # A blocked call is left uncapped so repeat counts keep escalating and
    # ``force_stop_after`` can fire — see RepeatedToolCallMiddleware.evaluate.
    assert len(uncapped["k"]) == 11


def test_merge_tool_call_history_deltas_commute():
    """Two eval tasks in one superstep merge the same way in either order."""
    seed = {"eval:probe": ["s1"]}
    a = history_delta({"eval:probe": ["a1"]}, {"eval:probe": 10})
    b = history_delta({"eval:probe": ["b1"]}, {"eval:probe": 10})

    a_then_b = merge_tool_call_history(merge_tool_call_history(seed, a), b)
    b_then_a = merge_tool_call_history(merge_tool_call_history(seed, b), a)

    assert sorted(a_then_b["eval:probe"]) == sorted(b_then_a["eval:probe"])
    assert set(a_then_b["eval:probe"]) == {"s1", "a1", "b1"}


def test_merge_tool_call_history_keeps_the_same_hash_from_both_evals():
    """Two evals making the identical call each get counted, not collapsed.

    An earlier shape-inference reducer absorbed the duplicate into the common
    prefix and under-counted the repeat.
    """
    merged = merge_tool_call_history({"eval:probe": ["s1"]}, history_delta({"eval:probe": ["x"]}, {"eval:probe": 10}))
    merged = merge_tool_call_history(merged, history_delta({"eval:probe": ["x"]}, {"eval:probe": 10}))

    assert merged["eval:probe"] == ["s1", "x", "x"]


def test_single_writer_history_stays_bounded_past_the_window():
    """The regression guard: drive real updates out of ``evaluate`` past the window.

    ``evaluate`` appends *then* trims, so a saturated update is an equal-length
    rotation (``old[1:] + [new]``). A reducer that tried to recognise a trim by
    comparing list shapes mistook that for two writers diverging and concatenated,
    growing the history by a full window every step and inflating repeat counts
    until legitimate calls were blocked.
    """
    middleware = RepeatedToolCallMiddleware(window_size=10, max_repeats=5)
    state: dict[str, list[str]] = {}

    for i in range(25):
        verdict = middleware.evaluate("task", {"i": i}, state.get("task", []))
        assert not verdict.blocked, f"distinct args must never be a loop (step {i})"
        state = merge_tool_call_history(state, {"task": verdict.history})

    assert len(state["task"]) == 10


def test_serialized_eval_excludes_across_event_loops():
    """The gate must serialize evals that never share an event loop.

    Two parallel ``task`` dispatches of the same sub-agent resolve one ``thread_id``
    (``{context_id}::{checkpoint_ns}``) but reach ``eval`` through
    ``LocalA2ARunnable.invoke`` → ``asyncio.run`` on ToolNode executor threads — a
    fresh loop each. An ``asyncio.Lock`` binds to the loop that first awaits it, so a
    gate keyed per loop serializes neither and leaves the #217 sandbox collision open.
    """
    import threading

    from agent_common.middleware.ptc_guard import serialized_eval

    holder_inside = threading.Event()
    saw_holder_inside: list[bool] = []

    async def _hold():
        async with serialized_eval("shared-thread"):
            holder_inside.set()
            await asyncio.sleep(0.3)
            holder_inside.clear()

    async def _contend():
        # Only start contending once the holder is demonstrably in the section.
        holder_inside.wait(2.0)
        async with serialized_eval("shared-thread"):
            # With real mutual exclusion we cannot get here until the holder left.
            saw_holder_inside.append(holder_inside.is_set())

    holder = threading.Thread(target=lambda: asyncio.run(_hold()))
    contender = threading.Thread(target=lambda: asyncio.run(_contend()))
    holder.start()
    contender.start()
    holder.join(5)
    contender.join(5)

    assert not holder.is_alive() and not contender.is_alive(), "gate deadlocked across loops"
    assert saw_holder_inside == [False], "the second eval entered while the first still held the gate"
