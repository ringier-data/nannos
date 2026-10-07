"""Graph-level: a turn whose tool calls the HITL middleware answers itself reaches the model.

The middleware answers a call in place in three cases — an approved ``client_action``
the browser already executed (the one-pause shortcut), a rejection, and a corrective
answer. Every call then has its ToolMessage and the tools node has nothing to do, so
langchain's model→tools edge falls through to "a structured response exists → end".
On the FIRST turn of a conversation there is none and the edge goes back to the model.
On every later turn the checkpoint still holds the previous turn's structured response:
the run ended without the model reading the answers, and the stream replayed the
previous turn's reply. Only a real graph with a real checkpointer shows it.
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
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent_common.middleware.conditional_hitl import ConditionalHumanInTheLoopMiddleware, _answered


class Answer(BaseModel):
    text: str = Field(description="the reply")


class _ClientActionArgs(BaseModel):
    kind: str = Field(description="kind")
    target_type: str = Field(description="type")
    target_id: str = Field(description="id")


class _ScriptedModel(BaseChatModel):
    """Calls ``client_action`` on a user message; answers once it has read a tool result."""

    calls: int = 0
    turn: int = 0

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
        self.calls += 1
        assert self.calls < 20, "runaway loop"
        if isinstance(messages[-1], HumanMessage):
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "client_action",
                        "args": {"kind": "submit", "target_type": "Settings", "target_id": "me"},
                        "id": f"ca-{self.turn}",
                    }
                ],
            )
        else:
            message = AIMessage(
                content="",
                tool_calls=[{"name": "Answer", "args": {"text": f"turn {self.turn} done"}, "id": f"ans-{self.turn}"}],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _build(model: _ScriptedModel, executions: list):
    def _client_action(kind: str, target_type: str, target_id: str) -> str:
        executions.append(kind)
        return "ran"

    tool = StructuredTool.from_function(
        func=_client_action, name="client_action", description="act", args_schema=_ClientActionArgs
    )
    return create_agent(
        model=model,
        tools=[tool],
        middleware=[ConditionalHumanInTheLoopMiddleware(interrupt_on={"client_action": True})],
        response_format=ToolStrategy(Answer),
        checkpointer=InMemorySaver(),
    )


async def _turn(agent, model: _ScriptedModel, config: dict, turn: int, decision: dict) -> dict:
    model.turn = turn
    first = await agent.ainvoke({"messages": [HumanMessage(content=f"save it ({turn})")]}, config)
    assert "__interrupt__" in first, "the client_action must stop for approval"
    return await agent.ainvoke(Command(resume={"decisions": [decision]}), config)


APPROVED_BY_BROWSER = {"type": "approve", "client_action_result": {"ok": True}}
REJECTED = {"type": "reject", "message": "not now"}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [APPROVED_BY_BROWSER, REJECTED], ids=["shortcut", "reject"])
async def test_every_turn_reads_its_own_answered_calls(decision):
    model = _ScriptedModel()
    executions: list = []
    agent = _build(model, executions)
    config = {"configurable": {"thread_id": f"answered-{decision['type']}"}}

    first = await _turn(agent, model, config, 1, decision)
    assert first["structured_response"].text == "turn 1 done"

    calls_before = model.calls
    second = await _turn(agent, model, config, 2, decision)

    # The model read the second turn's answer instead of the run ending on the stale
    # structured response the first turn left in the checkpoint.
    assert model.calls > calls_before + 1, "the resumed second turn never reached the model"
    assert second["structured_response"].text == "turn 2 done"
    # The shortcut answers the call; the tool itself never runs (no second pause).
    assert executions == []
    answered = [m for m in second["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "ca-2"]
    assert len(answered) == 1


def test_answered_jumps_only_when_nothing_is_left_for_the_tools_node():
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "a", "args": {}, "id": "1"},
            {"name": "b", "args": {}, "id": "2"},
        ],
    )
    one = ToolMessage(content="x", tool_call_id="1")
    two = ToolMessage(content="y", tool_call_id="2")
    assert "jump_to" not in _answered(ai, [one])
    assert _answered(ai, [one, two])["jump_to"] == "model"
    assert "jump_to" not in _answered(ai, [])


class _ApplyAndSubmitModel(_ScriptedModel):
    """Sends the fill and the save in ONE step first; alone once told to."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        assert self.calls < 20, "runaway loop"
        last = messages[-1]
        if isinstance(last, HumanMessage):
            calls = [
                {"name": "client_action", "args": {"kind": "apply", "target_type": "S", "target_id": "1"}, "id": "fill"},
                {"name": "client_action", "args": {"kind": "submit", "target_type": "S", "target_id": "1"}, "id": "save"},
            ]
        elif isinstance(last, ToolMessage) and last.tool_call_id in ("fill", "save"):
            calls = [{"name": "client_action", "args": {"kind": "submit", "target_type": "S", "target_id": "1"}, "id": "save-2"}]
        else:
            calls = [{"name": "Answer", "args": {"text": "saved"}, "id": "ans"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])


