"""Every delegation is an A2A task — through the real orchestrator graph.

The dispatch middleware used to carry the sub-agent's task lifecycle itself: it
probed the sub-agent's checkpoint for pending interrupts before each call, caught
the ``GraphInterrupt`` the sub-agent re-raised, built the ``Command(resume)``, and
refused a second concurrent call to the same agent because two calls would have
shared one thread. All of that is now the sub-agent's in-process A2A server's
business, and the dispatch is a client: it opens a task named after the tool
call, reads the task's state, parks the orchestrator when the task is waiting for
the user, and delivers the answer to the task on the replay.

These tests drive a genuine ``GraphFactory`` graph with a scripted model and mock
sub-agents that travel the real in-process server, so what is asserted is the
production wiring — the deterministic task id, the replay finding the parked
task, the id-keyed resume reaching the sub-agent, and two same-agent calls both
running.
"""

from __future__ import annotations

from a2a.types import TaskState
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.middleware.dynamic_tool_dispatch import delegation_task_id
from tests.support.extraction import a2a_tracking, delegations
from tests.support.graph_harness import (
    final_response,
    parallel_calls,
    runtime_context,
    scripted_graph,
    task_call,
    turn_config,
    user_turn,
)
from tests.support.mock_subagents import APPROVAL_INTERRUPT_ID, MockSubAgent
from tests.support.scripted_model import ScriptedChatModel


def _task_results(state: dict) -> list[ToolMessage]:
    return [m for m in state["messages"] if isinstance(m, ToolMessage) and "a2a_metadata" in (m.additional_kwargs or {})]


async def test_two_calls_to_one_agent_in_one_message_both_run():
    """Same agent twice in parallel: two tasks, one conversation, both answered.

    The second call used to be refused up front ("one task per agent"); the
    in-process server now runs the two tasks one after the other on the agent's
    conversation thread, so the model gets both results.
    """
    github = MockSubAgent("github-agent", "GitHub.", reply=lambda instruction: f"did: {instruction}")
    model = ScriptedChatModel(
        responses=[
            parallel_calls(
                ("task", {"subagent_type": "github-agent", "description": "who am I"}),
                ("task", {"subagent_type": "github-agent", "description": "list repos"}),
            ),
            final_response("Both done."),
        ]
    )
    graph = scripted_graph(model)
    state = await graph.ainvoke(user_turn("who am I, and my repos"), config=turn_config("t-par"), context=runtime_context(github))

    results = _task_results(state)
    assert sorted(m.content for m in results) == ["did: list repos", "did: who am I"]
    assert github.received == ["who am I", "list repos"] or github.received == ["list repos", "who am I"]

    task_ids = {m.additional_kwargs["a2a_metadata"]["task_id"] for m in results}
    assert task_ids == {delegation_task_id("t-par", "tc-0"), delegation_task_id("t-par", "tc-1")}
    for tid in task_ids:
        assert (await github.aget_task(tid)).status.state == TaskState.TASK_STATE_COMPLETED
    assert a2a_tracking(state)["github-agent"]["context_id"] == "t-par"


async def test_sub_agent_approval_parks_the_turn_and_the_replay_answers_the_task():
    """A sub-agent's tool approval: park, replay, deliver — without probing checkpoints.

    1. The delegation opens task T (named after the tool call). The sub-agent's
       graph parks on an approval; its server reports T as ``input_required``
       with the interrupt; the dispatch calls ``interrupt()`` and the turn parks.
    2. The user approves. LangGraph replays the tool call byte-identical; the
       dispatch asks the server for T, finds it parked, and ``interrupt()`` now
       returns the decision, which travels to T as a message. The server fits it
       to the parked interrupt (id-keyed ``Command(resume)``) and the graph
       completes. The work is not run twice.
    """
    github = MockSubAgent("github-agent", "GitHub.", reply="you are aartaria", approval="github_get_me")
    model = ScriptedChatModel(
        responses=[task_call("github-agent", "who am I", call_id="call-task"), final_response("Told them.")]
    )
    graph = scripted_graph(model)
    config = turn_config("t-hitl")
    context = runtime_context(github)
    task_id = delegation_task_id("t-hitl", "call-task")

    parked_state = await graph.ainvoke(user_turn("who am I on github"), config=config, context=context)

    interrupts = parked_state.get("__interrupt__") or []
    assert len(interrupts) == 1, "the orchestrator turn must park on the sub-agent's approval"
    action_requests = interrupts[0].value["action_requests"]
    assert action_requests[0]["name"] == "github_get_me"
    assert github.received == ["who am I"]
    assert github.resumed_with == []
    parked = await github.aget_task(task_id)
    assert parked is not None and parked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert _task_results(parked_state) == []  # no result yet: the tool call is still open

    decisions = {"decisions": [{"type": "approve", "id": "github_get_me:1"}]}
    state = await graph.ainvoke(Command(resume={interrupts[0].id: decisions}), config=config, context=context)

    assert github.resumed_with == [{APPROVAL_INTERRUPT_ID: decisions}]
    assert github.received == ["who am I"], "the replay must answer the parked task, not re-run the work"
    (result,) = _task_results(state)
    assert result.content == "you are aartaria"
    assert result.additional_kwargs["a2a_metadata"]["task_id"] == task_id
    assert result.additional_kwargs["a2a_metadata"]["state"] == "TASK_STATE_COMPLETED"
    assert (await github.aget_task(task_id)).status.state == TaskState.TASK_STATE_COMPLETED
    assert delegations(state)[0].completed
    tracking = a2a_tracking(state)["github-agent"]
    assert tracking["context_id"] == "t-hitl"
    assert "task_id" not in tracking  # completed: nothing left to continue


async def test_a_rejected_approval_reaches_the_sub_agent_as_a_rejection():
    github = MockSubAgent("github-agent", "GitHub.", reply="refused", approval="github_get_me")
    model = ScriptedChatModel(responses=[task_call("github-agent", "who am I"), final_response("Ok.")])
    graph = scripted_graph(model)
    config = turn_config("t-reject")
    context = runtime_context(github)

    parked_state = await graph.ainvoke(user_turn("who am I"), config=config, context=context)
    (interrupt,) = parked_state["__interrupt__"]
    decisions = {"decisions": [{"type": "reject", "id": "github_get_me:1", "message": "not now"}]}
    await graph.ainvoke(Command(resume={interrupt.id: decisions}), config=config, context=context)

    assert github.resumed_with == [{APPROVAL_INTERRUPT_ID: decisions}]


async def test_a_question_asked_in_words_is_relayed_not_parked():
    """``input_required`` through the structured response is the model's to relay."""
    slack = MockSubAgent("slack-notifier", "Slack.", input_required="which channel?")
    model = ScriptedChatModel(
        responses=[task_call("slack-notifier", "post it"), final_response("Which channel?", task_state="input_required")]
    )
    graph = scripted_graph(model)
    state = await graph.ainvoke(user_turn("post it"), config=turn_config("t-ask"), context=runtime_context(slack))

    assert not state.get("__interrupt__")
    (result,) = _task_results(state)
    assert "which channel?" in result.content
    tracking = a2a_tracking(state)["slack-notifier"]
    assert tracking["requires_input"] is True
    assert tracking["task_id"] == delegation_task_id("t-ask", "call-task"), "kept: the reply continues this task"
