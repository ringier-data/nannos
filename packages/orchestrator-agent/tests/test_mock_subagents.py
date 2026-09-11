"""Mock sub-agents must be dispatchable through the real orchestrator path.

Routing tests are only worth anything if the mock travels the same code as a
real sub-agent. These drive ``DynamicToolDispatchMiddleware._adispatch_task_tool``
directly — registry lookup, ``astream``, A2A metadata wrapping, Command
construction — with no model and no credentials, so they run in the normal
suite alongside everything else.
"""

from __future__ import annotations

import pytest
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.stream_events import ErrorEvent, TaskUpdate
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import SecretStr, ValidationError

from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
from app.models.config import GraphRuntimeContext, UserConfig
from app.utils import build_runtime_context
from tests.support.extraction import delegated_agents, delegations
from tests.support.mock_subagents import MockSubAgent, mock_subagents


def _context(*agents: MockSubAgent) -> GraphRuntimeContext:
    return GraphRuntimeContext(
        user_id="test-user",
        user_sub="test-sub",
        name="Test User",
        email="test@local",
        subagent_registry={a["name"]: a for a in mock_subagents(*agents)},
    )


def _task_call(subagent: str, description: str = "do the thing", call_id: str = "c1") -> dict:
    return {
        "id": call_id,
        "name": "task",
        "args": {"subagent_type": subagent, "description": description},
        "type": "tool_call",
    }


async def _dispatch(context: GraphRuntimeContext, tool_call: dict):
    middleware = DynamicToolDispatchMiddleware()
    return await middleware._adispatch_task_tool(
        tool_call,
        context,
        {"messages": []},
        {"configurable": {"thread_id": "test-thread"}},
    )


def _tool_message(result) -> ToolMessage:
    """Pull the ToolMessage out of whatever dispatch returned."""
    if isinstance(result, Command):
        messages = result.update.get("messages", [])
        assert messages, f"Command carried no messages: {result}"
        return messages[-1]
    assert isinstance(result, ToolMessage), f"unexpected dispatch result: {type(result)}"
    return result


# ---------------------------------------------------------------------------
# Registration via the real context builder
# ---------------------------------------------------------------------------


def test_build_runtime_context_registers_mocks_alongside_builtins():
    """The blocker this module exists to clear.

    Mocks must arrive in ``subagent_registry`` through the same
    ``build_runtime_context`` call production uses — not by hand-building the
    context — or the test proves nothing about routing.

    Note the assignment style: ``UserConfig.sub_agents`` is annotated
    ``list[CompiledSubAgent]`` whose ``runnable`` is a ``Runnable``, but no A2A
    runnable actually subclasses ``Runnable``. Production gets away with it
    because executor.py:311 *assigns* after construction and the model has no
    ``validate_assignment``. Passing them to the constructor instead raises.
    """
    slack = MockSubAgent("slack-notifier", "Sends Slack messages.")
    runner = MockSubAgent("revenue-analyst", "Runs data queries.")

    user_config = UserConfig(
        user_id="test-user",
        user_sub="test-sub",
        access_token=SecretStr("test-token"),
        name="Test User",
        email="test@local",
        language="en",
        timezone="Europe/Zurich",
        model=None,
        message_formatting="markdown",
        tools=[],
        sub_agents=[],
        local_subagents=[],
    )
    user_config.sub_agents = mock_subagents(slack, runner)

    registry = build_runtime_context(user_config).subagent_registry

    assert {"slack-notifier", "revenue-analyst"} <= set(registry)
    assert registry["slack-notifier"]["runnable"] is slack
    # file-analyzer is registered unconditionally as a built-in capability.
    assert "file-analyzer" in registry


def test_constructor_assignment_of_subagents_is_rejected_by_pydantic():
    """Pins the constructor/assignment asymmetry so the workaround isn't 'fixed' away."""
    with pytest.raises(ValidationError):
        UserConfig(
            user_id="test-user",
            user_sub="test-sub",
            access_token=SecretStr("test-token"),
            name="Test User",
            email="test@local",
            sub_agents=mock_subagents(MockSubAgent("slack-notifier")),
        )


# ---------------------------------------------------------------------------
# Registration and dispatch
# ---------------------------------------------------------------------------


async def test_registered_mock_receives_the_instruction_and_reply_reaches_orchestrator():
    slack = MockSubAgent("slack-notifier", "Sends Slack messages.", reply="posted to #general")

    result = await _dispatch(_context(slack), _task_call("slack-notifier", "post the Q3 summary to @john.doe"))

    assert slack.call_count == 1
    assert slack.called_with_substring("@john.doe")
    assert "posted to #general" in _tool_message(result).content


async def test_unregistered_subagent_returns_none_to_allow_builtin_fallback():
    """None is the signal that lets SubAgentMiddleware handle general-purpose."""
    result = await _dispatch(_context(MockSubAgent("slack-notifier")), _task_call("nonexistent-agent"))

    assert result is None


