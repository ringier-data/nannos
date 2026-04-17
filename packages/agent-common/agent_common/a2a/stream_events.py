"""Pydantic models for sub-agent streaming events.

These models replace the loose ``Dict[str, Any]`` dicts previously yielded
by ``_astream_impl`` / ``astream``.  A discriminated union on the ``type``
field lets consumers pattern-match with ``isinstance`` instead of string-key
lookups, and gives every event a documented, validated shape.

The three event kinds mirror the A2A protocol concepts:

* **TaskUpdate** — progress, activity-log, work-plan, or terminal result
* **ArtifactUpdate** — streaming content chunks (main response or
  intermediate output)
* **ErrorEvent** — recoverable or fatal error signals

Usage::

    from agent_common.a2a.stream_events import (
        TaskUpdate, ArtifactUpdate, ErrorEvent, StreamEvent,
        ActivityLogMeta, WorkPlanMeta,
    )

    # Yielding
    yield TaskUpdate(status_text="Searching…",
                     event_metadata=ActivityLogMeta())

    # Consuming
    async for event in runnable.astream(…):
        if isinstance(event, TaskUpdate) and event.event_metadata:
            if isinstance(event.event_metadata, WorkPlanMeta):
                handle_todos(event.event_metadata.todos)
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from a2a.types import TaskState
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, computed_field

# Terminal states where the task is done (success or failure).
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.completed, TaskState.failed, TaskState.canceled, TaskState.rejected}
)

# ---------------------------------------------------------------------------
# Sub-agent response data — A2A-inspired typed payload
# ---------------------------------------------------------------------------
#
# Follows the A2A protocol pattern: core lifecycle fields are typed
# attributes; app-specific extensions (auth requirements, agent-specific
# extras like ``foundry_session_rid``) live in ``metadata``.
#
# Both local (``_build_response``) and remote (``_handle_task_response``,
# ``_handle_message_response``) producers emit a ``TaskResponseData``.
# Remote A2A ``Message`` responses are converted into ``TaskResponseData``
# with their text in ``messages`` and protocol fields in ``metadata``.
# ---------------------------------------------------------------------------


class TaskResponseData(BaseModel):
    """Task-lifecycle response produced by local and remote sub-agents.

    Covers completed results, auth-required interrupts, input-required
    pauses, and error payloads.  A2A ``Message`` responses are also
    normalised into this shape.

    The boolean properties ``is_complete``, ``requires_input``, and
    ``requires_auth`` are derived from ``state`` (mirroring the A2A
    protocol) and included in ``model_dump()`` via ``@computed_field``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: Literal["task"] = "task"
    task_id: str = ""
    context_id: str = ""
    state: TaskState = TaskState.working
    messages: list[BaseMessage] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_complete(self) -> bool:
        return self.state in TERMINAL_STATES

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_input(self) -> bool:
        return self.state == TaskState.input_required

    @computed_field  # type: ignore[prop-decorator]
    @property
    def requires_auth(self) -> bool:
        return self.state == TaskState.auth_required


# ---------------------------------------------------------------------------
# Event metadata variants (nested in TaskUpdate / ArtifactUpdate)
# ---------------------------------------------------------------------------


class ActivityLogMeta(BaseModel):
    """Marker metadata for activity-log (tool-call / delegation) events."""

    activity_log: Literal[True] = True


class WorkPlanMeta(BaseModel):
    """Metadata carrying a todo-list snapshot for work-plan events."""

    work_plan: Literal[True] = True
    todos: List[Any] = Field(default_factory=list)


class IntermediateOutputMeta(BaseModel):
    """Marker metadata for intermediate (thinking) output chunks."""

    intermediate_output: Literal[True] = True


EventMetadata = Union[ActivityLogMeta, WorkPlanMeta, IntermediateOutputMeta]

# ---------------------------------------------------------------------------
# Top-level stream event models
# ---------------------------------------------------------------------------


class TaskUpdate(BaseModel):
    """Progress or terminal update from a sub-agent.

    Covers working-state status messages, activity-log entries,
    work-plan snapshots, and the final completed/failed result.

    Producers set the state on the inner ``TaskResponseData``;
    consumers access it via ``event.data.state``.
    """

    type: Literal["task_update"] = "task_update"
    data: TaskResponseData = Field(default_factory=TaskResponseData)
    status_text: str = ""
    event_metadata: Optional[EventMetadata] = None


class ArtifactUpdate(BaseModel):
    """Streaming content chunk (main response or intermediate output).

    Local agents yield just ``content`` (and optionally ``event_metadata``);
    remote agents additionally carry ``artifact_id``, ``append``, etc.
    """

    type: Literal["artifact_update"] = "artifact_update"
    content: str = ""
    event_metadata: Optional[IntermediateOutputMeta] = None

    # Remote-only fields (from A2AClientRunnable)
    artifact_id: Optional[str] = None
    append: Optional[bool] = None
    last_chunk: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None


class ErrorEvent(BaseModel):
    """Recoverable or fatal error signal."""

    type: Literal["error"] = "error"
    error: str = ""
    data: TaskResponseData = Field(default_factory=TaskResponseData)
    error_type: str = ""
    requires_retry: bool = False


# Discriminated union — matches on the ``type`` field.
StreamEvent = Union[TaskUpdate, ArtifactUpdate, ErrorEvent]


def parse_event_metadata(raw: Optional[Dict[str, Any]]) -> Optional[EventMetadata]:
    """Parse a raw metadata dict (e.g. from A2A protocol) into a typed EventMetadata."""
    if not raw:
        return None
    if raw.get("work_plan"):
        return WorkPlanMeta(todos=raw.get("todos", []))
    if raw.get("activity_log"):
        return ActivityLogMeta()
    if raw.get("intermediate_output"):
        return IntermediateOutputMeta()
    return None
