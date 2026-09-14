"""Regression guards for the behaviours ADR-0008 replaced rather than kept.

The A2A task-lifecycle refactor deleted four test modules along with the code
they covered (``test_adoption_postgres_fork``, the two
``test_concurrent_same_agent_*`` modules, ``test_subagent_resume_command``).
Most of what they guarded moved to ``test_delegation_task_lifecycle`` and
``agent-common``'s ``test_local_a2a_server`` / ``test_a2a_resume_alignment``.
These are the leftovers — behaviours the old code had, the new code changed,
and nothing asserts either way:

- **An adopted sub-agent's memory belongs to the run, not the conversation.**
  The fork-on-adopt gave every adopting conversation its own copy of the run's
  checkpoint; seeding the run's ``context_id`` means any conversation adopting
  that run derives the *same* thread. Safe only while a run is adoptable once —
  the test below names the three constraints that make it so.
- **The pre-change adoption record still adopts.** ``adopt_thread_from`` now
  means what ``context_id`` means, so it is read (with a migration warning)
  rather than dropped — otherwise a conversation adopted before the deploy kept
  its registered sub-agent and lost the run's history, silently.
- **A parked sub-agent thread is only reachable through its own task.** A NEW
  task on a parked thread is rejected (``PARKED_TASK_MESSAGE``), and only a
  delivered answer clears the interrupt. That looks like a way to wedge an agent
  for a whole conversation, and it is not — two guards keep the park reachable,
  and the tests below pin both, because losing either one WOULD wedge it.

Everything here passes. Several of these tests pin a behaviour rather than
demanding a better one, because the behaviour is load-bearing for something
non-obvious; each says so, and says what breaks if it changes.
"""

from __future__ import annotations

import logging

from a2a.types import TaskState
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.threads import local_sub_agent_thread_id
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable
from langchain_core.messages import ToolMessage

from app.core.agent import _build_adoption_seed
from app.middleware.dynamic_tool_dispatch import delegation_task_id
from tests.support.graph_harness import (
    runtime_context,
    scripted_graph,
    task_call,
    turn_config,
    user_turn,
)
from tests.support.mock_subagents import MockSubAgent
from tests.support.scripted_model import ScriptedChatModel
from tests.test_scheduled_run_adoption import VALIDATED, _local_runnable, _registry

RUN_CTX = "server-run-ctx"


def _task_results(state: dict) -> list[ToolMessage]:
    return [m for m in state["messages"] if isinstance(m, ToolMessage) and "a2a_metadata" in (m.additional_kwargs or {})]


def _seeded_thread(runnable: DynamicLocalAgentRunnable, record: dict, conversation_id: str) -> str:
    """The checkpoint thread a delegation lands on in *conversation_id*, given *record*.

    Follows the production waterfall: ``_extract_tracking_ids`` picks the
    context id (seeded record first, orchestrator conversation as fallback) and
    ``get_thread_id`` turns it into the thread.
    """
    sub_input = SubAgentInput(
        messages=[],
        a2a_tracking={runnable.tracking_key: record},
        orchestrator_conversation_id=conversation_id,
    )
    context_id, _ = DynamicLocalAgentRunnable._extract_tracking_ids(runnable, sub_input)
    return DynamicLocalAgentRunnable.get_thread_id(runnable, context_id, sub_input)


# ---------------------------------------------------------------------------
# Adoption: a sub-agent's memory is the RUN's, not the conversation's
# ---------------------------------------------------------------------------


def test_adoption_puts_every_conversation_on_the_runs_own_thread():
    """A sub-agent's adopted memory is keyed by the RUN, not by the conversation.

    The seeded ``context_id`` IS the thread (ADR-0008 §5), so the conversation id
    does not enter it: any conversation adopting a given run derives the same
    ``{run_ctx}::dynamic-{name}``. The fork-on-adopt this replaced gave each
    adopting conversation its own copy, so this is the one behaviour the fork
    provided that the seed does not.

    That is safe only because a run is adoptable exactly ONCE, which rests on
    three independent constraints:

    1. ``job.delivery_channel_id`` is singular — one notification per run;
    2. adoption happens by replying in that notification's thread, and a thread
       maps to one conversation;
    3. ``_validate_scheduled_run_origin`` re-resolves the job under the
       authenticated user's token, so another user's job 404s.

    Break any of them — a job gaining several delivery channels is the likely one
    — and two conversations would interleave turns in one sub-agent's memory.
    This test is where that shows up, so read the list above before "fixing" it.
    """
    runnable = _local_runnable("report-agent")
    _, _, record = _build_adoption_seed(dict(VALIDATED), _registry(runnable))

    thread_a = _seeded_thread(runnable, record, "conversation-a")
    thread_b = _seeded_thread(runnable, record, "conversation-b")

    assert thread_a == thread_b == local_sub_agent_thread_id(RUN_CTX, "report-agent")


