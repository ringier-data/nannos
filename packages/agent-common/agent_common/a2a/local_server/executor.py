"""The ``AgentExecutor`` that runs a local sub-agent graph for one A2A message.

This is the sub-agent's half of the task lifecycle — the half the orchestrator's
dispatch middleware used to carry for it. Given a message it decides, from the
graph's own state, whether it is opening a task, continuing the agent's
conversation, or answering a paused interrupt, and it reports what the graph did
in A2A terms: working status with the extension vocabulary, streamed artifacts,
a terminal state, or a pause (``input_required`` / ``auth_required``) whose
status message any client can act on.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterable, Sequence

from a2a.helpers import new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import InvalidParamsError, Part, TaskState, TaskStatus, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from langgraph.errors import GraphInterrupt
from ringier_a2a_sdk.models import TodoItem

from agent_common.core.hitl_resume import KIND_AUTH, KIND_HITL, interrupt_kind

from ..authentication import AuthPayload
from ..event_translation import INTERRUPTS_METADATA_KEY, INTERVENTION_STATES, json_safe_interrupts
from ..extensions import (
    INTERMEDIATE_OUTPUT_EXTENSION,
    new_activity_log_message,
    new_auth_required_message,
    new_client_action_message,
    new_client_action_request_message,
    new_hitl_interrupt_message,
    new_work_plan_message,
)
from ..message_conversion import a2a_message_to_human_message, answer_from_message
from ..stream_events import (
    TERMINAL_STATES,
    ActivityLogMeta,
    ArtifactUpdate,
    ClientActionMeta,
    ErrorEvent,
    IntermediateOutputMeta,
    StreamEvent,
    TaskUpdate,
    WorkPlanMeta,
)
from .resume import build_resume_command

if TYPE_CHECKING:
    from ..base import LocalA2ARunnable

logger = logging.getLogger(__name__)

#: ``ServerCallContext.state`` key under which the caller hands the executor its
#: own LangChain ``RunnableConfig``. Being in-process is what makes this
#: possible: the sub-agent graph inherits the caller's callbacks, tracing,
#: cost-attribution tags and checkpointer instead of rebuilding them.
PARENT_CONFIG_KEY = "parent_config"

PARKED_TASK_MESSAGE = (
    "Not started: '{agent}' is waiting for a decision on a pending action from an earlier task in this "
    "conversation, and cannot take new work until that task is answered. This is plumbing, not something "
    "the user can act on: do not report '{agent}' as unavailable. Once the pending task has completed, "
    "delegate this work again."
)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _text_of(content: Any) -> str:
    """The text a model returned, thinking blocks and tool-call scaffolding dropped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and "text" in b]
        return "\n\n".join(parts) if parts else str(content)
    return str(content)


