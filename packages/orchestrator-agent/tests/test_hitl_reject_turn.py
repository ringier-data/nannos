"""Human-in-the-loop rejection, against the orchestrator's real graph.

The guarantee under test is narrow and important: when a user rejects a guarded
tool call, that tool must not run — and the other calls in the same batch must
behave correctly around it.

Why this file was rewritten
---------------------------
It used to build its own graph. ``build_hitl_graph`` assembled a ``StateGraph``
with a docstring conceding it *"replicates the topology from
langchain.agents.factory"*, including its own router::

    pending = [c for c in last_ai.tool_calls if c["id"] not in tool_msg_ids]
    if pending:
        return "tools"

That router **is** the protection being asserted. A rejected tool call is not
removed from the AIMessage — langchain's ``_process_decision`` returns the tool
call, not ``None``, and answers it with a synthetic error ToolMessage instead —
so "the tool did not run" depends entirely on a router skipping calls that
already have a result. A test that supplies that router proves its own router
works, and would stay green while production executed rejected tools. Which is
the bug the module was written to catch.

The rule, worth stating once: **a test must not replace the system whose
behaviour it asserts.** See ``AGENTS.md`` → *Test rigor*.

So these tests compile the production graph through ``scripted_graph()``: real
``GraphFactory``, real middleware stack, real ``ConditionalHumanInTheLoopMiddleware``,
real router. Only the model is scripted.

Two consequences of using the real middleware
---------------------------------------------
1. **Tool names decide what is guarded.** The orchestrator scores risk with
   ``score_tool_risk``, which falls back to ``_deterministic_fallback(tool_name)``
   when ``cache is None`` — always, in tests, since there is no
   ``tool_risk_scores`` table. So guarding is name-based and needs no LLM.
   ``delete_records`` scores 0.95 and is guarded; ``get_records`` scores 0.3 and
   is not. The old ``dangerous_tool``/``safe_tool`` names both score **0.7** —
   under the 0.8 threshold, so neither would have been guarded here at all.
   Static ``interrupt_on`` config, which those names relied on, is a different
   path; ``TestLangchainHITLContract`` below still covers it.
2. **One interrupt covers the whole batch.** Not one per call. The number of
   ``action_requests`` is the number of decisions a resume must supply, and a
   mismatch raises ``ValueError`` rather than failing quietly.

Runs in the normal suite: no gateway, no credentials, no model.
"""

from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt
from typing_extensions import Annotated, TypedDict

from langchain.agents.factory import create_agent
from langchain.agents.middleware.human_in_the_loop import (
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
)
from tests.support.extraction import interrupted_tools, tool_calls
from tests.support.graph_harness import (
    final_response,
    parallel_calls,
    runtime_context,
    scripted_graph,
    tool_call,
    turn_config,
    user_turn,
)
from tests.support.scripted_model import ScriptedChatModel

executed: list[str] = []
"""Every tool invocation, in order. The primary evidence of non-execution: an
absent entry means the tool never ran, which no amount of state inspection can
establish as directly."""


# ---------------------------------------------------------------------------
# Tools, named for the risk they score
# ---------------------------------------------------------------------------


@tool
def delete_records(target: str) -> str:
    """Delete records permanently."""
    executed.append(f"delete_records:{target}")
    return f"deleted {target}"


@tool
def remove_cache(key: str) -> str:
    """Remove a cache entry."""
    executed.append(f"remove_cache:{key}")
    return f"removed {key}"


@tool
def get_records(query: str) -> str:
    """Look up records."""
    executed.append(f"get_records:{query}")
    return f"found {query}"


GUARDED = ("delete_records", "remove_cache")
"""Both score 0.95 — above the 0.8 threshold."""

TOOL_REGISTRY = {t.name: t for t in (delete_records, remove_cache, get_records)}


# ---------------------------------------------------------------------------
# Driving the real graph
# ---------------------------------------------------------------------------


def _real_turn(*responses: AIMessage):
    """A production graph plus the runtime context that registers the tools.

    ``runtime_context(**overrides)`` forwards ``tool_registry`` straight onto
    ``GraphRuntimeContext``, so registering real tools needs no harness change.
    """
    graph = scripted_graph(ScriptedChatModel(responses=list(responses)))
    return graph, runtime_context(tool_registry=TOOL_REGISTRY)


