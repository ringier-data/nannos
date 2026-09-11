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


def _build_parallel_eval_agent(risk: float) -> tuple[Any, list[str]]:
    """An agent whose single model step emits two ``eval`` calls."""
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
    model = _ScriptedModel()
    model.responses = deque(
        [
            AIMessage(
                content="",
                id="ai-1",
                tool_calls=[
                    {"id": "call-a", "name": "eval", "args": {"code": _PROGRAM.format(tag="A", delay=_SLOW_MS)}},
                    {"id": "call-b", "name": "eval", "args": {"code": _PROGRAM.format(tag="B", delay=0)}},
                ],
            ),
            AIMessage(content="done", id="ai-final"),
        ]
    )
    agent = create_agent(model=model, tools=[], middleware=[loop_detection, middleware], checkpointer=InMemorySaver())
    return agent, executed


def _eval_results(result: dict) -> dict[str, str]:
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
    agent, executed = _build_parallel_eval_agent(risk=0.1)
    config = {"configurable": {"thread_id": "ptc-parallel-low"}}

    result = await asyncio.wait_for(agent.ainvoke({"messages": [HumanMessage("go")]}, config), 60)

    state = await agent.aget_state(config)
    _assert_both_evals_intact(_eval_results(result), executed, state.values.get("tool_call_history") or {})


async def test_two_evals_in_one_step_each_get_their_own_approval():
    """High-risk inner calls: one interrupt per eval, and each honours its own decision.

    Before the fix both evals recorded onto one thread-keyed collector, so the
    approvals were batched into whichever ``interrupt()`` fired first and the other
    eval's call was never asked about.
    """
    agent, executed = _build_parallel_eval_agent(risk=0.95)
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
    _assert_both_evals_intact(_eval_results(resumed), executed, state.values.get("tool_call_history") or {})


@pytest.mark.parametrize(
    ("current", "update", "expected"),
    [
        # First write / nothing held yet.
        ({}, {"t": ["a"]}, {"t": ["a"]}),
        (None, {"t": ["a"]}, {"t": ["a"]}),
        # A single writer appending: the update simply wins.
        ({"t": ["a"]}, {"t": ["a", "b"]}, {"t": ["a", "b"]}),
        # Unchanged key passes through.
        ({"t": ["a"]}, {"t": ["a"]}, {"t": ["a"]}),
        # The loop guard's sliding-window trim must NOT be undone: the update is a
        # suffix of what we hold, so it replaces rather than merges.
        ({"t": ["a", "b", "c"]}, {"t": ["b", "c"]}, {"t": ["b", "c"]}),
        # Two eval tasks extending the same seed in one superstep: keep both tails.
        ({"t": ["seed", "a"]}, {"t": ["seed", "b"]}, {"t": ["seed", "a", "b"]}),
        # Keys only one writer touched are preserved.
        ({"t": ["a"], "u": ["x"]}, {"t": ["a", "b"]}, {"t": ["a", "b"], "u": ["x"]}),
    ],
)
def test_merge_tool_call_history(current, update, expected):
    assert merge_tool_call_history(current, update) == expected


def test_merge_tool_call_history_is_associative_over_two_eval_tasks():
    """Applying two concurrent extensions in either order yields the same history."""
    seed = {"eval:probe": ["s1"]}
    a = {"eval:probe": ["s1", "a1"]}
    b = {"eval:probe": ["s1", "b1"]}

    a_then_b = merge_tool_call_history(merge_tool_call_history(seed, a), b)
    b_then_a = merge_tool_call_history(merge_tool_call_history(seed, b), a)

    assert sorted(a_then_b["eval:probe"]) == sorted(b_then_a["eval:probe"])
    assert set(a_then_b["eval:probe"]) == {"s1", "a1", "b1"}