def test_a_legacy_adopt_thread_from_record_still_adopts_the_run():
    """A pre-ADR-0008 adoption record names the run's conversation, and still works.

    Conversations that adopted a run before the deploy have
    ``{"adopt_thread_from": ...}`` persisted in ``a2a_tracking``. The dispatch
    used to FORK that checkpoint into the conversation's own thread; there is no
    fork any more and the run's context id *is* the thread, so the old key means
    exactly what ``context_id`` means now. Dropping it would start the sub-agent
    blank while ``_adopted_sub_agent_ids_from_tracking`` kept recovering
    ``sub_agent_id`` — an agent that stays registered and looks adopted, with no
    memory of the run.
    """
    runnable = _local_runnable("report-agent")
    legacy = {"adopt_thread_from": RUN_CTX, "is_complete": True, "sub_agent_id": 5}

    assert _seeded_thread(runnable, legacy, "conversation-a") == local_sub_agent_thread_id(RUN_CTX, "report-agent")


def test_a_current_context_id_wins_over_a_legacy_key(caplog):
    """Once the delegation returns, ``a2a_tracking`` records ``context_id`` beside the
    legacy key. The current one must win, and the migration notice must stop firing."""
    runnable = _local_runnable("report-agent")
    both = {"adopt_thread_from": "stale-run-ctx", "context_id": RUN_CTX, "is_complete": True}

    with caplog.at_level(logging.WARNING):
        thread = _seeded_thread(runnable, both, "conversation-a")

    assert thread == local_sub_agent_thread_id(RUN_CTX, "report-agent")
    assert not [r for r in caplog.records if "adopt_thread_from" in r.message]


