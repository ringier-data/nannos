"""End-to-end PTC bridge tests: guard behaviour through a real ``eval`` call.

These exercise ``CodeInterpreterMiddleware``'s programmatic-tool-calling bridge
with a risk-guarded wrapped tool, confirming that:

* a low-risk call executes inside ``eval`` and returns its result,
* a high-risk call *returns* an approval-required payload out of ``eval`` without
  executing the underlying tool (redirecting the model to the normal path), and
* the tolerant ``CodeInterpreterMiddleware`` subclass survives dict-form tools
  injected into ``request.tools`` (as the orchestrator does at runtime).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Optional

import pytest

from langchain.agents.factory import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langchain_quickjs import CodeInterpreterMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent_common.core.graph_utils import _PTCToleranceCodeInterpreterMiddleware
from agent_common.middleware.ptc_guard import wrap_tool_for_ptc


class _Args(BaseModel):
    path: str = Field(description="path")


class _ScriptedModel(BaseChatModel):
    """Fake model that replays scripted AIMessages and accepts bind_tools."""

    responses: deque = deque()

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> "_ScriptedModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.responses.popleft())])


def _executed_recorder() -> tuple[StructuredTool, list[str]]:
    executed: list[str] = []

    async def _safe_read(path: str) -> str:
        executed.append(path)
        return f"contents-of:{path}"

    tool = StructuredTool.from_function(
        coroutine=_safe_read,
        name="safe_read",
        description="read a path",
        args_schema=_Args,
    )
    return tool, executed


def _scorer(score: float):
    async def _fn(tool_name, args, *, tool=None, cache=None, server_slug="_self"):
        return score, None

    return _fn


def _build_agent(score: float, code: str):
    inner, executed = _executed_recorder()
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(score), default_risk_threshold=0.8)
    ci = CodeInterpreterMiddleware(ptc=[wrapped])
    model = _ScriptedModel()
    model.responses = deque(
        [
            AIMessage(
                content="",
                id="ai-1",
                tool_calls=[{"id": "c1", "name": "eval", "args": {"code": code}}],
            ),
            AIMessage(content="done", id="ai-2"),
        ]
    )
    agent = create_agent(model=model, tools=[], middleware=[ci])
    return agent, executed


def _last_eval_message(result: dict) -> str:
    for msg in reversed(result["messages"]):
        if getattr(msg, "name", None) == "eval":
            return str(msg.content)
    return ""


async def test_low_risk_tool_executes_through_eval():
    agent, executed = _build_agent(0.1, "const r = await tools.safeRead({path: '/data/a'}); r")
    result = await agent.ainvoke({"messages": [HumanMessage("go")]})
    assert executed == ["/data/a"]
    assert "contents-of:/data/a" in _last_eval_message(result)


async def test_high_risk_tool_blocked_in_eval_and_not_executed():
    agent, executed = _build_agent(0.95, "const r = await tools.safeRead({path: '/etc/passwd'}); JSON.stringify(r)")
    result = await agent.ainvoke({"messages": [HumanMessage("go")]})
    # The underlying tool must NOT run.
    assert executed == []
    # The eval result must surface the approval-required guidance.
    content = _last_eval_message(result).lower()
    assert "human_approval_required" in content
    assert "approval" in content or "directly" in content


class _DictToolInjector(AgentMiddleware):
    """Inject a provider-native dict tool into ``request.tools`` (as orchestrator does)."""

    _DICT_TOOL = {"type": "function", "function": {"name": "native_schema_tool"}}

    def wrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        tools = list(getattr(request, "tools", []) or []) + [self._DICT_TOOL]
        return handler(request.override(tools=tools))

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        tools = list(getattr(request, "tools", []) or []) + [self._DICT_TOOL]
        return await handler(request.override(tools=tools))


async def test_tolerant_middleware_survives_dict_tools_in_request():
    """The orchestrator injects dict-form tools; the tolerant subclass must not crash.

    A plain ``CodeInterpreterMiddleware`` would raise
    ``AttributeError: 'dict' object has no attribute 'name'`` in
    ``filter_tools_for_ptc``. The tolerant subclass strips dict tools before
    building the PTC prompt while still executing the low-risk wrapped tool.
    """
    inner, executed = _executed_recorder()
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.1), default_risk_threshold=0.8)
    ci = _PTCToleranceCodeInterpreterMiddleware(static_ptc_tools=[wrapped])
    model = _ScriptedModel()
    model.responses = deque(
        [
            AIMessage(
                content="",
                id="ai-1",
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "eval",
                        "args": {"code": "const r = await tools.safeRead({path: '/data/a'}); r"},
                    }
                ],
            ),
            AIMessage(content="done", id="ai-2"),
        ]
    )
    # _DictToolInjector sits OUTER so the dict tool is present when the
    # code-interpreter middleware inspects request.tools.
    agent = create_agent(model=model, tools=[], middleware=[_DictToolInjector(), ci])
    result = await agent.ainvoke({"messages": [HumanMessage("go")]})
    assert executed == ["/data/a"]
    assert "contents-of:/data/a" in _last_eval_message(result)


def _bound_tool_names(tools: list) -> list[str]:
    names: list[str] = []
    for tool in tools:
        if isinstance(tool, dict):
            fn = tool.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                names.append(fn["name"])
            else:
                names.append(tool.get("name"))
        else:
            names.append(getattr(tool, "name", None))
    return [n for n in names if n]


async def test_ptc_exposed_tools_hidden_from_model_binding():
    """PTC-exposed tools must NOT appear in the model's bound tool list.

    The wrapped tool is reachable only via ``eval``; the bound tools seen by the
    model are limited to ``eval`` plus never-exposed tools (dispatch /
    response-schema), so the LangSmith trace stays minimal.
    """
    inner, _ = _executed_recorder()  # name safe_read

    async def _noop() -> str:
        return "ok"

    # ``write_todos`` is in _PTC_EXCLUDED_TOOL_NAMES → never exposed, stays visible.
    excluded_tool = StructuredTool.from_function(coroutine=_noop, name="write_todos", description="plan")
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=_scorer(0.1), default_risk_threshold=0.8)
    ci = _PTCToleranceCodeInterpreterMiddleware(static_ptc_tools=[wrapped])

    bound: list[list[str]] = []

    class _CapturingModel(_ScriptedModel):
        def bind_tools(self, tools: list, **kwargs: Any) -> "_CapturingModel":
            bound.append(_bound_tool_names(tools))
            return self

    model = _CapturingModel()
    model.responses = deque([AIMessage(content="done", id="ai-1")])
    # ``inner`` is also a normal bound tool — it must be stripped because it is
    # exposed via PTC; ``write_todos`` and ``eval`` must remain.
    agent = create_agent(model=model, tools=[inner, excluded_tool], middleware=[ci])
    await agent.ainvoke({"messages": [HumanMessage("go")]})

    assert bound, "model was never called"
    last = bound[-1]
    assert "safe_read" not in last  # PTC-exposed → hidden from the model
    assert "eval" in last  # the REPL tool stays visible
    assert "write_todos" in last  # excluded from PTC → stays visible


async def test_broaden_exposure_false_keeps_request_tools_visible():
    """With ``broaden_exposure=False`` (orchestrator), ``request.tools`` are NOT
    harvested into PTC — only the static fs baseline is exposed/hidden.

    The orchestrator must keep its dispatchable tools bound to the model; it
    delegates via ``task`` and must not pull its registry into the PTC prompt.
    """
    inner, _ = _executed_recorder()  # name safe_read

    async def _noop() -> str:
        return "ok"

    baseline = StructuredTool.from_function(coroutine=_noop, name="ls", description="list")
    wrapped_baseline = wrap_tool_for_ptc(baseline, risk_scorer=_scorer(0.1), default_risk_threshold=0.8)
    ci = _PTCToleranceCodeInterpreterMiddleware(static_ptc_tools=[wrapped_baseline], broaden_exposure=False)

    bound: list[list[str]] = []

    class _CapturingModel(_ScriptedModel):
        def bind_tools(self, tools: list, **kwargs: Any) -> "_CapturingModel":
            bound.append(_bound_tool_names(tools))
            return self

    model = _CapturingModel()
    model.responses = deque([AIMessage(content="done", id="ai-1")])
    agent = create_agent(model=model, tools=[inner], middleware=[ci])
    await agent.ainvoke({"messages": [HumanMessage("go")]})

    assert bound, "model was never called"
    last = bound[-1]
    assert "ls" not in last  # static fs baseline → exposed via PTC → hidden
    assert "safe_read" in last  # request.tools NOT harvested → stays bound
    assert "eval" in last  # the REPL tool stays visible


# ---------------------------------------------------------------------------
# HITL-from-eval: interrupt fires from the main loop and re-runs eval on resume
# ---------------------------------------------------------------------------


def _build_hitl_agent(score: float, code: str) -> tuple[Any, list[str]]:
    """Agent using the tolerant middleware + checkpointer so ``interrupt`` works."""
    inner, executed = _executed_recorder()
    scorer = _scorer(score)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=scorer, default_risk_threshold=0.8)
    ci = _PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[wrapped], risk_scorer=scorer, default_risk_threshold=0.8
    )
    model = _ScriptedModel()
    model.responses = deque(
        [
            AIMessage(
                content="",
                id="ai-1",
                tool_calls=[{"id": "c1", "name": "eval", "args": {"code": code}}],
            ),
            AIMessage(content="done", id="ai-2"),
        ]
    )
    agent = create_agent(model=model, tools=[], middleware=[ci], checkpointer=InMemorySaver())
    return agent, executed


async def test_high_risk_eval_call_interrupts_then_executes_on_approve():
    code = "const r = await tools.safeRead({path: '/etc/passwd'}); JSON.stringify(r)"
    agent, executed = _build_hitl_agent(0.95, code)
    config = {"configurable": {"thread_id": "t-approve"}}

    # First run blocks on the HITL interrupt without executing the tool.
    result = await agent.ainvoke({"messages": [HumanMessage("go")]}, config=config)
    assert executed == []
    assert "__interrupt__" in result

    # Approve and resume: eval re-runs and the tool executes for real once.
    resumed = await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)
    assert executed == ["/etc/passwd"]
    assert "contents-of:/etc/passwd" in _last_eval_message(resumed)
    assert "__interrupt__" not in resumed


async def test_high_risk_eval_call_rejected_returns_rejection_payload():
    code = "const r = await tools.safeRead({path: '/etc/passwd'}); JSON.stringify(r)"
    agent, executed = _build_hitl_agent(0.95, code)
    config = {"configurable": {"thread_id": "t-reject"}}

    result = await agent.ainvoke({"messages": [HumanMessage("go")]}, config=config)
    assert "__interrupt__" in result

    resumed = await agent.ainvoke(Command(resume={"decisions": [{"type": "reject"}]}), config=config)
    # The tool never executes on rejection.
    assert executed == []
    content = _last_eval_message(resumed).lower()
    assert "human_rejected" in content
    assert "__interrupt__" not in resumed


_TWO_HIGH_RISK_CALLS = (
    "const a = await tools.safeRead({path: '/etc/passwd'});"
    "const b = await tools.safeRead({path: '/etc/shadow'});"
    "JSON.stringify([a, b])"
)


async def test_multiple_pending_eval_calls_resume_with_matching_decisions():
    """Two high-risk inner calls in one eval → one interrupt with 2 action_requests.

    Post-migration the resume arrives with exactly one decision per pending call
    (the orchestrator's ``executor._build_interrupt_resume_map`` replicates the single
    blanket UI decision to the interrupt's action_request count and keys it by
    interrupt id). ``awrap_tool_call`` now applies them 1:1 — no replication/truncation.
    """
    agent, executed = _build_hitl_agent(0.95, _TWO_HIGH_RISK_CALLS)
    config = {"configurable": {"thread_id": "t-multi-approve"}}

    result = await agent.ainvoke({"messages": [HumanMessage("go")]}, config=config)
    assert executed == []
    assert "__interrupt__" in result
    assert len(result["__interrupt__"][0].value["action_requests"]) == 2

    resumed = await agent.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}, {"type": "approve"}]}), config=config
    )
    assert executed == ["/etc/passwd", "/etc/shadow"]
    assert "__interrupt__" not in resumed


async def test_per_call_decisions_applied_by_id_end_to_end():
    """Approve one call and reject the other, matched by call_id through the real eval.

    Decisions are deliberately keyed by id (as the client now sends them); only the
    approved path must execute, regardless of the concurrent re-run ordering.
    """
    agent, executed = _build_hitl_agent(0.95, _TWO_HIGH_RISK_CALLS)
    config = {"configurable": {"thread_id": "t-multi-by-id"}}

    result = await agent.ainvoke({"messages": [HumanMessage("go")]}, config=config)
    action_requests = result["__interrupt__"][0].value["action_requests"]
    call_id_by_path = {
        ar["args"]["path"]: ar["args"]["_call_id"] for ar in action_requests
    }

    decisions = [
        {"id": call_id_by_path["/etc/passwd"], "type": "approve"},
        {"id": call_id_by_path["/etc/shadow"], "type": "reject"},
    ]
    resumed = await agent.ainvoke(Command(resume={"decisions": decisions}), config=config)

    assert executed == ["/etc/passwd"]  # only the approved call ran
    assert "__interrupt__" not in resumed


async def test_decision_count_mismatch_raises():
    """A single decision for 2 pending eval calls now raises (strict 1:1 contract).

    The migration guarantees the resume is pre-replicated upstream, so a count
    mismatch here is a genuine bug rather than a stale-resume artefact — the
    tolerant middleware no longer silently replicates or truncates.
    """
    agent, _ = _build_hitl_agent(0.95, _TWO_HIGH_RISK_CALLS)
    config = {"configurable": {"thread_id": "t-multi-mismatch"}}

    await agent.ainvoke({"messages": [HumanMessage("go")]}, config=config)
    with pytest.raises(ValueError, match="does not match number of pending eval tool calls"):
        await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)


async def test_low_risk_eval_call_never_interrupts_with_tolerant_middleware():
    code = "const r = await tools.safeRead({path: '/data/a'}); r"
    agent, executed = _build_hitl_agent(0.1, code)
    config = {"configurable": {"thread_id": "t-low"}}

    result = await agent.ainvoke({"messages": [HumanMessage("go")]}, config=config)
    assert executed == ["/data/a"]
    assert "__interrupt__" not in result
    assert "contents-of:/data/a" in _last_eval_message(result)


# ---------------------------------------------------------------------------
# Production-shaped resume: the graph is REBUILT between interrupt and resume
# (mirroring DynamicLocalAgentRunnable, which rebuilds the LangGraph per
# invocation). The checkpointer + thread_id are shared. On a doc-compliant
# resume, the interrupted ``eval`` *tool node* replays with its checkpointed
# args; the approved inner call must execute for real and the eval ToolMessage
# must carry the real result (not an error). The model may then run once to
# summarize — that is the normal agent loop, not the bug. The bug is the eval
# replay producing an *error* result that the model is then asked to continue.
# ---------------------------------------------------------------------------


def _build_hitl_agent_with_saver(
    score: float,
    code: str,
    saver: InMemorySaver,
    model: BaseChatModel,
) -> tuple[Any, list[str]]:
    """Build a fresh graph wired to a *shared* checkpointer (production shape)."""
    inner, executed = _executed_recorder()
    scorer = _scorer(score)
    wrapped = wrap_tool_for_ptc(inner, risk_scorer=scorer, default_risk_threshold=0.8)
    ci = _PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[wrapped], risk_scorer=scorer, default_risk_threshold=0.8
    )
    agent = create_agent(model=model, tools=[], middleware=[ci], checkpointer=saver)
    return agent, executed


async def test_rebuilt_graph_resume_replays_eval_tool_node_not_model():
    """REGRESSION (production resume): rebuilding the graph between interrupt and
    resume must still honor the approved ``eval`` call — execute the inner tool
    for real and surface its result. This is the path
    ``DynamicLocalAgentRunnable`` takes; the persistent-graph HITL tests above
    cannot catch a rebuild regression.
    """
    code = "const r = await tools.safeRead({path: '/etc/passwd'}); JSON.stringify(r)"
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t-rebuild"}}

    # First graph: emits the eval call, then would say "done".
    first_model = _ScriptedModel()
    first_model.responses = deque(
        [
            AIMessage(
                content="",
                id="ai-1",
                tool_calls=[{"id": "c1", "name": "eval", "args": {"code": code}}],
            ),
            AIMessage(content="done", id="ai-2"),
        ]
    )
    agent1, _ = _build_hitl_agent_with_saver(0.95, code, saver, first_model)
    result = await agent1.ainvoke({"messages": [HumanMessage("go")]}, config=config)
    assert "__interrupt__" in result

    # Second graph: brand-new instance sharing the same checkpointer. After the
    # approved eval replays, this model runs once to summarize — allowed.
    second_model = _ScriptedModel()
    second_model.responses = deque([AIMessage(content="done", id="ai-2b")])
    agent2, executed2 = _build_hitl_agent_with_saver(0.95, code, saver, second_model)

    resumed = await agent2.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config=config)

    eval_msg = _last_eval_message(resumed)
    assert executed2 == ["/etc/passwd"], (
        f"approved eval call did not execute on rebuilt resume; eval said: {eval_msg!r}"
    )
    assert "contents-of:/etc/passwd" in eval_msg, f"eval result lost on resume: {eval_msg!r}"
    assert "__interrupt__" not in resumed


async def test_inner_interrupted_nested_resume_through_resuming_parent():
    """REGRESSION (production two-level dispatch + HITL resume).

    Mirrors the real orchestrator → sub-agent path end-to-end:

    * The sub-agent (inner) graph runs from *inside* the orchestrator's (outer)
      Pregel node, sharing a single checkpointer (production has one PostgreSQL
      saver for both, on distinct ``thread_id``s).
    * On the first dispatch the inner PTC interrupt is suppressed + persisted and
      re-raised so it propagates to the outer graph.
    * On resume the outer graph is itself replayed under ``Command(resume)``; its
      dispatch node detects the inner's pending interrupt, surfaces it via
      ``interrupt()`` (which returns the decisions on this resume), and forwards a
      ``Command(resume=...)`` to the inner graph — exactly orchestrator PATH 1.

    The bug: while the *outer* is resuming, langgraph leaks the parent's
    ``checkpoint_map`` (its namespace→checkpoint-id resolution) into the inner
    config via ``var_child_runnable_config``. The inner Pregel then resolves a
    *parent* checkpoint id for its own thread instead of loading the LATEST
    checkpoint of its own thread, silently discards the ``Command(resume)``, and
    re-runs the model from an empty state — the approved eval is lost and the
    sub-agent answers as if freshly invoked. The leak is invisible on the first
    dispatch (no resume → no stale map), so only the resume path exposes it.

    ``denest_parent_pregel_context()`` must strip ``checkpoint_id`` /
    ``checkpoint_map`` (not just ``__pregel_task_id``) so the inner replays its
    own eval tool node and executes the approved call. Resuming the inner graph
    *directly* (outside a resuming parent) does NOT reproduce this — the failure
    requires the resuming-parent ambient context.
    """
    from typing import TypedDict

    from langgraph.errors import GraphInterrupt
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt as _interrupt

    from agent_common.core.graph_utils import denest_parent_pregel_context

    code = "const r = await tools.safeRead({path: '/etc/passwd'}); JSON.stringify(r)"
    # ONE shared checkpointer for outer + inner (production = one PostgreSQL saver).
    saver = InMemorySaver()
    inner_cfg = {"configurable": {"thread_id": "inner-nested", "checkpoint_ns": ""}}

    def _inner_emitting_eval() -> Any:
        m = _ScriptedModel()
        m.responses = deque(
            [
                AIMessage(content="", id="ai-1", tool_calls=[{"id": "c1", "name": "eval", "args": {"code": code}}]),
                AIMessage(content="done", id="ai-2"),
            ]
        )
        return _build_hitl_agent_with_saver(0.95, code, saver, m)

    def _inner_summarizing() -> Any:
        m = _ScriptedModel()
        m.responses = deque([AIMessage(content="done", id="ai-2b")])
        return _build_hitl_agent_with_saver(0.95, code, saver, m)

    executed_ref: dict[str, list[str]] = {}

    class _OuterState(TypedDict):
        done: bool

    async def _dispatch(state: _OuterState) -> dict:
        # PATH 1: a pending interrupt in the inner checkpoint means we are resuming.
        ckpt = await saver.aget_tuple(inner_cfg)
        pending = None
        if ckpt and ckpt.pending_writes:
            for _tid, channel, value in ckpt.pending_writes:
                if channel == "__interrupt__":
                    pending = value[0].value if isinstance(value, (list, tuple)) else value
        if pending is not None:
            decisions = _interrupt(pending)  # raises first time; returns decisions on resume
            inner, executed = _inner_summarizing()
            agent_input: Any = Command(resume=decisions)
        else:
            inner, executed = _inner_emitting_eval()
            agent_input = {"messages": [HumanMessage("go")]}
        executed_ref["x"] = executed
        # De-nesting wraps the whole inner invocation (the contextvar must stay set).
        with denest_parent_pregel_context():
            await inner.ainvoke(agent_input, config=inner_cfg)
        st = await inner.aget_state(inner_cfg)
        if st.interrupts:
            raise GraphInterrupt(st.interrupts)
        return {"done": True}

    outer = StateGraph(_OuterState)
    outer.add_node("dispatch", _dispatch)
    outer.add_edge(START, "dispatch")
    outer.add_edge("dispatch", END)
    outer_graph = outer.compile(checkpointer=saver)
    outer_cfg = {"configurable": {"thread_id": "outer-nested"}}

    res1 = await outer_graph.ainvoke({"done": False}, config=outer_cfg)
    assert "__interrupt__" in res1, "inner PTC interrupt did not propagate to outer graph"

    # Resume the OUTER graph (production path): its dispatch node forwards a
    # Command(resume) into the inner graph from within the resuming parent.
    await outer_graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config=outer_cfg)

    final = await _inner_summarizing()[0].aget_state(inner_cfg)
    eval_msg = ""
    for msg in reversed(final.values.get("messages", [])):
        if getattr(msg, "name", None) == "eval":
            eval_msg = str(msg.content)
            break
    executed2 = executed_ref["x"]
    assert executed2 == ["/etc/passwd"], (
        "approved eval call did not execute when resumed through a resuming parent; "
        f"executed={executed2!r}, eval said: {eval_msg!r}"
    )
    assert "contents-of:/etc/passwd" in eval_msg, f"eval result lost on resume: {eval_msg!r}"
