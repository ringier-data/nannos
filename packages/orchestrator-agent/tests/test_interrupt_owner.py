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
from unittest.mock import AsyncMock

from a2a.types import TaskState
from langchain_core.tools import tool

from app.core.executor import OrchestratorDeepAgentExecutor
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