def test_reading_a_legacy_record_is_announced(caplog):
    """The migration is silent otherwise, and 'the agent forgot the run' reports
    need something to grep for."""
    runnable = _local_runnable("report-agent")
    legacy = {"adopt_thread_from": RUN_CTX, "is_complete": True, "sub_agent_id": 5}

    with caplog.at_level(logging.WARNING):
        _seeded_thread(runnable, legacy, "conversation-a")

    assert any("adopt_thread_from" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Deterministic task ids
# ---------------------------------------------------------------------------


def test_one_tool_call_id_in_two_conversations_names_two_tasks():
    """The uuid5 seed must keep the conversation in it.

    A provider that reuses tool-call ids across conversations (``call_1``,
    ``toolu_01`` …) would otherwise have conversation B's delegation find
    conversation A's task in the shared, now-persistent task store and deliver
    its work as an answer to A's parked question.
    """
    assert delegation_task_id("conversation-a", "call_1") != delegation_task_id("conversation-b", "call_1")
    # …and stay stable within one conversation, which is what makes the replay work.
    assert delegation_task_id("conversation-a", "call_1") == delegation_task_id("conversation-a", "call_1")


# ---------------------------------------------------------------------------
# An abandoned approval
# ---------------------------------------------------------------------------


async def _park_on_an_approval(agent: MockSubAgent, model: ScriptedChatModel, thread: str):
    """Delegate once, park the turn on the sub-agent's tool approval."""
    graph = scripted_graph(model)
    config = turn_config(thread)
    context = runtime_context(agent)
    state = await graph.ainvoke(user_turn("who am I on github"), config=config, context=context)
    assert state.get("__interrupt__"), "precondition: the turn must park on the approval"
    return graph, config, context


async def test_cancelling_a_parked_task_does_not_clear_the_question():
    """``cancel`` closes the task record, not the graph's suspension.

    ``LocalSubAgentExecutor.cancel`` enqueues a ``CANCELED`` status event. It
    neither stops the graph nor clears the interrupt it is suspended on. Pinned
    because it is the premise of the two guards below: nothing in the cancel path
    clears a park, so reachability has to come from somewhere else.

    Production never even reaches here for a parked delegation — ``executor.cancel``
    propagates only to ``get_all_active_subagent_dispatches``, and the dispatch
    deregisters itself in a ``finally`` as ``GraphInterrupt`` propagates, so by the
    time the turn is parked there is no active dispatch left to cancel.
    """
    github = MockSubAgent("github-agent", "GitHub.", reply="you are aartaria", approval="github_get_me")
    model = ScriptedChatModel(responses=[task_call("github-agent", "who am I", call_id="call-task")])
    await _park_on_an_approval(github, model, "t-abandon-cancel")

    task_id = delegation_task_id("t-abandon-cancel", "call-task")
    assert (await github.aget_task(task_id)).status.state == TaskState.TASK_STATE_INPUT_REQUIRED

    await github.cancel_task(task_id)

    assert await github.aget_pending_interrupts({}), "the graph is still suspended on the approval"


async def test_a_tracked_task_id_beats_a_proposed_one():
    """GUARD 1: a parked delegation stays addressable through ``a2a_tracking``.

    While a sub-agent's task is unfinished (``is_complete`` false — what an
    ``input_required`` park leaves behind), ``a2a_tracking`` keeps its ``task_id``.
    The next delegation therefore addresses THAT task rather than the id proposed
    for a fresh one, arrives as a continuation, and resumes the park.

    This is what keeps a parked agent recoverable on a later turn, and it is why
    resume-round exhaustion (``MAX_SUBAGENT_RESUME_ROUNDS`` returns a result while
    the sub-agent is still parked) does not wedge the agent. If the precedence ever
    flipped to the proposed id, such a delegation would open a NEW task on a parked
    thread and come back as ``PARKED_TASK_MESSAGE`` — for every later turn.
    """
    github = MockSubAgent("github-agent", "GitHub.")
    parked_task_id = delegation_task_id("t-track", "call-earlier")
    sub_input = SubAgentInput(
        messages=[],
        a2a_tracking={github.tracking_key: {"context_id": "t-track", "task_id": parked_task_id, "is_complete": False}},
        orchestrator_conversation_id="t-track",
        proposed_task_id=delegation_task_id("t-track", "call-now"),
    )

    context_id, task_id = github._extract_tracking_ids(sub_input)

    assert context_id == "t-track"
    assert task_id == parked_task_id, "the parked task must win over the proposal, or the delegation is rejected"


async def test_a_completed_task_id_is_dropped_so_the_proposal_wins():
    """The other half of GUARD 1: continuity must not outlive the task.

    A completed delegation's ``task_id`` is dropped, so the next delegation opens a
    fresh task under its proposed id instead of trying to continue a finished one.
    """
    github = MockSubAgent("github-agent", "GitHub.")
    sub_input = SubAgentInput(
        messages=[],
        a2a_tracking={
            github.tracking_key: {
                "context_id": "t-track",
                "task_id": delegation_task_id("t-track", "call-earlier"),
                "is_complete": True,
            }
        },
        orchestrator_conversation_id="t-track",
        proposed_task_id=delegation_task_id("t-track", "call-now"),
    )

    _, task_id = github._extract_tracking_ids(sub_input)

    assert task_id is None, "a finished task is not continued; astream falls back to the proposed id"


async def test_a_later_message_is_routed_into_the_pending_interrupt():
    """GUARD 2: the orchestrator never abandons its own interrupt.

    ``execute`` turns ANY incoming message into ``Command(resume=...)`` while the
    graph has pending interrupts — there is no "this is a new request" branch (see
    ``if current_state.interrupts`` in ``executor.py``). So a user who ignores the
    approval card and types something else still resolves the park: the words reach
    the sub-agent's HITL reader, which classifies them approve / reject /
    not-an-answer (``agent_common.core.hitl_resume._from_intent``).

    Asserted at the resume-map level, the unit that routing produces: a message
    arriving while parked must come out keyed by the pending interrupt's id.
    Anything else would start a fresh turn and strand the sub-agent's task — the
    wedge these guards exist to prevent.
    """
    from app.core.executor import OrchestratorDeepAgentExecutor

    github = MockSubAgent("github-agent", "GitHub.", reply="you are aartaria", approval="github_get_me")
    model = ScriptedChatModel(responses=[task_call("github-agent", "who am I", call_id="call-task")])
    graph, config, _ = await _park_on_an_approval(github, model, "t-routing")

    pending = (await graph.aget_state(config)).interrupts
    assert pending, "precondition: the orchestrator graph is parked"

    resume_value = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
        pending, hitl_decisions=None, query="never mind, what's the weather"
    )

    assert set(resume_value) == {intr.id for intr in pending}, "the words are routed into the pending interrupt"
