"""Graph-level tests for loop force-stop (issue #182).

The unit tests call ``aafter_model`` directly and take on faith that the state update it
returns does the right thing once LangGraph applies it. These run a *real* agent graph with
a real checkpointer instead, because the bug that bricked conversations was never visible
in a single hook call: it was written into the checkpoint, and only the *next* turn died.

So the assertions here are about what ends up persisted, and about what the model is handed
when the conversation is resumed — which is exactly what the provider validates:

* every ``ToolMessage`` answers a ``tool_call`` on the immediately preceding ``AIMessage``
  (no orphaned result → ``toolResult blocks … exceeds … toolUse blocks``), and
* every ``tool_call`` has exactly one result (no dangling call → ``tool_use`` with no
  ``tool_result``).
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from langchain.agents.factory import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from agent_common.middleware.loop_detection_middleware import RepeatedToolCallMiddleware


def assert_tool_pairing_valid(messages: list[BaseMessage]) -> None:
    """Assert the message list satisfies the provider's tool-call/result contract.

    This is the offline proxy for what Bedrock enforces; a list that fails here is one the
    gateway rejects with a hard 400, permanently, because it lives in the checkpoint.
    """
    answered: dict[str, int] = {}
    open_calls: dict[str, str] = {}

    for position, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in open_calls, (
                f"orphaned tool result at index {position}: tool_call_id "
                f"{message.tool_call_id!r} matches no tool call on the preceding AIMessage"
            )
            answered[message.tool_call_id] = answered.get(message.tool_call_id, 0) + 1
            assert answered[message.tool_call_id] == 1, (
                f"duplicate tool result at index {position} for {message.tool_call_id!r}"
            )
        elif isinstance(message, AIMessage):
            unanswered = [cid for cid in open_calls if cid not in answered]
            assert not unanswered, (
                f"dangling tool call(s) {unanswered} left unanswered before the AIMessage at index {position}"
            )
            open_calls = {tc["id"]: tc["name"] for tc in message.tool_calls if tc.get("id")}

    unanswered = [cid for cid in open_calls if cid not in answered]
    assert not unanswered, f"dangling tool call(s) {unanswered} at the end of the conversation"


class _SearchArgs(BaseModel):
    q: str = Field(description="query")


def _make_search_tool() -> StructuredTool:
    def _search(q: str) -> str:
        return "no results"

    return StructuredTool.from_function(func=_search, name="search", description="search", args_schema=_SearchArgs)


class _LoopingModel(BaseChatModel):
    """A model that keeps making the same tool call until told to stop.

    Records every message list it is handed, so a test can assert on what would actually
    have gone to the provider rather than only on what was persisted.
    """

    calls: int = 0
    stop_looping: bool = False
    seen_requests: list = []

    @property
    def _llm_type(self) -> str:
        return "looping"

    def bind_tools(self, tools: list, **kwargs: Any) -> "_LoopingModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen_requests.append(list(messages))
        self.calls += 1
        # A runaway loop would hang the suite; fail loudly instead.
        assert self.calls < 25, "force-stop never ended the run"

        if self.stop_looping:
            message: BaseMessage = AIMessage(content="Here is the answer.")
        else:
            message = AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {"q": "always the same"}, "id": f"call-{self.calls}"}],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _build_agent(model: _LoopingModel):
    return create_agent(
        model=model,
        tools=[_make_search_tool()],
        middleware=[
            RepeatedToolCallMiddleware(
                max_repeats=1,
                window_size=10,
                # Block on the 2nd identical call, force-stop on the 3rd.
                force_stop_after=2,
            )
        ],
        checkpointer=InMemorySaver(),
    )


@pytest.mark.asyncio
async def test_force_stop_ends_the_run_and_leaves_a_valid_checkpoint():
    model = _LoopingModel(seen_requests=[])
    agent = _build_agent(model)
    config = {"configurable": {"thread_id": "loop-1"}}

    result = await agent.ainvoke({"messages": [HumanMessage(content="search for it")]}, config)

    # The run terminated on its own — jump_to: "end" really does end the assembled graph,
    # not just return a state update the unit tests can inspect.
    assert model.calls < 25

    messages = result["messages"]
    assert_tool_pairing_valid(messages)

    # The looping call was stopped, and stopping it did not leave it unanswered.
    last_ai = next(m for m in reversed(messages) if isinstance(m, AIMessage))
    assert last_ai.tool_calls, "the AIMessage must keep its tool calls — rewriting it is what orphans results"
    blocked = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
    assert blocked, "the blocked call must be answered, not left dangling"
    assert blocked[-1].tool_call_id == last_ai.tool_calls[0]["id"]


@pytest.mark.asyncio
async def test_conversation_resumes_cleanly_after_a_force_stop():
    """The regression that bricked threads: the turn *after* the stop.

    A force-stopped turn is only harmless if the next turn can still be sent.

    Note this covers resume for a *plain* tool loop, where the stopped call never produced
    a result — that shape survives the old stripping behaviour too, so this test alone does
    not discriminate between old and new. ``test_structured_output_turns_never_corrupt_the_checkpoint``
    is the discriminating one: there the result exists before the stop.
    """
    model = _LoopingModel(seen_requests=[])
    agent = _build_agent(model)
    config = {"configurable": {"thread_id": "loop-2"}}

    await agent.ainvoke({"messages": [HumanMessage(content="search for it")]}, config)
    calls_after_first_turn = model.calls

    # Same thread: the force-stopped history is replayed from the checkpoint.
    model.stop_looping = True
    result = await agent.ainvoke({"messages": [HumanMessage(content="never mind, just answer")]}, config)

    assert model.calls > calls_after_first_turn, "the resumed turn never reached the model"

    # What the model was handed on the resumed turn is what the provider would have seen.
    assert_tool_pairing_valid(model.seen_requests[-1])
    assert_tool_pairing_valid(result["messages"])

    # And the turn produced a normal answer rather than dying on the replayed history.
    assert result["messages"][-1].content == "Here is the answer."


@pytest.mark.asyncio
async def test_pairing_helper_catches_both_failure_shapes():
    """Guard the guard: the invariant must actually reject the two bad shapes."""
    orphan = [
        AIMessage(content="", id="ai", tool_calls=[]),
        ToolMessage(content="result", tool_call_id="gone", name="search"),
    ]
    with pytest.raises(AssertionError, match="orphaned tool result"):
        assert_tool_pairing_valid(orphan)

    dangling = [
        AIMessage(content="", id="ai", tool_calls=[{"name": "search", "args": {}, "id": "unanswered"}]),
    ]
    with pytest.raises(AssertionError, match="dangling tool call"):
        assert_tool_pairing_valid(dangling)


class AnswerSchema(BaseModel):
    """Terminal response schema, under a name the exemption does not cover.

    Deliberately not called ``FinalResponseSchema``: this exercises the force-stop path
    itself, independently of which tool names happen to be exempt from loop detection.
    """

    task_state: str = Field(description="state")
    message: str = Field(description="answer")


class _StructuredOutputModel(BaseChatModel):
    """Always answers by calling the response schema with identical arguments.

    This is the production shape. With a ``ToolStrategy`` response format the model node
    returns ``{"messages": [AIMessage, ToolMessage]}`` — the call *and* its result in one
    state update — so by the time ``after_model`` runs, the result already exists. That is
    what made stripping ``tool_calls`` corrupt the checkpoint.
    """

    calls: int = 0
    seen_requests: list = []
    schema_name: str = "AnswerSchema"

    @property
    def _llm_type(self) -> str:
        return "structured"

    def bind_tools(self, tools: list, **kwargs: Any) -> "_StructuredOutputModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen_requests.append(list(messages))
        self.calls += 1
        assert self.calls < 25, "the run never terminated"
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": self.schema_name,
                                # Byte-identical every turn, exactly like the sub-agent
                                # pass-through variant in production.
                                "args": {"task_state": "completed", "message": ""},
                                "id": f"answer-{self.calls}",
                            }
                        ],
                    )
                )
            ]
        )


class FinalResponseSchema(BaseModel):
    """The real terminal response schema name, which ``RESPONSE_TOOLS`` exempts."""

    task_state: str = Field(description="state")
    message: str = Field(description="answer")


def _build_structured_agent(model: _StructuredOutputModel, *, schema: type[BaseModel] = AnswerSchema):
    return create_agent(
        model=model,
        tools=[],
        response_format=ToolStrategy(schema),
        middleware=[
            RepeatedToolCallMiddleware(
                max_repeats=1,
                window_size=10,
                force_stop_after=2,
            )
        ],
        checkpointer=InMemorySaver(),
    )


@pytest.mark.asyncio
async def test_structured_output_turns_never_corrupt_the_checkpoint():
    """The production path, reproduced: repeated turns that each end in the response tool.

    ``tool_call_history`` is cumulative across turns, so turn 2 is blocked and turn 3
    force-stops — the same ladder the failing thread walked, in three turns instead of
    thirteen. Before the fix, turn 2 appended a second result for a call that already had
    one, and turn 3 stripped the call its result belonged to; from then on every turn
    replayed an illegal history.
    """
    model = _StructuredOutputModel(seen_requests=[])
    agent = _build_structured_agent(model)
    config = {"configurable": {"thread_id": "structured-1"}}

    for turn in range(4):
        result = await agent.ainvoke({"messages": [HumanMessage(content=f"question {turn}")]}, config)
        # What the model was handed is what the provider would have received.
        assert_tool_pairing_valid(model.seen_requests[-1])
        assert_tool_pairing_valid(result["messages"])

    # Four turns actually happened — the conversation never got stuck or bricked.
    assert model.calls >= 4


@pytest.mark.asyncio
async def test_response_tool_is_never_force_stopped():
    """The exemption, end to end: the terminal response tool is not a loop candidate."""
    model = _StructuredOutputModel(seen_requests=[], schema_name="FinalResponseSchema")
    agent = _build_structured_agent(model, schema=FinalResponseSchema)
    config = {"configurable": {"thread_id": "structured-2"}}

    for turn in range(6):
        result = await agent.ainvoke({"messages": [HumanMessage(content=f"question {turn}")]}, config)
        assert_tool_pairing_valid(result["messages"])

    # No BLOCKED result was ever produced for the response tool.
    blocked = [
        m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content.startswith("BLOCKED:")
    ]
    assert not blocked, "the terminal response tool must never be blocked as a loop"
