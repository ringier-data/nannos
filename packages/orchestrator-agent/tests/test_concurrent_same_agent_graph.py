"""The concurrency guard inside a real graph, not a hand-built request.

``AGENTS.md`` (§Testing) asks for real graph execution in middleware integration
tests, and it earns its keep here twice over:

- the guard reads ``request.runtime.state``, so a unit test that hand-builds that
  dict cannot notice the real state drifting away from the assumed shape, nor the
  guard sitting at the wrong point in the middleware chain;
- the whole tag design rests on the refusal landing *after* the owner's result
  (parallel siblings are written in ``tool_calls`` order). That ordering is
  LangGraph's, not ours, so it is asserted against the real thing.

The sub-agent's internals are still stubbed — ``_adispatch_task_tool`` stands in
for a dispatch that would otherwise need a checkpointer, a runnable protocol and
an LLM round trip for the human-message build. Everything up to and including the
guard is the production path.
"""

from collections import deque
from typing import Any, Optional

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
from app.middleware.task_refusal import is_concurrent_task_refusal
from app.models.config import GraphRuntimeContext
from langchain.agents.factory import create_agent

AGENT = "github-agent"


@tool
def task(description: str, subagent_type: str) -> str:
    """Launch a sub-agent (stand-in for the deepagents tool; never executed here)."""
    return "unreachable: DynamicToolDispatchMiddleware intercepts every task call"


class ScriptedModel(BaseChatModel):
    """Emits pre-baked assistant messages, one per model call."""

    responses: deque = deque()

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.responses.popleft())])


def _two_same_agent_calls() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"subagent_type": AGENT, "description": "who am I on GitHub"},
                "id": "call_owner",
                "type": "tool_call",
            },
            {
                "name": "task",
                "args": {"subagent_type": AGENT, "description": "list my repos"},
                "id": "call_second",
                "type": "tool_call",
            },
        ],
    )


@pytest.fixture
def dispatched(monkeypatch) -> list[str]:
    """Records every call that reached dispatch, standing in for the sub-agent."""
    seen: list[str] = []

    async def _fake_dispatch(self, tool_call, user_context, state, config, stream_writer=None):
        seen.append(tool_call["id"])
        return ToolMessage(
            content="You are aartaria (GitHub ID 10273710).",
            name="task",
            tool_call_id=tool_call["id"],
        )

    monkeypatch.setattr(DynamicToolDispatchMiddleware, "_adispatch_task_tool", _fake_dispatch)
    return seen


def _build_agent():
    model = ScriptedModel()
    model.responses = deque([_two_same_agent_calls(), AIMessage(content="Done.")])
    return create_agent(
        model=model,
        tools=[task],
        middleware=[DynamicToolDispatchMiddleware()],
        context_schema=GraphRuntimeContext,
    )


def _context() -> GraphRuntimeContext:
    return GraphRuntimeContext(
        user_id="u1",
        user_sub="sub1",
        name="Test",
        email="test@example.com",
        subagent_registry={AGENT: {"description": "GitHub agent"}},
    )


@pytest.mark.asyncio
async def test_only_one_sibling_reaches_dispatch(dispatched):
    result = await _build_agent().ainvoke(
        {"messages": [HumanMessage(content="who am I, and list my repos")]},
        context=_context(),
    )

    assert dispatched == ["call_owner"], "the second sibling must never reach the sub-agent"

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2, "both calls must still be answered — an unanswered call breaks the history"
    by_id = {m.tool_call_id: m for m in tool_messages}
    assert "aartaria" in by_id["call_owner"].content
    assert "Not executed" in by_id["call_second"].content


@pytest.mark.asyncio
async def test_refusal_is_tagged_and_lands_after_the_owner(dispatched):
    """The ordering every downstream "latest task result" reader depends on."""
    result = await _build_agent().ainvoke(
        {"messages": [HumanMessage(content="who am I, and list my repos")]},
        context=_context(),
    )

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["call_owner", "call_second"]
    assert not is_concurrent_task_refusal(tool_messages[0])
    assert is_concurrent_task_refusal(tool_messages[1])