async def test_each_mock_only_sees_its_own_delegations():
    slack = MockSubAgent("slack-notifier", "Sends Slack messages.")
    runner = MockSubAgent("revenue-analyst", "Runs data queries.")
    context = _context(slack, runner)

    await _dispatch(context, _task_call("revenue-analyst", "fetch Q3 earnings"))

    assert runner.called_with_substring("Q3 earnings")
    assert not slack.called


async def test_reply_can_depend_on_the_instruction():
    echo = MockSubAgent("revenue-analyst", reply=lambda instruction: f"received: {instruction}")

    result = await _dispatch(_context(echo), _task_call("revenue-analyst", "count the rows"))

    assert "received: count the rows" in _tool_message(result).content


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_subagent_error_surfaces_without_raising():
    """A failing sub-agent must come back as content, not blow up the turn."""
    broken = MockSubAgent("slack-notifier", error="slack API returned 503")

    result = await _dispatch(_context(broken), _task_call("slack-notifier"))

    assert "slack API returned 503" in _tool_message(result).content


async def test_subagent_input_required_reaches_the_orchestrator():
    """The question a sub-agent stopped to ask must arrive intact.

    `input_required` is the state the orchestrator is supposed to *relay* rather
    than answer, so the text is the payload — a state with no question in it
    leaves the model nothing to pass on.
    """
    stalled = MockSubAgent("slack-notifier", input_required="which channel? #team-eng or #team-ops")

    result = await _dispatch(_context(stalled), _task_call("slack-notifier"))

    assert "which channel?" in _tool_message(result).content


def test_a_subagent_cannot_both_fail_and_ask_for_input():
    """Two terminal states are a scenario bug, and a silent precedence rule in
    `_process` would decide it invisibly. Refuse at construction instead."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        MockSubAgent("slack-notifier", error="boom", input_required="which channel?")


@pytest.mark.parametrize(
    "outcome",
    [{"error": "boom"}, {"input_required": "which channel?"}, {"reply": "done"}],
    ids=["failed", "input_required", "completed"],
)
async def test_every_terminal_state_reuses_the_orchestrator_context_id(outcome):
    """`a2a_tracking` correlates a sub-agent by context_id, so a non-success
    response that mints a fresh one is untrackable across turns — which is
    exactly the case a multi-turn failure test would need to follow."""
    agent = MockSubAgent("slack-notifier", **outcome)

    response = await agent._process(
        SubAgentInput(messages=[HumanMessage("go")], orchestrator_conversation_id="ctx-42"),
        {"configurable": {"thread_id": "test-thread"}},
    )

    assert response.context_id == "ctx-42"


# ---------------------------------------------------------------------------
# The runnable contract itself
# ---------------------------------------------------------------------------


async def test_astream_yields_a_terminal_task_update():
    """`astream` comes from LocalA2ARunnable — this pins that we satisfy it."""
    agent = MockSubAgent("revenue-analyst", reply="done")

    events = [
        event
        async for event in agent.astream(
            {"messages": [{"role": "user", "content": "go"}], "orchestrator_conversation_id": "ctx-1"},
            {"configurable": {"thread_id": "test-thread"}},
        )
    ]

    assert events, "mock produced no stream events"
    assert isinstance(events[-1], TaskUpdate)


async def test_astream_without_parent_config_is_rejected():
    """LocalA2ARunnable refuses to run unparented — a mock must not paper over it.

    Missing config means the sub-agent would fall back to wrong user_id /
    assistant_id values, so the base class treats it as a programming error.
    """
    agent = MockSubAgent("revenue-analyst")

    events = [event async for event in agent.astream({"messages": [{"role": "user", "content": "go"}]})]

    assert isinstance(events[-1], ErrorEvent)
    assert "requires parent config" in events[-1].error


async def test_input_modes_default_to_text_only():
    """Text-only keeps dispatch off the multimodal path, which needs an LLM."""
    assert MockSubAgent("revenue-analyst").input_modes == ["text"]
    assert MockSubAgent("revenue-analyst", input_modes=["text", "image"]).input_modes == ["text", "image"]


# ---------------------------------------------------------------------------
# Composition with the extraction vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_name", ["slack-notifier", "revenue-analyst"])
async def test_dispatch_result_is_readable_by_the_extraction_helpers(agent_name):
    """The two halves must compose: dispatch produces what extraction reads."""
    agent = MockSubAgent(agent_name, reply="finished")
    result = await _dispatch(_context(agent), _task_call(agent_name, "do it", call_id="c9"))

    # Reassemble the turn the way the graph would have recorded it.
    from langchain_core.messages import AIMessage, HumanMessage

    state = {
        "messages": [
            HumanMessage("please do it"),
            AIMessage(content="", tool_calls=[_task_call(agent_name, "do it", call_id="c9")]),
            _tool_message(result),
        ]
    }

    assert delegated_agents(state) == [agent_name]
    assert delegations(state)[0].completed
