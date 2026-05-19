"""Integration tests for HITL reject flow with parallel tool calls.

Reproduces the bug where rejecting HITL-guarded tool calls from models
that make parallel tool calls (e.g. Gemini) doesn't prevent execution.
"""

import uuid
from collections import deque
from typing import Any, Optional

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolCall, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
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


# --- Minimal fixtures ---

tool_execution_log: list[str] = []
"""Global log to track which tools actually executed."""


@tool
def dangerous_tool(action: str) -> str:
    """A tool that should be guarded by HITL."""
    tool_execution_log.append(f"dangerous_tool:{action}")
    return f"executed: {action}"


@tool
def safe_tool(query: str) -> str:
    """A tool that is auto-approved."""
    tool_execution_log.append(f"safe_tool:{query}")
    return f"result: {query}"


class FakeToolCallModel(BaseChatModel):
    """A fake model that supports bind_tools and returns scripted responses."""

    responses: deque[AIMessage] = deque()

    @property
    def _llm_type(self) -> str:
        return "fake-tool-call"

    def bind_tools(self, tools: list, **kwargs: Any) -> "FakeToolCallModel":
        """Accept tools binding (no-op for testing)."""
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        response = self.responses.popleft()
        return ChatResult(generations=[ChatGeneration(message=response)])


class State(TypedDict):
    messages: Annotated[list, add_messages]


# --- Helper to build a graph using HITL middleware directly ---


def build_hitl_graph(
    *,
    hitl_config: dict[str, bool | InterruptOnConfig],
    checkpointer=None,
):
    """Build a minimal graph with HITL middleware's after_model as a node.

    Replicates the topology from langchain.agents.factory:
      model → hitl.after_model → routing (conditional: tools | model | end)
    """
    hitl = HumanInTheLoopMiddleware(interrupt_on=hitl_config)

    tool_list = [dangerous_tool, safe_tool]
    tool_node = ToolNode(tool_list)

    builder = StateGraph(State)

    # "model" node is a no-op — we inject AIMessage directly via input
    builder.add_node("model", lambda state: None)

    # HITL after_model node
    # Runtime is required by the middleware, but only for description callbacks.
    # We pass None since our config uses static descriptions.
    def hitl_after_model(state):
        from unittest.mock import Mock

        runtime = Mock()
        return hitl.after_model(state, runtime)

    builder.add_node("hitl_after_model", hitl_after_model)

    # Tools node
    builder.add_node("tools", tool_node)

    # Wiring
    builder.add_edge(START, "model")
    builder.add_edge("model", "hitl_after_model")

    def route_after_hitl(state):
        """Route based on pending tool calls (same logic as factory)."""
        messages = state["messages"]
        # Find last AI message
        last_ai = None
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                last_ai = msg
                break
        if not last_ai or not last_ai.tool_calls:
            return END

        # Collect tool messages after the last AI message
        ai_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i] is last_ai or (isinstance(messages[i], AIMessage) and messages[i].id == last_ai.id):
                ai_idx = i
                break
        tool_msgs = [m for m in messages[ai_idx + 1 :] if isinstance(m, ToolMessage)] if ai_idx is not None else []
        tool_msg_ids = {m.tool_call_id for m in tool_msgs}

        pending = [c for c in last_ai.tool_calls if c["id"] not in tool_msg_ids]
        if pending:
            return "tools"
        # All tool calls have responses (artificial from HITL) → back to model
        return "model"

    builder.add_conditional_edges("hitl_after_model", route_after_hitl, ["tools", "model", END])
    builder.add_edge("tools", "model")

    return builder.compile(checkpointer=checkpointer)


# --- Tests ---