async def _resume(graph, context, config: dict, *decisions: dict) -> dict:
    """Answer the pending interrupt with one decision per guarded call."""
    return await graph.ainvoke(Command(resume={"decisions": list(decisions)}), config=config, context=context)


def _tool_message_for(state: dict, name: str) -> ToolMessage | None:
    for message in state.get("messages") or []:
        if isinstance(message, ToolMessage) and message.name == name:
            return message
    return None


class TestRejectAgainstTheRealGraph:
    """The orchestrator's own graph, middleware stack and router."""

    @pytest.fixture(autouse=True)
    def _clear_log(self):
        executed.clear()
        yield
        executed.clear()

    async def test_the_risk_scorer_decides_what_is_guarded(self):
        """Guarding is name-based here, and that is load-bearing for this file.

        If this fails, either the score table or the threshold moved, and every
        other test below is guarding a different set than it claims.
        """
        graph, context = _real_turn(
            parallel_calls(
                ("delete_records", {"target": "prod"}),
                ("remove_cache", {"key": "k1"}),
                ("get_records", {"query": "q"}),
            ),
            final_response(),
        )
        state = await graph.ainvoke(user_turn("do three things"), config=turn_config("hitl-scoring"), context=context)

        assert interrupted_tools(state) == list(GUARDED), (
            "expected exactly the two 0.95-scoring tools to be guarded; get_records scores 0.3"
        )

    async def test_nothing_executes_before_the_human_answers(self):
        """The interrupt fires in ``after_model``, ahead of the tool node.

        Including the *auto-approved* call in the same batch — it is held, not
        run early. A design that ran it first would leak a side effect from a
        turn the user might still cancel.
        """
        graph, context = _real_turn(
            parallel_calls(("delete_records", {"target": "prod"}), ("get_records", {"query": "q"})),
            final_response(),
        )
        config = turn_config("hitl-nothing")
        state = await graph.ainvoke(user_turn("delete and look up"), config=config, context=context)

        assert interrupted_tools(state) == ["delete_records"]
        assert executed == [], f"nothing may run before the human answers, but: {executed}"

    async def test_reject_prevents_execution_and_tells_the_model_why(self):
        """The whole point. The tool must not run, and the model must learn why —
        an error with no explanation invites an immediate retry."""
        graph, context = _real_turn(tool_call("delete_records", {"target": "prod"}), final_response("Understood."))
        config = turn_config("hitl-reject")

        await graph.ainvoke(user_turn("delete the prod records"), config=config, context=context)
        final = await _resume(graph, context, config, {"type": "reject", "message": "Not production."})

        assert executed == [], f"a rejected tool must not run, but: {executed}"

        message = _tool_message_for(final, "delete_records")
        assert message is not None, "the rejected call was never answered"
        assert message.status == "error"
        assert "Not production." in message.content

        rejected = [c for c in tool_calls(final) if c.rejected]
        assert [c.name for c in rejected] == ["delete_records"]

    async def test_partial_approval_executes_exactly_the_approved_subset(self):
        """Four calls, one auto-approved and three guarded, two of three allowed.

        The case worth having: the rejected call is *not* removed from the
        AIMessage, so "it did not run" can only be established from the
        execution log and its error result — never from the call's absence.
        """
        graph, context = _real_turn(
            parallel_calls(
                ("get_records", {"query": "q3"}),
                ("delete_records", {"target": "stale"}),
                ("remove_cache", {"key": "k1"}),
                ("delete_records", {"target": "prod"}),
            ),
            final_response("Two of three done; the prod delete was declined."),
        )
        config = turn_config("hitl-partial")

        state = await graph.ainvoke(user_turn("clean up"), config=config, context=context)
        assert interrupted_tools(state) == ["delete_records", "remove_cache", "delete_records"]

        final = await _resume(
            graph,
            context,
            config,
            {"type": "approve"},
            {"type": "approve"},
            {"type": "reject", "message": "Not production."},
        )

        # The *set*, not the sequence: the tool node's execution order is not
        # stable across runs, and pinning it would make this fail on scheduling
        # rather than on behaviour.
        assert sorted(executed) == ["delete_records:stale", "get_records:q3", "remove_cache:k1"], (
            f"exactly the auto-approved and approved calls should run, got: {executed}"
        )
        assert not any(entry.endswith(":prod") for entry in executed), "the declined call ran anyway"

        by_id = {c.id: c for c in tool_calls(final)}
        assert by_id["tc-3"].rejected, "the declined call should carry an error result"
        assert not by_id["tc-1"].rejected and not by_id["tc-0"].rejected

    async def test_one_decision_per_guarded_call_or_it_raises(self):
        """The count contract, and a real bug: a client that sends one decision
        for a batch of N must fail loudly rather than have it applied to one and
        the rest silently approved."""
        graph, context = _real_turn(
            parallel_calls(("delete_records", {"target": "a"}), ("remove_cache", {"key": "b"})),
            final_response(),
        )
        config = turn_config("hitl-count")

        await graph.ainvoke(user_turn("two things"), config=config, context=context)

        with pytest.raises(ValueError, match="Number of human decisions"):
            await _resume(graph, context, config, {"type": "reject", "message": "No."})

        assert executed == [], "a malformed resume must not execute anything"

    async def test_delegation_is_never_guarded(self):
        """``task`` scores 0.7 but is auto-approved by an explicit branch —
        *"sub-agent owns its own HITL"*. This is why routing scenarios never trip
        HITL, and it means a delegation cannot be put behind an approval here."""
        graph, context = _real_turn(tool_call("get_records", {"query": "q"}), final_response())
        state = await graph.ainvoke(user_turn("look it up"), config=turn_config("hitl-task"), context=context)

        assert interrupted_tools(state) == []
        assert executed == ["get_records:q"]

    async def test_gemini_shaped_message_still_interrupts_and_rejects(self):
        """Gemini emits ``content=[]`` with the call in ``additional_kwargs`` and
        client-generated uuid ids. The guard must key off the tool call, not the
        message shape — this shape is what the original bug report came in on."""
        call_id = str(uuid.uuid4())
        gemini_shaped = AIMessage(
            content=[],
            additional_kwargs={"function_call": {"name": "delete_records", "arguments": '{"target": "prod"}'}},
            tool_calls=[
                ToolCall(type="tool_call", name="delete_records", args={"target": "prod"}, id=call_id),
            ],
        )
        graph, context = _real_turn(gemini_shaped, final_response("Understood."))
        config = turn_config("hitl-gemini")

        state = await graph.ainvoke(user_turn("delete prod"), config=config, context=context)
        assert interrupted_tools(state) == ["delete_records"]

        final = await _resume(graph, context, config, {"type": "reject", "message": "No."})

        assert executed == []
        assert _tool_message_for(final, "delete_records").status == "error"

    async def test_approve_then_reject_across_turns(self):
        """A decision must not leak forward. Turn 1 approves, turn 2 rejects, on
        one thread — so turn 2 reads turn 1's checkpoint."""
        graph, context = _real_turn(
            tool_call("delete_records", {"target": "stale"}, call_id="t1"),
            final_response("Deleted."),
            tool_call("delete_records", {"target": "prod"}, call_id="t2"),
            final_response("Left alone."),
        )
        config = turn_config("hitl-multiturn")

        await graph.ainvoke(user_turn("delete the stale records"), config=config, context=context)
        await _resume(graph, context, config, {"type": "approve"})
        assert executed == ["delete_records:stale"]

        state = await graph.ainvoke(user_turn("now delete prod"), config=config, context=context)
        assert interrupted_tools(state) == ["delete_records"], "turn 2 must interrupt on its own merits"

        await _resume(graph, context, config, {"type": "reject", "message": "No."})
        assert executed == ["delete_records:stale"], f"turn 1's approval leaked into turn 2: {executed}"