class LocalSubAgentExecutor(AgentExecutor):
    """Run a ``LocalA2ARunnable``'s graph as an A2A agent."""

    def __init__(self, runnable: "LocalA2ARunnable") -> None:
        self._runnable = runnable

    # ------------------------------------------------------------------ execute

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None:
            raise InvalidParamsError(message="A message is required")
        call_state = (context.call_context.state or {}) if context.call_context else {}
        parent_config = call_state.get(PARENT_CONFIG_KEY)
        if not parent_config:
            raise InvalidParamsError(
                message=(
                    f"Local sub-agent '{self._runnable.name}' requires the caller's RunnableConfig "
                    f"(ServerCallContext.state['{PARENT_CONFIG_KEY}'])"
                )
            )

        task = context.current_task
        is_continuation = task is not None
        if task is None:
            task = new_task_from_user_message(message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        # Imported here: ``base`` imports this package lazily and must load first.
        from ..base import SubAgentInput

        message_meta = MessageToDict(message.metadata) if message.HasField("metadata") else {}
        scheduled_job_id = message_meta.get("scheduled_job_id")
        sub_input = SubAgentInput(
            messages=[a2a_message_to_human_message(message)],
            # The graph reads its ids from here (``_extract_tracking_ids``): the
            # conversation is the A2A context, the delegation is the A2A task.
            a2a_tracking={
                self._runnable.tracking_key: {
                    "context_id": task.context_id,
                    "task_id": task.id,
                    "is_complete": False,
                }
            },
            orchestrator_conversation_id=task.context_id,
            scheduled_job_id=int(scheduled_job_id) if scheduled_job_id is not None else None,
            message_formatting=message_meta.get("messageFormatting"),
        )

        try:
            run_config = self._runnable._instrument(sub_input, parent_config)
        except ValueError as exc:
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message(str(exc), context_id=task.context_id, task_id=task.id),
            )
            return

        # One conversation thread, one writer at a time. Two delegations to the
        # same agent in one assistant message used to be refused up front; now the
        # second waits and runs as a follow-up on the same conversation.
        async with self._runnable.local_server.thread_lock(task.context_id):
            pending = await self._runnable.aget_pending_interrupts(run_config)
            if pending and not is_continuation:
                # A NEW task cannot start on a thread parked on a question: its
                # description is work, not an answer, and delivering it to the
                # interrupt reader would reject the pending call with "not an
                # answer". Refuse this task; the parked one still resumes normally.
                logger.info(
                    "[LOCAL A2A] '%s' is parked on %d interrupt(s); rejecting new task %s",
                    self._runnable.name,
                    len(pending),
                    task.id,
                )
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    new_text_message(
                        PARKED_TASK_MESSAGE.format(agent=self._runnable.name),
                        context_id=task.context_id,
                        task_id=task.id,
                    ),
                )
                return
            if pending:
                answer = answer_from_message(message)
                graph_input: Any = build_resume_command(pending, answer)
                logger.info(
                    "[LOCAL A2A] Resuming '%s' task %s with an answer to %d interrupt(s)",
                    self._runnable.name,
                    task.id,
                    len(pending),
                )
            else:
                graph_input = sub_input
            await self._relay(self._runnable.astream_graph(graph_input, run_config), updater, task)

    # -------------------------------------------------------------------- relay

    async def _relay(self, stream: AsyncIterable[StreamEvent], updater: TaskUpdater, task: Any) -> None:
        """Publish the graph's stream as A2A events, in the shared extension vocabulary."""
        main_artifact_id = str(uuid.uuid4())
        thought_artifact_id = f"{main_artifact_id}-thought"
        main_open = False
        thought_open = False
        agent_name = self._runnable.name

        async def close_artifacts() -> None:
            nonlocal main_open, thought_open
            if main_open:
                await updater.add_artifact(
                    [Part(text="")], artifact_id=main_artifact_id, append=True, last_chunk=True, metadata={}
                )
                main_open = False
            if thought_open:
                await updater.add_artifact(
                    [Part(text="")],
                    artifact_id=thought_artifact_id,
                    append=True,
                    last_chunk=True,
                    metadata={},
                    extensions=[INTERMEDIATE_OUTPUT_EXTENSION],
                )
                thought_open = False

        try:
            async for event in stream:
                if isinstance(event, ArtifactUpdate):
                    if not event.content:
                        continue
                    intermediate = isinstance(event.event_metadata, IntermediateOutputMeta)
                    await updater.add_artifact(
                        [Part(text=event.content)],
                        artifact_id=thought_artifact_id if intermediate else main_artifact_id,
                        append=thought_open if intermediate else main_open,
                        last_chunk=False,
                        metadata={"agent_name": agent_name},
                        extensions=[INTERMEDIATE_OUTPUT_EXTENSION] if intermediate else None,
                    )
                    if intermediate:
                        thought_open = True
                    else:
                        main_open = True
                    continue

                if isinstance(event, ErrorEvent):
                    await close_artifacts()
                    await updater.update_status(
                        TaskState.TASK_STATE_FAILED,
                        new_text_message(
                            event.error or "The sub-agent hit an error.", context_id=task.context_id, task_id=task.id
                        ),
                        metadata=_json_safe(event.data.metadata) or None,
                    )
                    return

                if not isinstance(event, TaskUpdate):
                    continue

                meta = event.event_metadata
                if isinstance(meta, WorkPlanMeta):
                    todos = [t if isinstance(t, TodoItem) else TodoItem(**t) for t in meta.todos if t]
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        new_work_plan_message(todos, task.context_id, task.id),
                    )
                    continue
                if isinstance(meta, ClientActionMeta):
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        new_client_action_message(meta.client_action, task.context_id, task.id),
                    )
                    continue
                if isinstance(meta, ActivityLogMeta) or event.status_text:
                    kind = meta.kind if isinstance(meta, ActivityLogMeta) else None
                    await updater.update_status(
                        TaskState.TASK_STATE_WORKING,
                        new_activity_log_message(
                            event.status_text or "", task.context_id, task.id, source=agent_name, kind=kind
                        ),
                    )
                    continue

                # A TaskUpdate carrying data and no classification is the graph's
                # result for this exchange: completed, failed, or a question for
                # the user the agent asked through its structured response.
                data = event.data
                answer = _text_of(data.messages[-1].content) if data.messages else ""
                state = data.state
                if state == TaskState.TASK_STATE_WORKING:
                    if answer:
                        await updater.update_status(
                            TaskState.TASK_STATE_WORKING,
                            new_text_message(answer, context_id=task.context_id, task_id=task.id),
                        )
                    continue
                await close_artifacts()
                await updater.update_status(
                    state,
                    new_text_message(answer, context_id=task.context_id, task_id=task.id),
                    metadata=_json_safe({k: v for k, v in data.metadata.items() if v is not None}) or None,
                )
                if state in TERMINAL_STATES or state in INTERVENTION_STATES:
                    return

            # The stream ended without a result: the graph produced nothing this
            # exchange, which the caller must not mistake for success.
            await close_artifacts()
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                new_text_message("No response received from the sub-agent.", context_id=task.context_id, task_id=task.id),
            )

        except GraphInterrupt as gi:
            # The graph parked on a question. LangGraph suppressed the interrupt
            # into the checkpoint (the graph runs as a standalone root) and the
            # runnable re-raised it after the stream; the task pauses here and the
            # answer arrives as the next message addressed to this task.
            interrupts = tuple(gi.args[0]) if gi.args and gi.args[0] else ()
            await close_artifacts()
            state, status_message = interrupt_status(interrupts, task.context_id, task.id)
            await updater.update_status(
                state,
                status_message,
                metadata={INTERRUPTS_METADATA_KEY: json_safe_interrupts(interrupts)},
            )

    # ------------------------------------------------------------------- cancel

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id or "",
                context_id=context.context_id or "",
                status=TaskStatus(
                    state=TaskState.TASK_STATE_CANCELED,
                    message=new_text_message(
                        "Sub-agent task was cancelled.", context_id=context.context_id, task_id=context.task_id
                    ),
                ),
            )
        )