class TestHITLRejectIntegration:
    """Test that HITL reject properly prevents tool execution."""

    @pytest.mark.asyncio
    async def test_single_tool_call_reject_prevents_execution(self):
        """Single HITL-guarded tool call, rejected → tools should NOT execute."""
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # Inject an AIMessage with a single tool call
        ai_msg = AIMessage(
            content="",
            id="ai-1",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "delete"}, id="tc-1"),
            ],
        )
        input_state = {"messages": [HumanMessage(content="do something"), ai_msg]}

        # Run — should interrupt at HITL
        events = []
        async for event in graph.astream(input_state, config, stream_mode="updates"):
            events.append(event)

        # Verify interrupt
        state = await graph.aget_state(config)
        assert state.next, "Graph should be interrupted (has next nodes)"
        assert len(state.tasks) > 0
        interrupts = [t for t in state.tasks if t.interrupts]
        assert interrupts, "Should have HITL interrupt"

        # Resume with REJECT
        resume_cmd = Command(resume={"decisions": [{"type": "reject", "message": "No!"}]})
        events_after = []
        async for event in graph.astream(resume_cmd, config, stream_mode="updates"):
            events_after.append(event)

        # Verify: tools node should NOT have run
        tools_ran = any("tools" in event for event in events_after)
        assert not tools_ran, f"Tools should NOT execute after reject, but got events: {events_after}"

        # Verify final state has rejection ToolMessage
        final_state = await graph.aget_state(config)
        msgs = final_state.values["messages"]
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        assert any(m.status == "error" and "No!" in m.content for m in tool_msgs), (
            f"Should have rejection ToolMessage, got: {[m.content for m in tool_msgs]}"
        )

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_reject_all_prevents_execution(self):
        """Multiple HITL-guarded tool calls (Gemini-style), ALL rejected → tools should NOT execute."""
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # Inject AIMessage with MULTIPLE tool calls (like Gemini does)
        ai_msg = AIMessage(
            content="",
            id="ai-multi",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "delete-1"}, id="tc-1"),
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "delete-2"}, id="tc-2"),
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "delete-3"}, id="tc-3"),
            ],
        )
        input_state = {"messages": [HumanMessage(content="do three things"), ai_msg]}

        # Run — should interrupt
        async for _ in graph.astream(input_state, config, stream_mode="updates"):
            pass

        state = await graph.aget_state(config)
        assert state.next, "Graph should be interrupted"

        # Verify the interrupt has 3 action_requests
        for task in state.tasks:
            if task.interrupts:
                interrupt_value = task.interrupts[0].value
                assert len(interrupt_value["action_requests"]) == 3, (
                    f"Expected 3 action_requests, got {len(interrupt_value['action_requests'])}"
                )

        # Resume with 3 reject decisions (one per tool call)
        resume_cmd = Command(
            resume={
                "decisions": [
                    {"type": "reject", "message": "No!"},
                    {"type": "reject", "message": "No!"},
                    {"type": "reject", "message": "No!"},
                ]
            }
        )
        events_after = []
        async for event in graph.astream(resume_cmd, config, stream_mode="updates"):
            events_after.append(event)

        # Verify: tools node should NOT have run
        tools_ran = any("tools" in event for event in events_after)
        assert not tools_ran, f"Tools should NOT execute after reject, but got events: {events_after}"

    @pytest.mark.asyncio
    async def test_single_decision_for_multiple_tool_calls_raises_error(self):
        """Single reject decision for multiple tool calls → should raise ValueError.

        This is the actual bug: the UI sends 1 decision for N tool calls.
        """
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # Inject AIMessage with MULTIPLE tool calls
        ai_msg = AIMessage(
            content="",
            id="ai-multi-2",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "a"}, id="tc-a"),
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "b"}, id="tc-b"),
            ],
        )
        input_state = {"messages": [HumanMessage(content="do two things"), ai_msg]}

        # Run — should interrupt
        async for _ in graph.astream(input_state, config, stream_mode="updates"):
            pass

        state = await graph.aget_state(config)
        assert state.next, "Graph should be interrupted"

        # Resume with SINGLE reject decision (what the UI actually sends)
        resume_cmd = Command(resume={"decisions": [{"type": "reject", "message": "No!"}]})

        # This should raise ValueError due to decision count mismatch
        with pytest.raises(ValueError, match="Number of human decisions"):
            async for _ in graph.astream(resume_cmd, config, stream_mode="updates"):
                pass

    @pytest.mark.asyncio
    async def test_mixed_guarded_and_unguarded_tools_reject(self):
        """Mix of HITL-guarded and auto-approved tools. Reject guarded → only unguarded execute."""
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
                # safe_tool is NOT in the config → auto-approved
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # AIMessage with both guarded and unguarded tool calls
        ai_msg = AIMessage(
            content="",
            id="ai-mixed",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "bad"}, id="tc-danger"),
                ToolCall(type="tool_call", name="safe_tool", args={"query": "ok"}, id="tc-safe"),
            ],
        )
        input_state = {"messages": [HumanMessage(content="do mixed things"), ai_msg]}

        # Run — should interrupt (for dangerous_tool only)
        async for _ in graph.astream(input_state, config, stream_mode="updates"):
            pass

        state = await graph.aget_state(config)
        assert state.next

        # Resume with reject for the guarded tool
        resume_cmd = Command(resume={"decisions": [{"type": "reject", "message": "Nope"}]})
        events_after = []
        async for event in graph.astream(resume_cmd, config, stream_mode="updates"):
            events_after.append(event)

        # The safe_tool should still execute (it was auto-approved)
        # The dangerous_tool should NOT execute
        final_state = await graph.aget_state(config)
        msgs = final_state.values["messages"]
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]

        # Should have both:
        # 1. Rejection ToolMessage for dangerous_tool (status=error)
        # 2. Execution ToolMessage for safe_tool (status=success or no status)
        rejection_msgs = [m for m in tool_msgs if m.status == "error" and m.name == "dangerous_tool"]
        execution_msgs = [m for m in tool_msgs if m.name == "safe_tool"]

        assert rejection_msgs, f"Should have rejection for dangerous_tool, got: {[(m.name, m.status, m.content) for m in tool_msgs]}"
        assert execution_msgs, f"Should have execution for safe_tool, got: {[(m.name, m.status, m.content) for m in tool_msgs]}"

    @pytest.mark.asyncio
    async def test_approve_then_reject_across_turns(self):
        """Multi-turn: approve on turn 1, reject on turn 2.

        Tests whether approve decisions from turn 1 leak into turn 2's reject.
        Same thread_id (same conversation), so the checkpoint persists.
        """
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # ═══ TURN 1: Approve ═══
        ai_msg_1 = AIMessage(
            content="",
            id="ai-turn1",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "create"}, id="tc-t1"),
            ],
        )
        input_1 = {"messages": [HumanMessage(content="turn 1"), ai_msg_1]}

        async for _ in graph.astream(input_1, config, stream_mode="updates"):
            pass

        state_1 = await graph.aget_state(config)
        assert state_1.next, "Turn 1 should interrupt"

        # Approve turn 1
        approve_cmd = Command(resume={"decisions": [{"type": "approve"}]})
        async for _ in graph.astream(approve_cmd, config, stream_mode="updates"):
            pass

        # Verify turn 1: tool executed
        state_after_t1 = await graph.aget_state(config)
        msgs_t1 = state_after_t1.values["messages"]
        tool_results_t1 = [m for m in msgs_t1 if isinstance(m, ToolMessage) and m.name == "dangerous_tool"]
        assert any("executed:" in m.content for m in tool_results_t1), (
            f"Turn 1 tool should have executed, got: {[m.content for m in tool_results_t1]}"
        )

        # ═══ TURN 2: Reject ═══
        ai_msg_2 = AIMessage(
            content="",
            id="ai-turn2",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "delete"}, id="tc-t2"),
            ],
        )
        input_2 = {"messages": [HumanMessage(content="turn 2"), ai_msg_2]}

        async for _ in graph.astream(input_2, config, stream_mode="updates"):
            pass

        state_2 = await graph.aget_state(config)
        assert state_2.next, "Turn 2 should interrupt"

        # Verify the pending interrupt is for turn 2's tool call
        for task in state_2.tasks:
            if task.interrupts:
                interrupt_value = task.interrupts[0].value
                assert interrupt_value["action_requests"][0]["args"]["action"] == "delete", (
                    f"Interrupt should be for turn 2's 'delete' action, got: {interrupt_value}"
                )

        # Reject turn 2
        reject_cmd = Command(resume={"decisions": [{"type": "reject", "message": "No!"}]})
        events_t2 = []
        async for event in graph.astream(reject_cmd, config, stream_mode="updates"):
            events_t2.append(event)

        # Verify turn 2: tool did NOT execute
        tools_ran = any("tools" in event for event in events_t2)
        assert not tools_ran, (
            f"Turn 2 tools should NOT execute after reject. "
            f"Approve from turn 1 may have leaked. Events: {events_t2}"
        )

        # Verify the rejection message is present
        state_after_t2 = await graph.aget_state(config)
        msgs_t2 = state_after_t2.values["messages"]
        # Find ToolMessages for turn 2's tool call
        reject_msgs = [
            m for m in msgs_t2
            if isinstance(m, ToolMessage) and m.tool_call_id == "tc-t2"
        ]
        assert reject_msgs, f"Should have ToolMessage for turn 2's rejected tool call"
        assert reject_msgs[0].status == "error", (
            f"Rejected tool message should have error status, got: {reject_msgs[0].status}"
        )

    @pytest.mark.asyncio
    async def test_approve_then_reject_multiple_parallel_calls(self):
        """Multi-turn with parallel tool calls: approve N on turn 1, reject N on turn 2.

        Gemini scenario: model makes multiple tool calls per turn.
        """
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # ═══ TURN 1: 2 parallel tool calls, approve both ═══
        ai_msg_1 = AIMessage(
            content="",
            id="ai-par-t1",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "a"}, id="tc-p1a"),
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "b"}, id="tc-p1b"),
            ],
        )
        input_1 = {"messages": [HumanMessage(content="parallel turn 1"), ai_msg_1]}

        async for _ in graph.astream(input_1, config, stream_mode="updates"):
            pass

        state_1 = await graph.aget_state(config)
        assert state_1.next, "Turn 1 should interrupt with 2 action_requests"

        # Approve both (2 decisions for 2 tool calls)
        approve_cmd = Command(resume={"decisions": [{"type": "approve"}, {"type": "approve"}]})
        async for _ in graph.astream(approve_cmd, config, stream_mode="updates"):
            pass

        # Verify both tools executed
        state_after_t1 = await graph.aget_state(config)
        msgs_t1 = state_after_t1.values["messages"]
        executed_t1 = [
            m for m in msgs_t1
            if isinstance(m, ToolMessage) and m.name == "dangerous_tool" and "executed:" in m.content
        ]
        assert len(executed_t1) == 2, f"Both tools should execute in turn 1, got {len(executed_t1)}"

        # ═══ TURN 2: 2 parallel tool calls, reject both ═══
        ai_msg_2 = AIMessage(
            content="",
            id="ai-par-t2",
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "x"}, id="tc-p2x"),
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "y"}, id="tc-p2y"),
            ],
        )
        input_2 = {"messages": [HumanMessage(content="parallel turn 2"), ai_msg_2]}

        async for _ in graph.astream(input_2, config, stream_mode="updates"):
            pass

        state_2 = await graph.aget_state(config)
        assert state_2.next, "Turn 2 should interrupt with 2 action_requests"

        # Reject both (2 decisions for 2 tool calls)
        reject_cmd = Command(resume={"decisions": [
            {"type": "reject", "message": "No x!"},
            {"type": "reject", "message": "No y!"},
        ]})
        events_t2 = []
        async for event in graph.astream(reject_cmd, config, stream_mode="updates"):
            events_t2.append(event)

        # Verify: tools should NOT execute in turn 2
        tools_ran = any("tools" in event for event in events_t2)
        assert not tools_ran, (
            f"Turn 2 tools should NOT execute after reject. Events: {events_t2}"
        )


