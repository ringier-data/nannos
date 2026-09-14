"""The in-process A2A server a local sub-agent runs behind.

Drives a ``LocalA2ARunnable`` through its real ``astream`` — message conversion,
``DefaultRequestHandler``, ``TaskStore``, executor, event translation — with
only the graph replaced by a scripted stand-in. What is asserted is the task
lifecycle the orchestrator now relies on instead of probing checkpoints:

- a delegation opens a task under the id the caller proposed, and completes
  with a typed result whose ids are the task's own;
- a graph that parks on an interrupt pauses the task (``input_required`` /
  ``auth_required``) with the interrupts on the wire, and the next message to
  that task is delivered as an id-keyed ``Command(resume)`` fitted to the
  question actually asked;
- new work cannot start on a thread that is parked; runs on one conversation
  never overlap; a stored task can be read back by id;
- the SDK's live ``ActiveTask`` is a cache: it is released when a stream ends
  (parked or finished, drained or abandoned) and a resume rebuilds from the
  store and the checkpoint alone.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterable

import pytest
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import TaskState
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Interrupt
from ringier_a2a_sdk.models import TodoItem

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.event_translation import INTERRUPTS_METADATA_KEY
from agent_common.a2a.extensions import HUMAN_IN_THE_LOOP_EXTENSION, IN_TASK_AUTH_EXTENSION
from agent_common.a2a.local_server import PARKED_TASK_MESSAGE, set_local_task_store
from agent_common.a2a.message_conversion import answer_to_human_message
from agent_common.a2a.stream_events import (
    ActivityLogMeta,
    ArtifactUpdate,
    ErrorEvent,
    IntermediateOutputMeta,
    StreamEvent,
    TaskUpdate,
    WorkPlanMeta,
)
from langchain_core.messages import HumanMessage

INTERRUPT_ID = "45fda8478b2ef754419799e10992af06"
HITL_VALUE = {
    "action_requests": [{"name": "delete_records", "args": {"_call_id": "delete_records:1"}, "description": "Delete?"}],
    "review_configs": [{"action_name": "delete_records", "allowed_decisions": ["approve", "reject"]}],
}
AUTH_VALUE = {
    "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
    "tool": "github_get_me",
    "message": "GitHub needs your authorization",
    "auth_url": "https://auth.example/authorize",
}

PARENT_CONFIG = {
    "configurable": {"thread_id": "orchestrator-thread"},
    "metadata": {"user_id": "u1", "assistant_id": "a1"},
    "tags": ["conversation:ctx-1"],
}


class ScriptedAgent(LocalA2ARunnable):
    """A local sub-agent whose graph is a script.

    ``behaviour`` picks what the graph does on a fresh message; a ``Command``
    (resume) always completes and clears the recorded pending interrupt, which
    is what a real graph's checkpoint does once the answer lands.
    """

    def __init__(self, name: str = "scripted", behaviour: str = "complete", interrupt_value: dict | None = None):
        super().__init__()
        self._name = name
        self.behaviour = behaviour
        self.interrupt_value = interrupt_value or HITL_VALUE
        self.calls: list[tuple[Any, dict]] = []
        self.pending: list[Interrupt] = []
        self.windows: list[tuple[float, float]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Scripted test agent."

    def get_supported_input_modes(self) -> list[str]:
        return ["text", "image"]

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        return self._name

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        return self._name

    async def aget_pending_interrupts(self, config: dict) -> list:
        return list(self.pending)

    async def _astream_impl(self, input_data: Any, config: dict) -> AsyncIterable[StreamEvent]:
        self.calls.append((input_data, config))
        if isinstance(input_data, Command):
            self.pending = []
            yield TaskUpdate(data=self._build_success_response("resumed", context_id="ctx-1", task_id="ignored"))
            return

        context_id, task_id = self._extract_tracking_ids(input_data)
        text = self._extract_message_content(input_data)

        if self.behaviour == "interrupt":
            intr = Interrupt(value=self.interrupt_value, id=INTERRUPT_ID)
            self.pending = [intr]
            raise GraphInterrupt((intr,))
        if self.behaviour == "error":
            raise RuntimeError("boom")
        if self.behaviour == "slow":
            loop = asyncio.get_event_loop()
            start = loop.time()
            await asyncio.sleep(0.05)
            self.windows.append((start, loop.time()))
        if self.behaviour == "stream":
            yield ArtifactUpdate(content="Hel")
            yield ArtifactUpdate(content="lo")
            yield ArtifactUpdate(content="hmm", event_metadata=IntermediateOutputMeta())
            yield TaskUpdate(status_text="Using search", event_metadata=ActivityLogMeta())
            yield TaskUpdate(event_metadata=WorkPlanMeta(todos=[TodoItem(name="find", state="working")]))
        if self.behaviour == "input_required":
            yield TaskUpdate(data=self._build_input_required_response("Which campaign?", context_id, task_id))
            return
        yield TaskUpdate(
            data=self._build_success_response(f"done: {text}", context_id=context_id, task_id=task_id, session_rid="rid-1")
        )


@pytest.fixture(autouse=True)
def fresh_task_store():
    set_local_task_store(InMemoryTaskStore())


def fresh_input(text: str, *, proposed_task_id: str, context_id: str = "ctx-1") -> dict:
    return {
        "messages": [HumanMessage(content=text)],
        "a2a_tracking": {},
        "orchestrator_conversation_id": context_id,
        "proposed_task_id": proposed_task_id,
    }


def answer_input(agent: ScriptedAgent, answer: Any, *, task_id: str, context_id: str = "ctx-1") -> dict:
    return {
        "messages": [answer_to_human_message(answer)],
        "a2a_tracking": {agent.tracking_key: {"context_id": context_id, "task_id": task_id, "is_complete": False}},
        "orchestrator_conversation_id": context_id,
    }


async def collect(agent: LocalA2ARunnable, input_data: Any, config: Any = PARENT_CONFIG) -> list[StreamEvent]:
    return [event async for event in agent.astream(input_data, config)]


def final(events: list[StreamEvent]) -> TaskUpdate:
    updates = [e for e in events if isinstance(e, TaskUpdate)]
    assert updates, events
    return updates[-1]


# ── Opening and completing a task ────────────────────────────────────────────────


async def test_a_delegation_opens_the_proposed_task_and_completes_typed():
    agent = ScriptedAgent()
    events = await collect(agent, fresh_input("summarise", proposed_task_id="task-1"))

    result = final(events).data
    assert result.state == TaskState.TASK_STATE_COMPLETED
    assert result.task_id == "task-1"
    assert result.context_id == "ctx-1"
    assert result.messages[-1].content == "done: summarise"
    # The server's extra metadata rides the typed response, not a JSON envelope.
    assert result.metadata["session_rid"] == "rid-1"

    # The graph saw the task's ids as its own tracking record.
    sub_input, run_config = agent.calls[0]
    assert sub_input.a2a_tracking[agent.tracking_key] == {"context_id": "ctx-1", "task_id": "task-1", "is_complete": False}
    assert run_config["configurable"]["thread_id"] == "ctx-1::scripted"
    assert "sub_agent:scripted" in run_config["tags"]
    assert run_config["metadata"]["user_id"] == "u1"

    stored = await agent.aget_task("task-1")
    assert stored is not None and stored.status.state == TaskState.TASK_STATE_COMPLETED
    assert await agent.aget_task("never-opened") is None


async def test_follow_ups_share_the_conversation_thread_but_not_the_task():
    agent = ScriptedAgent()
    await collect(agent, fresh_input("first", proposed_task_id="task-1"))
    events = await collect(agent, fresh_input("second", proposed_task_id="task-2"))

    assert final(events).data.task_id == "task-2"
    threads = {config["configurable"]["thread_id"] for _, config in agent.calls}
    assert threads == {"ctx-1::scripted"}


async def test_streamed_events_keep_their_kinds():
    agent = ScriptedAgent(behaviour="stream")
    events = await collect(agent, fresh_input("stream it", proposed_task_id="task-1"))

    chunks = [e for e in events if isinstance(e, ArtifactUpdate)]
    assert [c.content for c in chunks if not isinstance(c.event_metadata, IntermediateOutputMeta)] == ["Hel", "lo"]
    assert [c.content for c in chunks if isinstance(c.event_metadata, IntermediateOutputMeta)] == ["hmm"]

    activity = [e for e in events if isinstance(e, TaskUpdate) and isinstance(e.event_metadata, ActivityLogMeta)]
    assert [a.status_text for a in activity] == ["Using search"]
    plans = [e for e in events if isinstance(e, TaskUpdate) and isinstance(e.event_metadata, WorkPlanMeta)]
    assert plans and plans[0].event_metadata.todos[0]["name"] == "find"

    assert final(events).data.state == TaskState.TASK_STATE_COMPLETED


async def test_a_question_asked_through_the_structured_response_is_not_an_interrupt():
    agent = ScriptedAgent(behaviour="input_required")
    events = await collect(agent, fresh_input("plan", proposed_task_id="task-1"))
    result = final(events).data
    assert result.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert INTERRUPTS_METADATA_KEY not in result.metadata
    assert "Which campaign?" in result.messages[-1].content

    # The user's reply continues the task as a fresh message on the same thread —
    # there is no pending interrupt to resume.
    agent.behaviour = "complete"
    events = await collect(agent, answer_input(agent, "the summer one", task_id="task-1"))
    assert final(events).data.task_id == "task-1"
    assert final(events).data.state == TaskState.TASK_STATE_COMPLETED
    assert not isinstance(agent.calls[-1][0], Command)


async def test_a_graph_error_fails_the_task():
    agent = ScriptedAgent(behaviour="error")
    events = await collect(agent, fresh_input("x", proposed_task_id="task-1"))
    result = final(events).data
    assert result.state == TaskState.TASK_STATE_FAILED
    assert "boom" in result.messages[-1].content


async def test_missing_parent_config_is_an_error_event():
    agent = ScriptedAgent()
    events = await collect(agent, fresh_input("x", proposed_task_id="task-1"), config=None)
    assert isinstance(events[-1], ErrorEvent)
    assert "requires parent config" in events[-1].error


# ── Pausing and resuming ─────────────────────────────────────────────────────────


async def test_an_interrupt_pauses_the_task_with_the_hitl_vocabulary():
    agent = ScriptedAgent(behaviour="interrupt")
    events = await collect(agent, fresh_input("delete them", proposed_task_id="task-1"))

    result = final(events).data
    assert result.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert result.task_id == "task-1"
    assert result.metadata[INTERRUPTS_METADATA_KEY] == [{"id": INTERRUPT_ID, "value": HITL_VALUE}]
    assert "Delete?" in final(events).status_text

    stored = await agent.aget_task("task-1")
    assert stored.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert HUMAN_IN_THE_LOOP_EXTENSION in stored.status.message.extensions


async def test_the_answer_to_a_paused_task_is_an_id_keyed_resume():
    agent = ScriptedAgent(behaviour="interrupt")
    await collect(agent, fresh_input("delete them", proposed_task_id="task-1"))

    decisions = {"decisions": [{"type": "approve", "id": "delete_records:1"}]}
    events = await collect(agent, answer_input(agent, decisions, task_id="task-1"))

    resumed_with, _ = agent.calls[-1]
    assert isinstance(resumed_with, Command)
    assert resumed_with.resume == {INTERRUPT_ID: decisions}
    result = final(events).data
    assert result.state == TaskState.TASK_STATE_COMPLETED
    assert result.task_id == "task-1"
    assert (await agent.aget_task("task-1")).status.state == TaskState.TASK_STATE_COMPLETED


async def test_words_are_delivered_untouched_and_fitted_answers_translated():
    agent = ScriptedAgent(behaviour="interrupt")
    await collect(agent, fresh_input("delete them", proposed_task_id="task-1"))
    await collect(agent, answer_input(agent, "no, forget it", task_id="task-1"))
    assert agent.calls[-1][0].resume == {INTERRUPT_ID: "no, forget it"}

    agent = ScriptedAgent(behaviour="interrupt")
    await collect(agent, fresh_input("delete them", proposed_task_id="task-2"))
    await collect(agent, answer_input(agent, {"authorization": {"decision": "declined"}}, task_id="task-2"))
    decisions = agent.calls[-1][0].resume[INTERRUPT_ID]["decisions"]
    assert [d["type"] for d in decisions] == ["reject"]


async def test_an_auth_interrupt_pauses_on_auth_required():
    agent = ScriptedAgent(behaviour="interrupt", interrupt_value=AUTH_VALUE)
    events = await collect(agent, fresh_input("who am I", proposed_task_id="task-1"))

    result = final(events).data
    assert result.state == TaskState.TASK_STATE_AUTH_REQUIRED
    assert result.metadata[INTERRUPTS_METADATA_KEY][0]["value"]["auth_url"] == AUTH_VALUE["auth_url"]
    assert "GitHub needs your authorization" in result.metadata["instructions"]
    stored = await agent.aget_task("task-1")
    assert IN_TASK_AUTH_EXTENSION in stored.status.message.extensions


async def test_new_work_cannot_start_on_a_parked_thread():
    agent = ScriptedAgent(behaviour="interrupt")
    await collect(agent, fresh_input("delete them", proposed_task_id="task-1"))

    events = await collect(agent, fresh_input("also list them", proposed_task_id="task-2"))
    result = final(events).data
    assert result.state == TaskState.TASK_STATE_REJECTED
    assert result.task_id == "task-2"
    assert PARKED_TASK_MESSAGE.format(agent="scripted") in result.messages[-1].content
    # The rejected task never reached the graph; the parked one still resumes.
    assert len(agent.calls) == 1
    await collect(agent, answer_input(agent, {"decisions": [{"type": "approve"}]}, task_id="task-1"))
    assert (await agent.aget_task("task-1")).status.state == TaskState.TASK_STATE_COMPLETED


async def test_a_replay_finds_the_task_it_opened():
    """The orchestrator's replayed tool call asks the store before sending anything."""
    agent = ScriptedAgent(behaviour="interrupt")
    await collect(agent, fresh_input("delete them", proposed_task_id="task-1"))
    parked = await agent.aget_task("task-1")
    assert parked is not None and parked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert parked.context_id == "ctx-1"


