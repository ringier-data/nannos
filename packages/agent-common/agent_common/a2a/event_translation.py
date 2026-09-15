"""A2A task events -> sub-agent ``StreamEvent``s, for every transport.

``A2AClientRunnable`` reads these events off an HTTP stream; a local sub-agent's
``astream`` reads them off the in-process server. Both hand each event to an
:class:`A2AStreamTranslator`, so the orchestrator's dispatch consumes one
vocabulary and cannot tell (nor need to) which transport produced it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Sequence

from a2a.types import (
    Message,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict
from langchain_core.messages import AIMessage
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

from .authentication import AuthenticationMethod, AuthPayload, ServiceAuthRequirement
from .extensions import (
    ACTIVITY_LOG_EXTENSION,
    CLIENT_ACTION_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    WORK_PLAN_EXTENSION,
)
from .stream_events import (
    TERMINAL_STATES,
    ActivityLogMeta,
    ArtifactUpdate,
    ClientActionMeta,
    EventMetadata,
    IntermediateOutputMeta,
    StreamEvent,
    TaskResponseData,
    TaskUpdate,
    WorkPlanMeta,
    parse_event_metadata,
)

logger = logging.getLogger(__name__)

#: States at which one ``message/send`` exchange is over: the task is done, or
#: it is waiting for someone and the stream will not move until they answer.
INTERVENTION_STATES: frozenset[int] = frozenset(
    {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
)

#: Event-metadata key under which the in-process server publishes the raw
#: LangGraph interrupts a paused sub-agent graph holds: ``[{"id", "value"}]``.
INTERRUPTS_METADATA_KEY = "interrupts"


def extract_text_from_parts(parts: Sequence[Part]) -> str:
    """Text content of A2A parts (data parts serialised as JSON)."""
    return a2a_parts_to_content(parts, text_only=True)


def extract_parts(parts: Sequence[Part]) -> list[Dict[str, Any]]:
    """A2A parts as plain dicts, for metadata that wants to carry them along.

    Works with both ``Message.parts`` and ``Artifact.parts``. In A2A v1.0+ a
    ``Part`` is a flat protobuf message; the populated ``content`` oneof field
    (text / raw / url / data) determines the kind.
    """
    parts_data: list[Dict[str, Any]] = []
    for part in parts:
        kind = part.WhichOneof("content")
        metadata = MessageToDict(part.metadata) if part.HasField("metadata") else {}
        if kind == "text":
            parts_data.append({"type": "text", "content": part.text, "metadata": metadata})
        elif kind in ("url", "raw"):
            parts_data.append(
                {
                    "type": "file",
                    "file": part.url if kind == "url" else part.raw,
                    "media_type": part.media_type,
                    "metadata": metadata,
                }
            )
        elif kind == "data":
            parts_data.append({"type": "data", "content": MessageToDict(part.data), "metadata": metadata})
    return parts_data


def parse_auth_payload(task_status: Any) -> Dict[str, Any]:
    """Application metadata for an ``auth-required`` status, following CIBA patterns.

    Reads the in-task-auth DataPart when there is one (both the flat historical
    shape and the ``AuthPayload.client_payload()`` shape with the requirement
    nested under ``auth_requirement``), else falls back to a generic OAuth2
    requirement described by the status text.
    """
    message_text = "Authentication required for downstream service"
    service_name = "unknown_service"
    auth_methods: list[dict[str, Any]] = []
    required_scopes: list[str] = ["read"]

    if task_status.HasField("message") and task_status.message.parts:
        message_text = extract_text_from_parts(task_status.message.parts)
        try:
            for part in task_status.message.parts:
                if part.WhichOneof("content") != "data":
                    continue
                auth_data = MessageToDict(part.data)
                logger.info(f"Parsing structured auth data: {auth_data}")
                requirement = auth_data.get("auth_requirement") if isinstance(auth_data, dict) else None
                source = requirement if isinstance(requirement, dict) else auth_data
                service_name = source.get("service", service_name) or service_name
                for method_data in source.get("auth_methods", []) or []:
                    auth_methods.append(AuthenticationMethod(**method_data).model_dump())
                if source.get("required_scopes"):
                    required_scopes = list(source["required_scopes"])
                if not auth_methods:
                    auth_methods.append(
                        {
                            "method": "oauth2",
                            "description": "OAuth2 authentication required",
                            "instructions": "Please complete the authentication flow",
                        }
                    )
                break
        except Exception as e:
            logger.warning(f"Failed to parse structured auth data: {e}")

    service_auth_requirement = ServiceAuthRequirement(
        service=service_name,
        auth_methods=[AuthenticationMethod(**method) for method in auth_methods]
        if auth_methods
        else [AuthenticationMethod(method="oauth2", description="Authentication required", instructions=message_text)],
        required_scopes=required_scopes,
    )
    auth_payload = AuthPayload(
        requires_auth=True,
        auth_requirement=service_auth_requirement,
        correlation_id=task_status.message.message_id if task_status.HasField("message") else None,
    )
    return {
        "auth_info": auth_payload.model_dump(),
        "auth_methods": [method.model_dump() for method in service_auth_requirement.auth_methods],
        "required_scopes": service_auth_requirement.required_scopes,
        "service": service_name,
        "instructions": message_text,
        "ciba_supported": any(method.method == "ciba" for method in service_auth_requirement.auth_methods),
        "device_code_supported": any(
            method.method == "device_code" for method in service_auth_requirement.auth_methods
        ),
        "corporate_sso_preferred": True,
    }


def synthetic_content(status: Any, artifacts: Optional[Sequence[Any]], app_metadata: Dict[str, Any]) -> str:
    """The text a consumer reads for a task status, state made explicit.

    For failed/incomplete tasks the state is spelled out in the text, so a model
    reading the result cannot mistake an unfinished task for a finished one.
    Operates on a ``TaskStatus`` (+ optional artifacts) so it works for both
    full-Task and status-update stream payloads.
    """
    content = "Task processed"
    if status.HasField("message") and status.message.parts:
        content = extract_text_from_parts(status.message.parts)
    elif status.state == TaskState.TASK_STATE_AUTH_REQUIRED:
        content = app_metadata.get("instructions", "Authentication required")
    elif artifacts:
        first_artifact = artifacts[0]
        if first_artifact.parts:
            first_part = first_artifact.parts[0]
            if first_part.WhichOneof("content") == "text":
                content = first_part.text or "Task completed"

    if status.state == TaskState.TASK_STATE_FAILED:
        lower_content = content.lower()
        if "failed" not in lower_content and "error" not in lower_content:
            content = f"ERROR: Task failed - {content}"
        else:
            content = f"Task execution failed: {content}"
    elif status.state == TaskState.TASK_STATE_WORKING:
        content = f"INCOMPLETE: Agent is still working - {content}"
    elif status.state not in TERMINAL_STATES:
        content = f"Agent status: {TaskState.Name(status.state)} - {content}"
    return content


def task_response(
    task_id: str,
    context_id: str,
    status: Any,
    artifacts: Optional[Sequence[Any]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> TaskResponseData:
    """A ``TaskResponseData`` from A2A task lifecycle fields.

    Works from either a full ``Task`` or a ``TaskStatusUpdateEvent`` (carrying
    only ``TaskStatus`` + ids), since A2A v1.0+ delivers these as separate
    payloads. Protocol fields are typed attributes; application metadata (auth
    details, the server's own extras) lives in ``metadata``.
    """
    app_metadata: dict[str, Any] = {}
    if status.state == TaskState.TASK_STATE_AUTH_REQUIRED:
        app_metadata.update(parse_auth_payload(status))
    if extra_metadata:
        app_metadata.update(extra_metadata)
    content = synthetic_content(status, artifacts, app_metadata)
    return TaskResponseData(
        task_id=task_id,
        context_id=context_id,
        state=status.state,
        messages=[AIMessage(content=content)],
        metadata=app_metadata,
    )


def message_response(message: Message) -> TaskResponseData:
    """A bare A2A ``Message`` reply (no task) as a ``TaskResponseData``."""
    text = extract_text_from_parts(message.parts)
    return TaskResponseData(
        task_id=message.task_id or "",
        context_id=message.context_id or "",
        messages=[AIMessage(content=text)] if text else [],
        metadata={
            "message_id": message.message_id,
            "role": Role.Name(message.role),
            "parts": extract_parts(message.parts),
        },
    )


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def metadata_from_status_message(message: Optional[Message]) -> Optional[EventMetadata]:
    """Classify a status update by the extension its message declares.

    Servers built on ``agent_common.a2a.extensions`` mark an activity-log line, a
    work-plan snapshot or a client-action directive on ``Message.extensions``
    (with the payload in a DataPart), not on event-level metadata flags. Reading
    the extension here gives those events the same typed metadata a producer of
    raw flags gets from :func:`parse_event_metadata`.
    """
    if message is None:
        return None
    extensions = set(message.extensions)
    data: dict[str, Any] = {}
    for part in message.parts:
        if part.WhichOneof("content") == "data":
            parsed = MessageToDict(part.data)
            if isinstance(parsed, dict):
                data = parsed
                break
    if ACTIVITY_LOG_EXTENSION in extensions:
        kind = (MessageToDict(message.metadata) if message.HasField("metadata") else {}).get("kind")
        return ActivityLogMeta(kind=kind if kind == "note" else None)
    if WORK_PLAN_EXTENSION in extensions:
        return WorkPlanMeta(todos=list(data.get("todos", [])))
    if CLIENT_ACTION_EXTENSION in extensions and isinstance(data.get("directive"), dict):
        return ClientActionMeta(client_action=data["directive"])
    return None


class A2AStreamTranslator:
    """Translate the events of one ``message/send`` exchange into ``StreamEvent``s.

    Stateful across the exchange: status-update events carry only ``TaskStatus``
    + ids, so the latest full ``Task`` snapshot is kept to supply artifacts to
    status-derived responses. ``finished`` turns true once the task reached a
    terminal or intervention state — the point at which the exchange is over.
    """

    def __init__(self) -> None:
        self.latest_task: Optional[Task] = None
        self.finished: bool = False
        self.task_updates: int = 0

    def translate(self, event: Any) -> list[StreamEvent]:
        """The ``StreamEvent``s (zero or one) an A2A event becomes."""
        if isinstance(event, Message):
            return [TaskUpdate(data=message_response(event))]

        if isinstance(event, TaskArtifactUpdateEvent):
            artifact = event.artifact
            text_content = "".join(p.text for p in artifact.parts if p.WhichOneof("content") == "text")
            if not text_content:
                return []
            intermediate = INTERMEDIATE_OUTPUT_EXTENSION in set(artifact.extensions)
            return [
                ArtifactUpdate(
                    content=text_content,
                    event_metadata=IntermediateOutputMeta() if intermediate else None,
                    artifact_id=artifact.artifact_id,
                    append=event.append,
                    last_chunk=event.last_chunk,
                    metadata=MessageToDict(artifact.metadata) if artifact.HasField("metadata") else None,
                )
            ]

        if isinstance(event, Task):
            self.latest_task = event
            task_id, context_id, status = event.id, event.context_id, event.status
            artifacts: Optional[Sequence[Any]] = event.artifacts
            raw_event_metadata: dict[str, Any] = {}
        elif isinstance(event, TaskStatusUpdateEvent):
            task_id, context_id, status = event.task_id, event.context_id, event.status
            artifacts = self.latest_task.artifacts if self.latest_task else None
            raw_event_metadata = MessageToDict(event.metadata) if event.HasField("metadata") else {}
        else:
            logger.debug(f"Ignoring unknown stream event: {type(event).__name__}")
            return []

        self.task_updates += 1
        # Event-level metadata that is not an event classification (activity log,
        # work plan, …) is the server's own data about the task — the in-process
        # server's raw interrupts, a remote agent's session handle — and travels
        # on the response so the consumer can act on it.
        extra = {
            k: v
            for k, v in raw_event_metadata.items()
            if k not in ("activity_log", "work_plan", "todos", "intermediate_output", "client_action")
        }
        response = task_response(task_id, context_id, status, artifacts, extra_metadata=extra or None)
        raw_status_text = ""
        if status.HasField("message") and status.message.parts:
            raw_status_text = extract_text_from_parts(status.message.parts)
        if status.state in TERMINAL_STATES or status.state in INTERVENTION_STATES:
            self.finished = True
        event_metadata = parse_event_metadata(raw_event_metadata) or metadata_from_status_message(
            status.message if status.HasField("message") else None
        )
        return [TaskUpdate(data=response, event_metadata=event_metadata, status_text=raw_status_text)]


def json_safe_interrupts(interrupts: Sequence[Any]) -> list[dict[str, Any]]:
    """LangGraph interrupts as the ``[{"id", "value"}]`` list the status event carries."""
    out: list[dict[str, Any]] = []
    for intr in interrupts:
        intr_id = getattr(intr, "id", None)
        if intr_id is None and isinstance(intr, dict):
            intr_id = intr.get("id")
        value = getattr(intr, "value", intr)
        if isinstance(intr, dict) and "value" in intr:
            value = intr["value"]
        out.append({"id": intr_id, "value": _json_safe(value)})
    return out
