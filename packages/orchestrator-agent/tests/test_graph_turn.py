"""Real graph turns, scripted model — the mock tier's end-to-end layer.

Everything below runs a genuine ``GraphFactory`` graph: real middleware stack,
real ``task`` dispatch, real state channels. Only the LLM and the sub-agents are
substituted, so these tests answer questions the message-level tests structurally
cannot — they assert against state the graph actually produced rather than state
a test author typed.

That distinction is the point. ``tests/test_extraction.py`` pins helper
behaviour against synthetic messages; if the graph emitted a different shape,
every one of those tests would still pass. These close that gap.

No gateway, no credentials.
"""

from __future__ import annotations

import pytest

from app.models.schemas import FinalResponseSchema
from tests.support.extraction import (
    a2a_tracking,
    delegated_agents,
    delegations,
    final_response as read_final_response,
    final_text,
    task_state,
    tool_names,
)
from tests.support.graph_harness import (
    final_response,
    runtime_context,
    scripted_graph,
    task_call,
    tool_call,
    turn_config,
    user_turn,
)
from tests.support.mock_subagents import MockSubAgent
from tests.support.scripted_model import ScriptedChatModel, subagent_enum


async def _run(model: ScriptedChatModel, *agents: MockSubAgent, prompt: str = "do the thing", thread: str = "t1"):
    graph = scripted_graph(model)
    return await graph.ainvoke(user_turn(prompt), config=turn_config(thread), context=runtime_context(*agents))


# ---------------------------------------------------------------------------
# Delegation round-trips through real state
# ---------------------------------------------------------------------------


async def test_delegation_survives_a_real_turn_and_is_readable():
    slack = MockSubAgent("slack-client", "Sends Slack messages.", reply="posted")
    model = ScriptedChatModel(responses=[task_call("slack-client", "post to @john.doe"), final_response("Sent.")])

    state = await _run(model, slack, prompt="slack the summary to john")

    assert slack.called_with_substring("@john.doe")
    assert delegated_agents(state) == ["slack-client"]
    assert delegations(state)[0].result == "posted"
    assert delegations(state)[0].completed


async def test_final_response_and_text_read_from_real_state():
    slack = MockSubAgent("slack-client")
    model = ScriptedChatModel(responses=[task_call("slack-client"), final_response("All set.")])

    state = await _run(model, slack)

    assert final_text(state) == "All set."
    assert task_state(state) == "completed"
    assert read_final_response(state)["message"] == "All set."


async def test_turn_without_delegation_reports_none():
    """The negative case has to work too, or routing assertions can't fail."""
    model = ScriptedChatModel(responses=[final_response("No delegation needed.")])

    state = await _run(model)

    assert delegated_agents(state) == []
    assert final_text(state) == "No delegation needed."


async def test_multiple_delegations_are_ordered():
    runner = MockSubAgent("agent-runner", "Runs data queries.", reply="revenue up 8%")
    slack = MockSubAgent("slack-client", "Sends Slack messages.", reply="posted")
    model = ScriptedChatModel(
        responses=[
            task_call("agent-runner", "fetch Q3 earnings", call_id="c1"),
            task_call("slack-client", "post it to @john", call_id="c2"),
            final_response("Fetched and sent."),
        ]
    )

    state = await _run(model, runner, slack)

    assert delegated_agents(state) == ["agent-runner", "slack-client"]
    assert runner.called_with_substring("Q3 earnings")
    assert slack.called_with_substring("@john")


# ---------------------------------------------------------------------------
# State channels — previously assumed, now observed
# ---------------------------------------------------------------------------


async def test_a2a_tracking_channel_is_populated_by_a_real_dispatch():
    """`a2a_tracking` was the least-verified helper: only ever tested against a
    dict written by hand. This is the middleware actually filling it."""
    slack = MockSubAgent("slack-client", reply="posted")
    model = ScriptedChatModel(responses=[task_call("slack-client"), final_response()])

    state = await _run(model, slack)

    tracking = a2a_tracking(state)
    assert "slack-client" in tracking
    entry = tracking["slack-client"]
    assert entry["is_complete"] is True
    assert entry["requires_input"] is False
    assert entry["requires_auth"] is False
    # Note the protobuf enum name, not the lowercase A2A task_state vocabulary.
    assert entry["state"] == "TASK_STATE_COMPLETED"


async def test_structured_response_channel_really_is_a_pydantic_instance():
    """Confirms the shape `final_response()` is written to survive — the channel
    holds a validated model, not a dict, because the graph sets response_format."""
    model = ScriptedChatModel(responses=[final_response("Done.")])

    state = await _run(model)

    assert isinstance(state["structured_response"], FinalResponseSchema)
    assert read_final_response(state)["message"] == "Done."


async def test_orchestrator_visible_tools_are_the_ones_the_graph_ran():
    slack = MockSubAgent("slack-client")
    model = ScriptedChatModel(
        responses=[tool_call("get_current_time", call_id="c0"), task_call("slack-client"), final_response()]
    )

    state = await _run(model, slack)

    assert tool_names(state) == ["get_current_time", "task", "FinalResponseSchema"]


# ---------------------------------------------------------------------------
# What the model was actually offered
# ---------------------------------------------------------------------------


async def test_task_tool_advertises_exactly_the_registered_subagents():
    """The `subagent_type` enum is rebuilt per request from subagent_registry.
    If it were empty the model would have nothing to route to — the original
    blocker — and no state assertion could detect it."""
    runner = MockSubAgent("agent-runner", "Runs data queries.")
    slack = MockSubAgent("slack-client", "Sends Slack messages.")
    model = ScriptedChatModel(responses=[final_response()])

    await _run(model, runner, slack)

    assert sorted(subagent_enum(model.bound_tool("task"))) == ["agent-runner", "slack-client"]


async def test_core_orchestrator_tools_reach_the_model():
    model = ScriptedChatModel(responses=[final_response()])

    await _run(model)

    bound = model.bound_tool_names()
    assert {"task", "write_todos", "get_current_time", "FinalResponseSchema"} <= set(bound)


# ---------------------------------------------------------------------------
# Turn scoping against genuine accumulated history
# ---------------------------------------------------------------------------


async def test_previous_turn_delegation_is_excluded_from_the_current_turn():
    """Same assertion as the synthetic test, but over history the checkpointer
    actually accumulated across two invocations."""
    slack = MockSubAgent("slack-client", reply="posted")
    model = ScriptedChatModel(
        responses=[
            task_call("slack-client", "post to @john"),
            final_response("Sent."),
            final_response("It is 14:00."),
        ]
    )
    graph = scripted_graph(model)
    context = runtime_context(slack)

    first = await graph.ainvoke(user_turn("slack john"), config=turn_config(), context=context)
    assert delegated_agents(first) == ["slack-client"]

    second = await graph.ainvoke(user_turn("thanks, what time is it?"), config=turn_config(), context=context)

    assert delegated_agents(second) == []
    assert delegated_agents(second, all_turns=True) == ["slack-client"]
    assert final_text(second) == "It is 14:00."


# ---------------------------------------------------------------------------
# Harness guardrail
# ---------------------------------------------------------------------------


async def test_unscripted_model_call_fails_loudly():
    """A turn that takes an unexpected path must break the test, not loop or
    silently reuse the last scripted message."""
    slack = MockSubAgent("slack-client")
    model = ScriptedChatModel(responses=[task_call("slack-client")])  # no final response

    with pytest.raises(AssertionError, match="ScriptedChatModel exhausted"):
        await _run(model, slack)
