"""Only the speaker whose turn raised an interrupt may answer it.

In a channel conversation one checkpoint serves every participant, so the next
message on a paused thread can come from anyone. The executor used to read it as
the answer regardless of who wrote it: another participant could approve a
pending call and resume it under their own token.

The owner is read from the interrupted checkpoint's metadata, which LangGraph
fills from the run config. That is the claim everything rests on, so it is
tested against the real graph and checkpointer, not a stubbed snapshot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from a2a.server.agent_execution import RequestContext
from a2a.types import Message, Part, TaskState
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from langchain_core.tools import tool

from app.core.executor import STALE_DECISION_MESSAGE, OrchestratorDeepAgentExecutor
from app.core.interrupt_owner import InterruptOwner, foreign_interrupt_message, interrupt_owner
from tests.support.extraction import interrupted_tools
from tests.support.graph_harness import final_response, runtime_context, scripted_graph, tool_call, user_turn
from tests.support.scripted_model import ScriptedChatModel


@tool
def delete_records(target: str) -> str:
    """Delete records permanently."""
    return f"deleted {target}"


async def _paused_by(user_id: str, user_name: str, thread_id: str):
    """Run one real turn as *user_id* that stops on an approval, and return the pending state."""
    graph = scripted_graph(
        ScriptedChatModel(responses=[tool_call("delete_records", {"target": "prod"}), final_response()])
    )
    context = runtime_context(tool_registry={"delete_records": delete_records})
    config = {
        "configurable": {"thread_id": thread_id},
        # The keys the executor sets on every turn's config.
        "metadata": {"user_id": user_id, "user_name": user_name, "scope": "channel"},
    }
    state = await graph.ainvoke(user_turn("delete the prod records"), config=config, context=context)
    assert interrupted_tools(state) == ["delete_records"], "precondition: the turn must pause on the approval"
    return await graph.aget_state(config)


def _recording_updater() -> SimpleNamespace:
    return SimpleNamespace(update_status=AsyncMock())


_TASK = SimpleNamespace(id="task-b", context_id="ctx-thread")


class TestTheCheckpointNamesItsSpeaker:
    async def test_the_interrupted_checkpoint_carries_the_turns_user(self):
        pending = await _paused_by("user-a", "Alice", "owner-recorded")

        assert pending.interrupts
        assert interrupt_owner(pending) == InterruptOwner(user_id="user-a", user_name="Alice")

    def test_an_unattributed_checkpoint_has_no_owner(self):
        """Unknown is not "nobody": the caller lets the message through rather than locking the thread."""
        assert interrupt_owner(SimpleNamespace(metadata={})) is None
        assert interrupt_owner(SimpleNamespace(metadata=None)) is None
        assert interrupt_owner(SimpleNamespace(metadata={"user_id": ""})) is None

    def test_a_missing_name_is_not_invented(self):
        assert interrupt_owner(SimpleNamespace(metadata={"user_id": "u", "user_name": ""})) == InterruptOwner("u", None)


class TestTheExecutorRefusesAnotherSpeaker:
    async def test_another_speaker_is_refused_with_the_owners_name(self):
        pending = await _paused_by("user-a", "Alice", "foreign-speaker")
        updater = _recording_updater()

        refused = await OrchestratorDeepAgentExecutor._refuse_foreign_interrupt(pending, "user-b", updater, _TASK)

        assert refused
        updater.update_status.assert_awaited_once()
        state, message = updater.update_status.await_args.args
        assert state == TaskState.TASK_STATE_COMPLETED
        assert "Alice" in message.parts[0].text

    async def test_the_owner_answers_as_before(self):
        pending = await _paused_by("user-a", "Alice", "owner-answers")
        updater = _recording_updater()

        assert not await OrchestratorDeepAgentExecutor._refuse_foreign_interrupt(pending, "user-a", updater, _TASK)
        updater.update_status.assert_not_awaited()

    async def test_nothing_pending_means_nothing_to_refuse(self):
        updater = _recording_updater()
        idle = SimpleNamespace(interrupts=(), metadata={"user_id": "user-a"})

        assert not await OrchestratorDeepAgentExecutor._refuse_foreign_interrupt(idle, "user-b", updater, _TASK)
        updater.update_status.assert_not_awaited()

    async def test_refusing_leaves_the_interrupt_pending_for_its_owner(self):
        """The refusal must not resume, reject or otherwise settle the owner's interrupt."""
        graph = scripted_graph(
            ScriptedChatModel(responses=[tool_call("delete_records", {"target": "prod"}), final_response()])
        )
        context = runtime_context(tool_registry={"delete_records": delete_records})
        config = {"configurable": {"thread_id": "still-pending"}, "metadata": {"user_id": "user-a", "user_name": "A"}}
        await graph.ainvoke(user_turn("delete the prod records"), config=config, context=context)
        before = await graph.aget_state(config)

        await OrchestratorDeepAgentExecutor._refuse_foreign_interrupt(before, "user-b", _recording_updater(), _TASK)

        after = await graph.aget_state(config)
        assert [i.id for i in after.interrupts] == [i.id for i in before.interrupts]
        assert after.config["configurable"]["checkpoint_id"] == before.config["configurable"]["checkpoint_id"]