def interrupt_status(interrupts: Sequence[Any], context_id: str, task_id: str) -> tuple[int, Any]:
    """The (state, status message) a paused graph's interrupts become on the wire.

    Uses the first interrupt to pick the vocabulary — a tool approval is the
    human-in-the-loop extension on ``input_required``, an in-task authorization
    is the in-task-auth extension on ``auth_required``, a client-action round
    trip is the client-action request, anything else is a generic
    ``input_required`` carrying the raw value as data. The full list rides the
    event metadata regardless, so the client can resume every one of them.
    """
    first = interrupts[0] if interrupts else None
    value = getattr(first, "value", first) if first is not None else {}
    if isinstance(first, dict):
        value = first.get("value", first)
    if not isinstance(value, dict):
        value = {}

    kind = interrupt_kind(value)
    if kind == KIND_HITL:
        action_requests = [ar for ar in value.get("action_requests", []) if isinstance(ar, dict)]
        review_configs = value.get("review_configs") or [
            {"action_name": ar.get("name", ""), "allowed_decisions": ["approve", "reject"]} for ar in action_requests
        ]
        description = (action_requests[0].get("description") if action_requests else "") or (
            "Approval required for: " + ", ".join(str(ar.get("name", "")) for ar in action_requests)
        )
        return TaskState.TASK_STATE_INPUT_REQUIRED, new_hitl_interrupt_message(
            description=description,
            action_requests=_json_safe(action_requests),
            review_configs=_json_safe(review_configs),
            context_id=context_id,
            task_id=task_id,
        )
    if kind == KIND_AUTH:
        text = value.get("message") or "Authentication is required to continue."
        payload = AuthPayload.for_service(
            service=value.get("service") or "",
            resource=value.get("tool") or "",
            auth_url=value.get("auth_url") or "",
            description=value.get("message") or "",
            correlation_id=value.get("tool_call_id") or "",
        ).client_payload()
        return TaskState.TASK_STATE_AUTH_REQUIRED, new_auth_required_message(
            text, payload, context_id=context_id, task_id=task_id
        )
    request = value.get("client_action_request")
    if isinstance(request, dict):
        return TaskState.TASK_STATE_INPUT_REQUIRED, new_client_action_request_message(
            _json_safe(request), context_id=context_id, task_id=task_id
        )
    text = value.get("message") if isinstance(value.get("message"), str) else "Additional input is required to continue."
    message = new_text_message(text, context_id=context_id, task_id=task_id)
    if value:
        message.parts.append(Part(data=ParseDict(_json_safe(value), Value()), metadata={"media_type": "application/json"}))
    return TaskState.TASK_STATE_INPUT_REQUIRED, message
