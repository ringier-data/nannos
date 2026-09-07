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
    gateway rejects with a hard 400, permanently, because it lives in the checkpoint. The
    rule is per *turn*, not global: results must follow their ``AIMessage`` immediately, so
    anything else appearing in between (a ``HumanMessage``, say) closes the group — a
    result arriving after that answers nothing, and a call left open is dangling.
    """
    open_calls: set[str] = set()
    answered: set[str] = set()
    opened_at = 0

    def _close(position: int) -> None:
        unanswered = sorted(open_calls - answered)
        assert not unanswered, (
            f"dangling tool call(s) {unanswered} from the AIMessage at index {opened_at}: "
            f"no result before index {position}"
        )

    for position, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in open_calls, (
                f"orphaned tool result at index {position}: tool_call_id "
                f"{message.tool_call_id!r} matches no tool call on the preceding AIMessage"
            )
            assert message.tool_call_id not in answered, (
                f"duplicate tool result at index {position} for {message.tool_call_id!r}"
            )
            answered.add(message.tool_call_id)
            continue

        _close(position)
        # Any non-result message ends the group; only an AIMessage opens a new one. Ids are
        # tracked per group rather than globally, so the check does not rely on tool_call_id
        # being unique across the whole conversation.
        open_calls, answered = set(), set()
        if isinstance(message, AIMessage) and message.tool_calls:
            open_calls = {tc["id"] for tc in message.tool_calls if tc.get("id")}
            opened_at = position

    _close(len(messages))


class _SearchArgs(BaseModel):
    q: str = Field(description="query")


def _make_search_tool(executions: list | None = None) -> StructuredTool:
    def _search(q: str) -> str:
        if executions is not None:
            executions.append(q)
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


def _build_agent(model: _LoopingModel, executions: list | None = None):
    return create_agent(
        model=model,
        tools=[_make_search_tool(executions)],
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

    # The provider's rule is per turn: the result must directly follow its call.
    separated = [
        AIMessage(content="", id="ai", tool_calls=[{"name": "search", "args": {}, "id": "late"}]),
        HumanMessage(content="actually, never mind"),
        ToolMessage(content="result", tool_call_id="late", name="search"),
    ]
    with pytest.raises(AssertionError, match="dangling tool call"):
        assert_tool_pairing_valid(separated)

    # Ids are scoped per turn, so reuse across turns is not a duplicate.
    reused = [
        AIMessage(content="", id="ai1", tool_calls=[{"name": "search", "args": {}, "id": "same"}]),
        ToolMessage(content="result", tool_call_id="same", name="search"),
        HumanMessage(content="again"),
        AIMessage(content="", id="ai2", tool_calls=[{"name": "search", "args": {}, "id": "same"}]),
        ToolMessage(content="result", tool_call_id="same", name="search"),
    ]
    assert_tool_pairing_valid(reused)


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


@pytest.mark.asyncio
async def test_blocking_still_fires_on_the_turn_after_a_force_stop():
    """The counter must survive the stop — otherwise the next turn runs the loop again.

    ``tool_call_history`` is private state carried in the checkpoint. If a force-stop lost
    it (or reset it), the very next turn would start from zero: the looping call would be
    executed again and the user would be back where they started, one turn later. So the
    check is behavioural — on the turn after a stop, the same call must be refused
    *without* the tool running.
    """
    executions: list = []
    model = _LoopingModel(seen_requests=[])
    agent = _build_agent(model, executions)
    config = {"configurable": {"thread_id": "loop-3"}}

    await agent.ainvoke({"messages": [HumanMessage(content="search for it")]}, config)
    executions_after_first_turn = len(executions)
    assert executions_after_first_turn, "the tool should have run before the loop was detected"

    # Same thread, same looping model: the model asks for the identical call again.
    result = await agent.ainvoke({"messages": [HumanMessage(content="try again")]}, config)

    # Refused on the strength of history carried over from the previous turn — the gate
    # still holds, and the tool did not execute a single further time.
    assert len(executions) == executions_after_first_turn, (
        "the looping tool ran again after the force-stop — the repeat counter was lost"
    )

    last_ai = next(m for m in reversed(result["messages"]) if isinstance(m, AIMessage))
    refusal = next(m for m in reversed(result["messages"]) if isinstance(m, ToolMessage))
    assert refusal.tool_call_id == last_ai.tool_calls[0]["id"]
    assert refusal.status == "error"
    assert_tool_pairing_valid(result["messages"])


@pytest.mark.asyncio
async def test_do_not_retry_guidance_reaches_the_model_on_the_next_turn():
    """The refusal is only useful if the model can still read it when it next runs.

    The BLOCKED result is what tells the model to stop retrying and answer with what it
    has. It is written into the checkpoint on the stopped turn, so the turn after must
    replay it verbatim — if it were dropped or rewritten, the model would be refused with
    no idea why, and would simply try again.
    """
    model = _LoopingModel(seen_requests=[])
    agent = _build_agent(model)
    config = {"configurable": {"thread_id": "loop-4"}}

    await agent.ainvoke({"messages": [HumanMessage(content="search for it")]}, config)

    model.stop_looping = True
    await agent.ainvoke({"messages": [HumanMessage(content="never mind")]}, config)

    replayed = model.seen_requests[-1]
    guidance = [
        m.content
        for m in replayed
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content.startswith("BLOCKED:")
    ]
    assert guidance, "the model was refused on the previous turn but sees no explanation now"
    assert any("Do NOT retry with the same arguments" in text for text in guidance)
    assert any("respond to the user with what you have so far" in text for text in guidance)

    # Specifically the *force-stopped* call must carry it. The stopped call used to get no
    # result at all, so the model was left with a bare unanswered call and no reason.
    stopped_call = next(m.tool_calls[0]["id"] for m in reversed(replayed) if isinstance(m, AIMessage) and m.tool_calls)
    stopped_result = next(m for m in replayed if isinstance(m, ToolMessage) and m.tool_call_id == stopped_call)
    assert stopped_result.content.startswith("BLOCKED:")
    assert "Do NOT retry with the same arguments" in stopped_result.content