# ---------------------------------------------------------------------------
# Kept below: the langchain contract, and the two-level proxy gap
# ---------------------------------------------------------------------------


class State(TypedDict):
    messages: Annotated[list, add_messages]


tool_execution_log: list[str] = []
"""Separate log for the classes below, which use statically-guarded tools rather
than risk-scored ones."""


@tool
def dangerous_tool(action: str) -> str:
    """A tool guarded by static ``interrupt_on`` config, not by risk score."""
    tool_execution_log.append(f"dangerous_tool:{action}")
    return f"executed: {action}"


@tool
def safe_tool(query: str) -> str:
    """Not in the static config, so auto-approved."""
    tool_execution_log.append(f"safe_tool:{query}")
    return f"result: {query}"


class TestLangchainHITLContract:
    """A pin on the dependency, deliberately kept after the rewrite above.

    These use langchain's own ``create_agent`` and ``HumanInTheLoopMiddleware``
    with **static** ``interrupt_on`` config — not the orchestrator's graph and
    not risk scoring. So they answer a narrower question than the class above:
    does *langchain's* router still skip answered tool calls?

    That is worth keeping precisely because the class above cannot answer it. If
    both break, these say the regression is upstream rather than ours, which is
    the difference between reading a changelog and bisecting our middleware. If
    only the class above breaks, it is ours.

    Labelled as a pin so nobody mistakes it for end-to-end coverage of the
    orchestrator.
    """

    @pytest.fixture(autouse=True)
    def clear_log(self):
        tool_execution_log.clear()
        yield
        tool_execution_log.clear()

    @pytest.mark.asyncio
    async def test_reject_single_tool_call_with_factory_graph(self):
        """Reject a single HITL-guarded tool call using create_agent factory graph."""
        checkpointer = MemorySaver()

        # Scripted: first call → tool call, second call (after reject) → text response
        model = ScriptedChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "destroy"}, id="tc-f1"),
                    ],
                ),
                AIMessage(content="OK, I won't do that."),
            ]
        )

        graph = create_agent(
            model,
            tools=[dangerous_tool, safe_tool],
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on={"dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
                ),
            ],
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # Run — should interrupt at HITL
        async for _ in graph.astream(
            {"messages": [HumanMessage(content="destroy things")]}, config, stream_mode="updates"
        ):
            pass

        state = await graph.aget_state(config)
        assert state.next, "Should be interrupted"
        assert not tool_execution_log, "Tool should NOT have executed before approval"

        # Resume with REJECT
        reject_cmd = Command(resume={"decisions": [{"type": "reject", "message": "No!"}]})
        async for _ in graph.astream(reject_cmd, config, stream_mode="updates"):
            pass

        # Verify: dangerous_tool should NOT have executed
        assert not any("dangerous_tool" in entry for entry in tool_execution_log), (
            f"dangerous_tool should NOT execute after reject, but log shows: {tool_execution_log}"
        )

        # Verify final state has the model's acknowledgment
        final = await graph.aget_state(config)
        msgs = final.values["messages"]
        assert any("won't" in m.content for m in msgs if isinstance(m, AIMessage) and m.content), (
            f"Model should acknowledge rejection, messages: {[m.content for m in msgs if isinstance(m, AIMessage)]}"
        )

    @pytest.mark.asyncio
    async def test_reject_parallel_tool_calls_with_factory_graph(self):
        """Reject multiple parallel HITL-guarded tool calls (Gemini pattern)."""
        checkpointer = MemorySaver()

        # Scripted: first call → 3 parallel tool calls, second call → text
        model = ScriptedChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "a"}, id="tc-pa"),
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "b"}, id="tc-pb"),
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "c"}, id="tc-pc"),
                    ],
                ),
                AIMessage(content="All rejected, understood."),
            ]
        )

        graph = create_agent(
            model,
            tools=[dangerous_tool, safe_tool],
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on={"dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
                ),
            ],
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # Run — should interrupt
        async for _ in graph.astream(
            {"messages": [HumanMessage(content="do three things")]}, config, stream_mode="updates"
        ):
            pass

        state = await graph.aget_state(config)
        assert state.next, "Should be interrupted"

        # Verify 3 action_requests
        for task in state.tasks:
            if task.interrupts:
                interrupt_value = task.interrupts[0].value
                assert len(interrupt_value["action_requests"]) == 3

        # Resume with 3 reject decisions
        reject_cmd = Command(resume={"decisions": [
            {"type": "reject", "message": "No a!"},
            {"type": "reject", "message": "No b!"},
            {"type": "reject", "message": "No c!"},
        ]})
        async for _ in graph.astream(reject_cmd, config, stream_mode="updates"):
            pass

        # Verify: NO tools executed
        assert not tool_execution_log, (
            f"No tools should execute after reject, but log shows: {tool_execution_log}"
        )

    @pytest.mark.asyncio
    async def test_approve_turn1_reject_turn2_with_factory_graph(self):
        """Multi-turn: approve in turn 1, reject in turn 2 — using factory graph."""
        checkpointer = MemorySaver()

        # FakeModel sequence:
        # Turn 1: tool call → (approve → tool executes) → text response
        # Turn 2: tool call → (reject) → text response
        model = ScriptedChatModel(
            responses=[
                # Turn 1: tool call
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "create"}, id="tc-t1"),
                    ],
                ),
                # Turn 1: after tool executes, model responds
                AIMessage(content="Created successfully."),
                # Turn 2: new tool call
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "delete"}, id="tc-t2"),
                    ],
                ),
                # Turn 2: after reject, model acknowledges
                AIMessage(content="OK, won't delete."),
            ]
        )

        graph = create_agent(
            model,
            tools=[dangerous_tool, safe_tool],
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on={"dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
                ),
            ],
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # ═══ TURN 1: Approve ═══
        async for _ in graph.astream(
            {"messages": [HumanMessage(content="create something")]}, config, stream_mode="updates"
        ):
            pass
        state = await graph.aget_state(config)
        assert state.next, "Turn 1 should interrupt"

        approve_cmd = Command(resume={"decisions": [{"type": "approve"}]})
        async for _ in graph.astream(approve_cmd, config, stream_mode="updates"):
            pass

        # Verify turn 1: tool executed
        assert any("dangerous_tool:create" in entry for entry in tool_execution_log), (
            f"Turn 1 tool should have executed, log: {tool_execution_log}"
        )
        tool_execution_log.clear()

        # ═══ TURN 2: Reject ═══
        async for _ in graph.astream(
            {"messages": [HumanMessage(content="now delete it")]}, config, stream_mode="updates"
        ):
            pass
        state = await graph.aget_state(config)
        assert state.next, "Turn 2 should interrupt"

        # Verify interrupt is for turn 2's tool
        for task in state.tasks:
            if task.interrupts:
                ar = task.interrupts[0].value["action_requests"][0]
                assert ar["args"]["action"] == "delete", f"Should be turn 2's delete, got: {ar}"

        reject_cmd = Command(resume={"decisions": [{"type": "reject", "message": "Don't delete!"}]})
        async for _ in graph.astream(reject_cmd, config, stream_mode="updates"):
            pass

        # Verify turn 2: tool did NOT execute
        assert not any("dangerous_tool:delete" in entry for entry in tool_execution_log), (
            f"Turn 2 tool should NOT execute after reject, but log shows: {tool_execution_log}"
        )