class TestHITLRejectWithCreateAgent:
    """Tests using the real create_agent factory to match production graph topology.

    These tests use a FakeModel that deterministically generates tool calls,
    simulating Gemini-style parallel tool calling behavior.
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

        # FakeModel: first call → tool call, second call (after reject) → text response
        model = FakeToolCallModel(
            responses=deque([
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "destroy"}, id="tc-f1"),
                    ],
                ),
                AIMessage(content="OK, I won't do that."),
            ])
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

        # FakeModel: first call → 3 parallel tool calls, second call → text
        model = FakeToolCallModel(
            responses=deque([
                AIMessage(
                    content="",
                    tool_calls=[
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "a"}, id="tc-pa"),
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "b"}, id="tc-pb"),
                        ToolCall(type="tool_call", name="dangerous_tool", args={"action": "c"}, id="tc-pc"),
                    ],
                ),
                AIMessage(content="All rejected, understood."),
            ])
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
        model = FakeToolCallModel(
            responses=deque([
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
            ])
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
    """Tests that simulate the production two-level interrupt proxying.

    In production:
    1. Sub-agent HITL calls interrupt() → GraphInterrupt
    2. Orchestrator catches it, calls its own interrupt() → orchestrator suspends
    3. User responds → orchestrator resumes → sub-agent resumes with decisions

    These tests use two REAL LangGraph graphs to simulate this exact flow.
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
        inner_model = FakeToolCallModel(
            responses=deque([
                AIMessage(content="", tool_calls=[
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "destroy"}, id="tc-inner-1"),
                ]),
                AIMessage(content="OK, won't destroy."),
            ])
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
        inner_model = FakeToolCallModel(
            responses=deque([
                AIMessage(content="", tool_calls=[
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "a"}, id="tc-par-a"),
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "b"}, id="tc-par-b"),
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "c"}, id="tc-par-c"),
                ]),
                AIMessage(content="All three rejected, noted."),
            ])
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
        """Two-level: single reject decision for multiple parallel calls (production bug).

        The frontend sends exactly 1 decision regardless of how many tool calls exist.
        The executor replicates it. This test verifies the flow AFTER replication.
        But also tests what happens WITHOUT replication (should error, not execute).
        """
        from langgraph.errors import GraphInterrupt

        inner_checkpointer = MemorySaver()
        inner_model = FakeToolCallModel(
            responses=deque([
                AIMessage(content="", tool_calls=[
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "x"}, id="tc-single-x"),
                    ToolCall(type="tool_call", name="dangerous_tool", args={"action": "y"}, id="tc-single-y"),
                ]),
                AIMessage(content="Rejected."),
            ])
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

        inner_config = {"configurable": {"thread_id": "inner-single-reject"}}
        outer_checkpointer = MemorySaver()

        async def delegate_to_subagent(state: State) -> dict:
            subagent_input = {"messages": [HumanMessage(content="do two things")]}
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

                # Simulate executor's decision replication
                decisions = user_decisions.get("decisions", []) if isinstance(user_decisions, dict) else []
                action_requests = sub_interrupt_value.get("action_requests", [])
                if len(decisions) == 1 and len(action_requests) > 1:
                    user_decisions = {"decisions": decisions * len(action_requests)}

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

        outer_config = {"configurable": {"thread_id": "outer-single-reject"}}

        # Run → interrupt
        async for _ in outer_graph.astream(
            {"messages": [HumanMessage(content="delegate")]}, outer_config, stream_mode="updates",
        ):
            pass

        outer_state = await outer_graph.aget_state(outer_config)
        assert outer_state.next, "Should be interrupted"
        assert len(outer_state.interrupts[-1].value["action_requests"]) == 2

        # Resume with SINGLE reject (frontend behavior) — replication happens in delegate_to_subagent
        reject_cmd = Command(resume={"decisions": [{"type": "reject", "message": "No!"}]})
        async for _ in outer_graph.astream(reject_cmd, outer_config, stream_mode="updates"):
            pass

        # Verify: NO tools executed
        assert not tool_execution_log, (
            f"No tools should execute after replicated reject, log: {tool_execution_log}"
        )