# ── One conversation, one writer ─────────────────────────────────────────────────


async def test_runs_on_one_conversation_never_overlap():
    agent = ScriptedAgent(behaviour="slow")
    await asyncio.gather(
        collect(agent, fresh_input("a", proposed_task_id="task-a")),
        collect(agent, fresh_input("b", proposed_task_id="task-b")),
    )
    assert len(agent.windows) == 2
    (s1, e1), (s2, e2) = sorted(agent.windows)
    assert e1 <= s2, "the second run started before the first finished"
    stored = {(await agent.aget_task(t)).status.state for t in ("task-a", "task-b")}
    assert stored == {TaskState.TASK_STATE_COMPLETED}


async def test_different_conversations_run_in_parallel():
    agent = ScriptedAgent(behaviour="slow")
    await asyncio.gather(
        collect(agent, fresh_input("a", proposed_task_id="task-a", context_id="ctx-a")),
        collect(agent, fresh_input("b", proposed_task_id="task-b", context_id="ctx-b")),
    )
    (s1, e1), (s2, e2) = sorted(agent.windows)
    assert s2 < e1, "runs on different conversations were serialised"


# ── The live object is a cache ──────────────────────────────────────────────────


async def live(agent: LocalA2ARunnable, task_id: str):
    """The SDK's in-memory ``ActiveTask`` for ``task_id``, or ``None``."""
    return await agent.local_server._handler._active_task_registry.get(task_id)