class TestTwoLevelInterruptProxying:
    """Sub-agent interrupts proxied up through the orchestrator.

    In production:
    1. Sub-agent HITL calls interrupt() → GraphInterrupt
    2. Orchestrator catches it, calls its own interrupt() → orchestrator suspends
    3. User responds → orchestrator resumes → sub-agent resumes with decisions

    KNOWN GAP, left standing deliberately rather than quietly. These build the
    outer graph and its proxying by hand, which is the same objection the module
    docstring raises about the router: the test supplies the mechanism it
    asserts. Production proxies through ``DynamicToolDispatchMiddleware``
    (``dynamic_tool_dispatch.py:349`` reads ``interrupt_value["action_requests"]``),
    not through this. So these prove a simulation of the flow is coherent — not
    that ours is.

    Doing it properly means a ``MockSubAgent`` that raises ``GraphInterrupt`` and
    a real dispatch through ``scripted_graph()``. That is its own piece of work
    (``tests/TEST_RIGOR_PLAN.md``), and deleting these first would drop the only
    coverage of the flow, weak as it is. Do not read them as end-to-end.
    """

    @pytest.fixture(autouse=True)
    def clear_log(self):
        tool_execution_log.clear()
        yield
        tool_execution_log.clear()

    @pytest.mark.asyncio
    async def test_two_level_reject_single_tool_call(self):
        """Two-level: outer graph catches sub-agent's HITL interrupt and proxies reject."""
        from langgraph.errors import GraphInterrupt

        # ── Inner graph (sub-agent) ──
        inner_checkpointer = MemorySaver()
        inner_model = ScriptedChatModel(
            responses=[
                AIMessage(content="", tool_calls=[
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "destroy"}, id="tc-inner-1"),
                ]),
                AIMessage(content="OK, won't destroy."),
            ]
        )
        inner_graph = create_agent(
            inner_model,
            tools=[dangerous_tool, safe_tool],
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on={"dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
                ),
            ],
            checkpointer=inner_checkpointer,
        )

        inner_thread_id = "inner-thread-1"
        inner_config = {"configurable": {"thread_id": inner_thread_id}}

        # ── Outer graph (orchestrator) with tool that delegates to inner ──
        outer_checkpointer = MemorySaver()

        async def delegate_to_subagent(state: State) -> dict:
            """Simulates dynamic_tool_dispatch: run sub-agent and proxy HITL."""
            subagent_input = {"messages": [HumanMessage(content="do something dangerous")]}

            try:
                async for _ in inner_graph.astream(subagent_input, inner_config, stream_mode="updates"):
                    pass

                # Post-stream interrupt check (like _astream_impl)
                post_state = await inner_graph.aget_state(inner_config)
                if post_state and post_state.interrupts:
                    raise GraphInterrupt(post_state.interrupts)

                # If no interrupt, get final state
                final_state = await inner_graph.aget_state(inner_config)
                final_msgs = final_state.values.get("messages", [])
                last_ai = next((m for m in reversed(final_msgs) if isinstance(m, AIMessage)), None)
                return {"messages": [AIMessage(content=last_ai.content if last_ai else "done")]}

            except GraphInterrupt as gi:
                # Proxy sub-agent's interrupt to orchestrator (like dynamic_tool_dispatch)
                sub_interrupt_value = gi.args[0][0].value if gi.args and gi.args[0] else {}
                user_decisions = interrupt(sub_interrupt_value)

                # Resume sub-agent with user's decisions
                resume_cmd = Command(resume=user_decisions if isinstance(user_decisions, dict) else {})
                async for _ in inner_graph.astream(resume_cmd, inner_config, stream_mode="updates"):
                    pass

                # Post-resume interrupt check
                post_state = await inner_graph.aget_state(inner_config)
                if post_state and post_state.interrupts:
                    raise GraphInterrupt(post_state.interrupts)

                final_state = await inner_graph.aget_state(inner_config)
                final_msgs = final_state.values.get("messages", [])
                last_ai = next((m for m in reversed(final_msgs) if isinstance(m, AIMessage)), None)
                return {"messages": [AIMessage(content=last_ai.content if last_ai else "done after resume")]}

        outer_graph_builder = StateGraph(State)
        outer_graph_builder.add_node("delegate", delegate_to_subagent)
        outer_graph_builder.add_edge(START, "delegate")
        outer_graph_builder.add_edge("delegate", END)
        outer_graph = outer_graph_builder.compile(checkpointer=outer_checkpointer)

        outer_thread_id = "outer-thread-1"
        outer_config = {"configurable": {"thread_id": outer_thread_id}}

        # ═══ Step 1: Run outer graph → should interrupt (two-level) ═══
        async for _ in outer_graph.astream(
            {"messages": [HumanMessage(content="delegate something")]},
            outer_config,
            stream_mode="updates",
        ):
            pass

        outer_state = await outer_graph.aget_state(outer_config)
        assert outer_state.next, "Outer graph should be interrupted (proxied from sub-agent)"
        assert not tool_execution_log, "No tools should have executed yet"

        # Verify the interrupt contains the sub-agent's HITL request
        interrupt_value = outer_state.interrupts[-1].value
        assert "action_requests" in interrupt_value, f"Should have action_requests, got: {interrupt_value}"

        # ═══ Step 2: Resume with REJECT ═══
        reject_cmd = Command(resume={"decisions": [{"type": "reject", "message": "Absolutely not!"}]})
        async for _ in outer_graph.astream(reject_cmd, outer_config, stream_mode="updates"):
            pass

        # ═══ Verify: dangerous_tool did NOT execute ═══
        assert not any("dangerous_tool" in entry for entry in tool_execution_log), (
            f"dangerous_tool should NOT execute after two-level reject, but log shows: {tool_execution_log}"
        )

    @pytest.mark.asyncio
    async def test_two_level_reject_parallel_tool_calls(self):
        """Two-level proxying with Gemini-style parallel tool calls, all rejected."""
        from langgraph.errors import GraphInterrupt

        inner_checkpointer = MemorySaver()
        inner_model = ScriptedChatModel(
            responses=[
                AIMessage(content="", tool_calls=[
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "a"}, id="tc-par-a"),
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "b"}, id="tc-par-b"),
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "c"}, id="tc-par-c"),
                ]),
                AIMessage(content="All three rejected, noted."),
            ]
        )
        inner_graph = create_agent(
            inner_model,
            tools=[dangerous_tool, safe_tool],
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on={"dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
                ),
            ],
            checkpointer=inner_checkpointer,
        )

        inner_thread_id = "inner-par-thread"
        inner_config = {"configurable": {"thread_id": inner_thread_id}}

        outer_checkpointer = MemorySaver()

        async def delegate_to_subagent(state: State) -> dict:
            subagent_input = {"messages": [HumanMessage(content="do three things")]}

            try:
                async for _ in inner_graph.astream(subagent_input, inner_config, stream_mode="updates"):
                    pass
                post_state = await inner_graph.aget_state(inner_config)
                if post_state and post_state.interrupts:
                    raise GraphInterrupt(post_state.interrupts)
                final_state = await inner_graph.aget_state(inner_config)
                final_msgs = final_state.values.get("messages", [])
                last_ai = next((m for m in reversed(final_msgs) if isinstance(m, AIMessage)), None)
                return {"messages": [AIMessage(content=last_ai.content if last_ai else "done")]}
            except GraphInterrupt as gi:
                sub_interrupt_value = gi.args[0][0].value if gi.args and gi.args[0] else {}
                user_decisions = interrupt(sub_interrupt_value)
                resume_cmd = Command(resume=user_decisions if isinstance(user_decisions, dict) else {})
                async for _ in inner_graph.astream(resume_cmd, inner_config, stream_mode="updates"):
                    pass
                post_state = await inner_graph.aget_state(inner_config)
                if post_state and post_state.interrupts:
                    raise GraphInterrupt(post_state.interrupts)
                final_state = await inner_graph.aget_state(inner_config)
                final_msgs = final_state.values.get("messages", [])
                last_ai = next((m for m in reversed(final_msgs) if isinstance(m, AIMessage)), None)
                return {"messages": [AIMessage(content=last_ai.content if last_ai else "done after resume")]}

        outer_graph_builder = StateGraph(State)
        outer_graph_builder.add_node("delegate", delegate_to_subagent)
        outer_graph_builder.add_edge(START, "delegate")
        outer_graph_builder.add_edge("delegate", END)
        outer_graph = outer_graph_builder.compile(checkpointer=outer_checkpointer)

        outer_thread_id = "outer-par-thread"
        outer_config = {"configurable": {"thread_id": outer_thread_id}}

        # ═══ Step 1: Run → interrupt ═══
        async for _ in outer_graph.astream(
            {"messages": [HumanMessage(content="delegate parallel")]}, outer_config, stream_mode="updates",
        ):
            pass

        outer_state = await outer_graph.aget_state(outer_config)
        assert outer_state.next, "Should be interrupted"
        interrupt_value = outer_state.interrupts[-1].value
        assert len(interrupt_value["action_requests"]) == 3, (
            f"Should have 3 action_requests, got: {len(interrupt_value['action_requests'])}"
        )

        # ═══ Step 2: Resume with 3 reject decisions ═══
        reject_cmd = Command(resume={"decisions": [
            {"type": "reject", "message": "No a!"},
            {"type": "reject", "message": "No b!"},
            {"type": "reject", "message": "No c!"},
        ]})
        async for _ in outer_graph.astream(reject_cmd, outer_config, stream_mode="updates"):
            pass

        # ═══ Verify: NO tools executed ═══
        assert not tool_execution_log, (
            f"No tools should execute after two-level reject, log: {tool_execution_log}"
        )

    @pytest.mark.asyncio
    async def test_two_level_single_reject_for_parallel_calls(self):
        """Two-level, real separate-checkpoint dispatch: one blanket reject for N parallel calls.

        Mirrors production rather than a shared subgraph: the sub-agent runs on its OWN
        thread/checkpointer. The orchestrator node detects the sub-agent's pending
        interrupt in its checkpoint (PATH 1), surfaces it via its own ``interrupt()``,
        and on resume rebuilds the sub-agent resume with the real
        ``_build_subagent_resume_command`` (id-keyed map, local branch). The orchestrator
        itself is resumed via the real ``executor._build_interrupt_resume_map``, which
        replicates the single blanket reject to the interrupt's action_request count and
        keys it by interrupt id. Exercises both helpers end to end.
        """
        from unittest.mock import Mock

        from agent_common.a2a.base import LocalA2ARunnable
        from app.core.executor import OrchestratorDeepAgentExecutor
        from app.middleware.dynamic_tool_dispatch import _build_subagent_resume_command

        # ── Sub-agent: real graph, its OWN checkpointer + thread (separate from orchestrator) ──
        inner_model = ScriptedChatModel(
            responses=[
                AIMessage(content="", tool_calls=[
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "x"}, id="tc-x"),
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "y"}, id="tc-y"),
                ]),
                AIMessage(content="Rejected."),
            ]
        )
        inner_graph = create_agent(
            inner_model,
            tools=[dangerous_tool, safe_tool],
            middleware=[
                HumanInTheLoopMiddleware(
                    interrupt_on={"dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"])}
                ),
            ],
            checkpointer=MemorySaver(),
        )
        inner_config = {"configurable": {"thread_id": "inner-single-reject"}}

        # Stand-in for a local in-process sub-agent so _build_subagent_resume_command
        # takes its LocalA2ARunnable (id-keyed map) branch.
        local_runnable = Mock(spec=LocalA2ARunnable)

        async def delegate_to_subagent(state: State) -> dict:
            # PATH 1: if the sub-agent already has a pending interrupt in its checkpoint,
            # surface + resume it — do NOT re-run fresh input over an interrupted thread.
            sub_state = await inner_graph.aget_state(inner_config)
            if not sub_state.interrupts:
                async for _ in inner_graph.astream(
                    {"messages": [HumanMessage(content="do two things")]}, inner_config, stream_mode="updates"
                ):
                    pass
                sub_state = await inner_graph.aget_state(inner_config)

            if sub_state.interrupts:
                sub_interrupt = sub_state.interrupts[-1]
                # First orchestrator pass: raises → orchestrator suspends. Resume: returns decisions.
                user_decisions = interrupt(sub_interrupt.value)
                resume_cmd = _build_subagent_resume_command(local_runnable, sub_interrupt, user_decisions)
                async for _ in inner_graph.astream(resume_cmd, inner_config, stream_mode="updates"):
                    pass

            final_state = await inner_graph.aget_state(inner_config)
            final_msgs = final_state.values.get("messages", [])
            last_ai = next((m for m in reversed(final_msgs) if isinstance(m, AIMessage)), None)
            return {"messages": [AIMessage(content=last_ai.content if last_ai else "done")]}

        outer_graph_builder = StateGraph(State)
        outer_graph_builder.add_node("delegate", delegate_to_subagent)
        outer_graph_builder.add_edge(START, "delegate")
        outer_graph_builder.add_edge("delegate", END)
        outer_graph = outer_graph_builder.compile(checkpointer=MemorySaver())

        outer_config = {"configurable": {"thread_id": "outer-single-reject"}}

        # Run → orchestrator suspends at its own interrupt() carrying the sub-agent's 2 action_requests.
        async for _ in outer_graph.astream(
            {"messages": [HumanMessage(content="delegate")]}, outer_config, stream_mode="updates",
        ):
            pass

        outer_state = await outer_graph.aget_state(outer_config)
        assert outer_state.next, "Orchestrator should be interrupted"
        assert len(outer_state.interrupts[-1].value["action_requests"]) == 2

        # Resume the orchestrator exactly as the real executor does: a single blanket reject
        # → id-keyed map, replicated to the interrupt's 2 action_requests.
        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
            outer_state.interrupts, [{"type": "reject", "message": "No!"}], query=""
        )
        async for _ in outer_graph.astream(Command(resume=resume_map), outer_config, stream_mode="updates"):
            pass

        # Both parallel calls were rejected → no tool executed.
        assert not tool_execution_log, f"No tools should execute after reject, log: {tool_execution_log}"