class TestGeminiMessageFormat:
    """Test HITL reject with Gemini-style AIMessage format.

    Gemini AIMessages have:
    - content=[] (empty list)
    - additional_kwargs with function_call (no id field)
    - tool_calls generated client-side with UUID ids
    """

    @pytest.fixture(autouse=True)
    def clear_log(self):
        tool_execution_log.clear()
        yield
        tool_execution_log.clear()

    @pytest.mark.asyncio
    async def test_gemini_format_single_reject(self):
        """Gemini-format AIMessage with content=[] and additional_kwargs — reject should work."""
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        # Gemini-style AIMessage: content=[], function_call in additional_kwargs
        tc_id = str(uuid.uuid4())
        ai_msg = AIMessage(
            content=[],  # Gemini uses empty list
            id=f"lc_run--{uuid.uuid4()}",
            additional_kwargs={
                "function_call": {
                    "name": "dangerous_tool",
                    "arguments": '{"action": "delete_everything"}',
                },
            },
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "delete_everything"}, id=tc_id),
            ],
        )
        input_state = {"messages": [HumanMessage(content="do something dangerous"), ai_msg]}

        # Run — should interrupt at HITL
        async for _ in graph.astream(input_state, config, stream_mode="updates"):
            pass

        state = await graph.aget_state(config)
        assert state.next, "Graph should be interrupted"

        # Resume with REJECT
        reject_cmd = Command(resume={"decisions": [{"type": "reject", "message": "User declined"}]})
        events_after = []
        async for event in graph.astream(reject_cmd, config, stream_mode="updates"):
            events_after.append(event)

        # Verify: tools should NOT execute
        assert not tool_execution_log, f"Tools executed despite reject: {tool_execution_log}"

        # Verify: rejection ToolMessage exists with correct tool_call_id
        final_state = await graph.aget_state(config)
        msgs = final_state.values["messages"]
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        reject_tms = [m for m in tool_msgs if m.status == "error"]
        assert reject_tms, f"Should have rejection ToolMessage, got: {[m.content for m in tool_msgs]}"
        assert reject_tms[0].tool_call_id == tc_id, (
            f"ToolMessage tool_call_id should match: expected {tc_id}, got {reject_tms[0].tool_call_id}"
        )

    @pytest.mark.asyncio
    async def test_gemini_format_reject_with_factory_graph(self):
        """Full factory graph with Gemini-format AIMessage — reject should prevent execution."""

        tc_id = str(uuid.uuid4())
        # Gemini-format: content=[], additional_kwargs with function_call
        gemini_ai_msg = AIMessage(
            content=[],
            additional_kwargs={
                "function_call": {
                    "name": "dangerous_tool",
                    "arguments": '{"action": "nuke"}',
                },
                "__gemini_function_call_thought_signatures__": [],
            },
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": "nuke"}, id=tc_id),
            ],
        )
        # After reject, model responds with text
        text_response = AIMessage(content="OK, I won't do that.")

        fake_model = FakeToolCallModel(responses=deque([gemini_ai_msg, text_response]))
        checkpointer = MemorySaver()

        agent = create_agent(
            model=fake_model,
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

        # First run: model → HITL interrupt
        async for _ in agent.astream(
            {"messages": [HumanMessage(content="nuke it")]}, config, stream_mode="updates",
        ):
            pass

        state = await agent.aget_state(config)
        assert state.next, "Should be interrupted"

        # Resume with REJECT
        reject_cmd = Command(resume={"decisions": [{"type": "reject", "message": "User declined"}]})
        async for _ in agent.astream(reject_cmd, config, stream_mode="updates"):
            pass

        # Verify: tool did NOT execute
        assert not tool_execution_log, f"Tool executed despite reject: {tool_execution_log}"

        # Verify rejection ToolMessage in state
        final_state = await agent.aget_state(config)
        msgs = final_state.values["messages"]
        reject_tms = [m for m in msgs if isinstance(m, ToolMessage) and m.status == "error"]
        assert reject_tms, "Rejection ToolMessage should exist"
        assert reject_tms[0].tool_call_id == tc_id

    @pytest.mark.asyncio
    async def test_gemini_format_parallel_reject(self):
        """Gemini-format with multiple parallel tool calls — all rejected."""
        checkpointer = MemorySaver()
        graph = build_hitl_graph(
            hitl_config={
                "dangerous_tool": InterruptOnConfig(allowed_decisions=["approve", "reject"]),
            },
            checkpointer=checkpointer,
        )

        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}}

        tc_ids = [str(uuid.uuid4()) for _ in range(3)]
        # Gemini-style: content=[], multiple function_calls
        ai_msg = AIMessage(
            content=[],
            id=f"lc_run--{uuid.uuid4()}",
            additional_kwargs={
                "function_call": {
                    "name": "dangerous_tool",
                    "arguments": '{"action": "delete_1"}',
                },
            },
            tool_calls=[
                ToolCall(type="tool_call", name="dangerous_tool", args={"action": f"delete_{i}"}, id=tc_ids[i])
                for i in range(3)
            ],
        )
        input_state = {"messages": [HumanMessage(content="delete all"), ai_msg]}

        # Run — should interrupt
        async for _ in graph.astream(input_state, config, stream_mode="updates"):
            pass

        state = await graph.aget_state(config)
        assert state.next, "Should be interrupted"

        # Resume with 3 rejections
        reject_cmd = Command(resume={"decisions": [
            {"type": "reject", "message": "No delete 1"},
            {"type": "reject", "message": "No delete 2"},
            {"type": "reject", "message": "No delete 3"},
        ]})
        async for _ in graph.astream(reject_cmd, config, stream_mode="updates"):
            pass

        # Verify: NO tools executed
        assert not tool_execution_log, f"Tools executed despite reject: {tool_execution_log}"

        # Verify 3 rejection ToolMessages
        final_state = await graph.aget_state(config)
        msgs = final_state.values["messages"]
        reject_tms = [m for m in msgs if isinstance(m, ToolMessage) and m.status == "error"]
        assert len(reject_tms) == 3, f"Expected 3 rejection ToolMessages, got {len(reject_tms)}"