async def test_a_parked_task_has_no_live_object_and_still_resumes():
    agent = ScriptedAgent(behaviour="interrupt")
    await collect(agent, fresh_input("delete them", proposed_task_id="task-1"))

    # Parked in the store, gone from the registry: nothing idles waiting for the answer.
    assert (await agent.aget_task("task-1")).status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert await live(agent, "task-1") is None

    # The answer lands on a fresh ActiveTask, rebuilt from the store and the checkpoint.
    decisions = {"decisions": [{"type": "approve", "id": "delete_records:1"}]}
    events = await collect(agent, answer_input(agent, decisions, task_id="task-1"))
    assert final(events).data.state == TaskState.TASK_STATE_COMPLETED
    assert isinstance(agent.calls[-1][0], Command)
    assert await live(agent, "task-1") is None


async def test_a_finished_task_leaves_the_registry():
    agent = ScriptedAgent()
    await collect(agent, fresh_input("hi", proposed_task_id="task-1"))
    assert (await agent.aget_task("task-1")).status.state == TaskState.TASK_STATE_COMPLETED
    assert await live(agent, "task-1") is None


async def test_a_consumer_that_stops_early_releases_the_task():
    agent = ScriptedAgent(behaviour="stream")
    stream = agent.astream(fresh_input("hi", proposed_task_id="task-1"), PARENT_CONFIG)
    await anext(stream)  # the opening Task event
    assert isinstance(await anext(stream), ArtifactUpdate)
    assert await live(agent, "task-1") is not None
    await stream.aclose()

    # The run in flight finished first; only then was the live object dropped.
    assert (await agent.aget_task("task-1")).status.state == TaskState.TASK_STATE_COMPLETED
    assert await live(agent, "task-1") is None


async def test_aclose_cuts_off_every_live_task_without_waiting():
    agent = ScriptedAgent(behaviour="slow")
    streams = [
        agent.astream(fresh_input("a", proposed_task_id="task-a", context_id="ctx-a"), PARENT_CONFIG),
        agent.astream(fresh_input("b", proposed_task_id="task-b", context_id="ctx-b"), PARENT_CONFIG),
    ]
    for stream in streams:
        await anext(stream)  # the opening Task event: both are live, both streams still held open
    assert await live(agent, "task-a") is not None
    assert await live(agent, "task-b") is not None

    started = asyncio.get_event_loop().time()
    await agent.local_server.aclose()
    assert asyncio.get_event_loop().time() - started < 1.0

    assert await live(agent, "task-a") is None
    assert await live(agent, "task-b") is None
    for stream in streams:
        await stream.aclose()
    assert await live(agent, "task-a") is None