@pytest.mark.asyncio
async def test_a_save_sent_with_its_fill_waits_for_the_fill():
    model = _ApplyAndSubmitModel()
    executions: list = []
    agent = _build(model, executions)
    config = {"configurable": {"thread_id": "fill-and-save"}}

    first = await agent.ainvoke({"messages": [HumanMessage(content="fill and save")]}, config)
    # The save sharing the step was answered, not put before the user: only the
    # fill (gated here by the static rule) is asking.
    requests = first["__interrupt__"][0].value["action_requests"]
    assert [r["args"]["kind"] for r in requests] == ["apply"]

    second = await agent.ainvoke(Command(resume={"decisions": [APPROVED_BY_BROWSER]}), config)
    # The model was told the save did not run, and why.
    refused = [m for m in second["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "save"]
    assert refused and "NOT RUN" in refused[0].content and refused[0].status == "error"
    # Next step: the save alone, asking for approval on its own.
    requests = second["__interrupt__"][0].value["action_requests"]
    assert [(r["args"]["kind"], r["args"]["_call_id"]) for r in requests] == [("submit", "save-2")]


class _InvokeArgs(_ClientActionArgs):
    action: str | None = Field(default=None, description="action")


class _InvokeAndFillModel(_ScriptedModel):
    """Enters edit mode, fills and points at a field in ONE step; fills alone once told to."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        assert self.calls < 20, "runaway loop"
        last = messages[-1]
        target = {"target_type": "S", "target_id": "1"}
        if isinstance(last, HumanMessage):
            calls = [
                {"name": "client_action", "args": {"kind": "invoke", "action": "edit", **target}, "id": "edit"},
                {"name": "client_action", "args": {"kind": "apply", **target}, "id": "fill"},
                {"name": "client_action", "args": {"kind": "highlight", **target}, "id": "point"},
            ]
        elif isinstance(last, ToolMessage) and last.tool_call_id in ("edit", "fill", "point"):
            calls = [{"name": "client_action", "args": {"kind": "apply", **target}, "id": "fill-2"}]
        else:
            calls = [{"name": "Answer", "args": {"text": "filled"}, "id": "ans"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])


@pytest.mark.asyncio
async def test_a_fill_sent_with_an_invoke_waits_for_the_invoke():
    def _client_action(kind: str, target_type: str, target_id: str, action: str | None = None) -> str:
        return "ran"

    tool = StructuredTool.from_function(
        func=_client_action, name="client_action", description="act", args_schema=_InvokeArgs
    )
    agent = create_agent(
        model=_InvokeAndFillModel(),
        tools=[tool],
        middleware=[ConditionalHumanInTheLoopMiddleware(interrupt_on={"client_action": True})],
        response_format=ToolStrategy(Answer),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "invoke-and-fill"}}

    first = await agent.ainvoke({"messages": [HumanMessage(content="edit and fill")]}, config)
    # Only the invoke is put before the user; its siblings were answered.
    requests = first["__interrupt__"][0].value["action_requests"]
    assert [r["args"]["kind"] for r in requests] == ["invoke"]

    second = await agent.ainvoke(Command(resume={"decisions": [APPROVED_BY_BROWSER]}), config)
    for call_id, kind in (("fill", "apply"), ("point", "highlight")):
        refused = [m for m in second["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == call_id]
        assert refused and refused[0].status == "error"
        assert refused[0].content.startswith(f"NOT RUN: {kind} was sent together with invoke 'edit'")
        assert f"then {kind} in the next step" in refused[0].content
    # The invoke itself ran (answered by the browser), and the fill comes next, alone.
    ran = [m for m in second["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "edit"]
    assert ran and "NOT RUN" not in ran[0].content
    requests = second["__interrupt__"][0].value["action_requests"]
    assert [(r["args"]["kind"], r["args"]["_call_id"]) for r in requests] == [("apply", "fill-2")]


# --------------------------------------------------------- premature final response


class _AnswersTooEarlyModel(_ScriptedModel):
    """Announces its final answer in the same message as the tool call."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        assert self.calls < 20, "runaway loop"
        if isinstance(messages[-1], HumanMessage):
            calls = [
                {"name": "lookup", "args": {"q": "x"}, "id": "look"},
                {"name": "Answer", "args": {"text": "premature"}, "id": "early"},
            ]
        else:
            calls = [{"name": "Answer", "args": {"text": f"read: {messages[-1].content}"}, "id": "late"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])


class _LookupArgs(BaseModel):
    q: str = Field(description="q")


@pytest.mark.asyncio
async def test_a_final_response_next_to_a_tool_call_waits_for_its_result():
    from agent_common.middleware.premature_final_response import PrematureFinalResponseMiddleware

    executions: list = []

    def _lookup(q: str) -> str:
        executions.append(q)
        return "the result"

    agent = create_agent(
        model=_AnswersTooEarlyModel(),
        tools=[StructuredTool.from_function(func=_lookup, name="lookup", description="l", args_schema=_LookupArgs)],
        middleware=[PrematureFinalResponseMiddleware()],
        response_format=ToolStrategy(Answer),
        checkpointer=InMemorySaver(),
    )
    out = await agent.ainvoke({"messages": [HumanMessage(content="go")]}, {"configurable": {"thread_id": "early"}})

    assert executions == ["x"]
    # The model answered after reading the tool's result, not before it.
    assert out["structured_response"].text == "read: the result"
    assert_no_orphans = [m for m in out["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "early"]
    assert assert_no_orphans == []


# --------------------------------------------------------- a refused call, retried


class _RetriesARefusalModel(_ScriptedModel):
    """Re-sends the very save the user just refused, as the live agent did."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        assert self.calls < 20, "runaway loop"
        last = messages[-1]
        save = {"kind": "submit", "target_type": "BudgetGuard", "target_id": "settings"}
        if isinstance(last, HumanMessage) or (isinstance(last, ToolMessage) and "NOT RUN" not in last.content):
            calls = [{"name": "client_action", "args": save, "id": f"save-{self.calls}"}]
        else:
            calls = [{"name": "Answer", "args": {"text": "not saved"}, "id": f"ans-{self.calls}"}]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])


@pytest.mark.asyncio
async def test_a_refused_call_is_not_put_to_the_user_again_in_the_same_turn():
    executions: list = []
    agent = _build(_RetriesARefusalModel(), executions)
    config = {"configurable": {"thread_id": "refused-retry"}}

    first = await agent.ainvoke({"messages": [HumanMessage(content="save it")]}, config)
    assert "__interrupt__" in first
    from agent_common.core.hitl_resume import structural_decisions

    clicked = structural_decisions({"decisions": [{"type": "reject"}]}, [{"name": "client_action"}])
    second = await agent.ainvoke(Command(resume={"decisions": clicked}), config)

    # No second card: the identical retry was answered in place.
    assert "__interrupt__" not in second
    assert second["structured_response"].text == "not saved"
    retried = [m for m in second["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "save-2"]
    assert retried and retried[0].content.startswith("NOT RUN: the user rejected this exact call")
    assert executions == []

    # A new message from the user may ask for it after all: that is asked again.
    third = await agent.ainvoke({"messages": [HumanMessage(content="ok, save it now")]}, config)
    assert "__interrupt__" in third