def test_the_refusal_names_the_owner_or_says_who_it_means():
    assert "Alice" in foreign_interrupt_message("Alice")
    assert "the person who started it" in foreign_interrupt_message(None)


def _clicked(*decisions: dict) -> RequestContext:
    """A button click: no words, one DataPart of decisions."""
    context = Mock(spec=RequestContext)
    context.message = Mock(spec=Message)
    context.message.parts = [Part(data=ParseDict({"decisions": list(decisions)}, Value()))]
    return context


def _typed() -> RequestContext:
    context = Mock(spec=RequestContext)
    context.message = Mock(spec=Message)
    context.message.parts = []
    return context


def _pending_call_id(state) -> str:
    return state.interrupts[0].value["action_requests"][0]["args"]["_call_id"]


class TestAStaleClickRunsNothing:
    """A chat card keeps its buttons after it was answered in words or replaced.

    A click on it then arrived as a turn with no words: with nothing pending it
    ran as an empty turn ("your latest message was empty"), and against a newer
    card its unmatched call id silently rejected that card.
    """

    async def test_a_click_with_nothing_pending_is_answered_not_run(self):
        updater = _recording_updater()
        idle = SimpleNamespace(interrupts=(), metadata={})

        refused = await OrchestratorDeepAgentExecutor._refuse_stale_decision(
            idle, _clicked({"type": "approve", "id": "gmail_create_draft:1@a:0"}), updater, _TASK
        )

        assert refused
        state, message = updater.update_status.await_args.args
        assert state == TaskState.TASK_STATE_COMPLETED
        assert message.parts[0].text == STALE_DECISION_MESSAGE

    async def test_a_click_for_another_call_leaves_the_newer_card_pending(self):
        graph = scripted_graph(
            ScriptedChatModel(responses=[tool_call("delete_records", {"target": "prod"}), final_response()])
        )
        context = runtime_context(tool_registry={"delete_records": delete_records})
        config = {"configurable": {"thread_id": "newer-card"}, "metadata": {"user_id": "user-a", "user_name": "A"}}
        await graph.ainvoke(user_turn("delete the prod records"), config=config, context=context)
        before = await graph.aget_state(config)
        assert _pending_call_id(before), "precondition: the pending call carries its id"

        refused = await OrchestratorDeepAgentExecutor._refuse_stale_decision(
            before, _clicked({"type": "approve", "id": "delete_records:old@card:0"}), _recording_updater(), _TASK
        )

        assert refused
        after = await graph.aget_state(config)
        assert [i.id for i in after.interrupts] == [i.id for i in before.interrupts]
        assert after.config["configurable"]["checkpoint_id"] == before.config["configurable"]["checkpoint_id"]

    async def test_a_click_for_the_pending_call_goes_through(self):
        pending = await _paused_by("user-a", "Alice", "matching-click")
        updater = _recording_updater()

        assert not await OrchestratorDeepAgentExecutor._refuse_stale_decision(
            pending, _clicked({"type": "approve", "id": _pending_call_id(pending)}), updater, _TASK
        )
        updater.update_status.assert_not_awaited()

    async def test_a_blanket_click_still_answers_what_is_pending(self):
        """Cards posted before buttons named their calls send a bare decision."""
        pending = await _paused_by("user-a", "Alice", "blanket-click")

        assert not await OrchestratorDeepAgentExecutor._refuse_stale_decision(
            pending, _clicked({"type": "reject"}), _recording_updater(), _TASK
        )

    async def test_words_are_never_a_stale_click(self):
        idle = SimpleNamespace(interrupts=(), metadata={})
        updater = _recording_updater()

        assert not await OrchestratorDeepAgentExecutor._refuse_stale_decision(idle, _typed(), updater, _TASK)
        updater.update_status.assert_not_awaited()

    async def test_a_client_action_result_is_never_a_stale_click(self):
        """The dock answers a client action (read the page) with its result.

        The guard collected only approval call ids, so the dock's result to
        ``read_current_page`` was refused as "already answered" and the turn ended.
        """
        pending = SimpleNamespace(
            interrupts=(SimpleNamespace(id="i1", value={"client_action_request": {"id": "ca-1"}}),),
            metadata={},
        )
        for decision in (
            {"type": "approve", "id": "ca-1", "client_action_result": {"ok": True}},
            {"type": "approve", "id": "other", "client_action_result": {"ok": True}},
            {"type": "approve", "id": "ca-1"},
        ):
            updater = _recording_updater()
            assert not await OrchestratorDeepAgentExecutor._refuse_stale_decision(
                pending, _clicked(decision), updater, _TASK
            ), decision
            updater.update_status.assert_not_awaited()

    async def test_an_unknown_question_is_left_to_its_reader(self):
        """An authorization prompt has no call id to compare a click against."""
        pending = SimpleNamespace(interrupts=(SimpleNamespace(id="i1", value={"task_state": "auth"}),), metadata={})
        assert not await OrchestratorDeepAgentExecutor._refuse_stale_decision(
            pending, _clicked({"type": "approve", "id": "x"}), _recording_updater(), _TASK
        )
