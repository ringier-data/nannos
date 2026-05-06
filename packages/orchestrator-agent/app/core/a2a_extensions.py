"""A2A protocol extension URIs and message builder helpers.

Defines custom extensions for structured streaming events, aligned with the
A2A 1.0.0 specification's extension mechanism (Message.extensions, Part.data).

These extensions are declared in the agent card capabilities and referenced
in Message.extensions on relevant status update events so clients can classify
events without relying on ad-hoc metadata boolean flags.
"""

import uuid

from a2a.types import DataPart, Message, Part, Role, TextPart
from ringier_a2a_sdk.models import TodoItem

# ---------------------------------------------------------------------------
# Extension URIs
# ---------------------------------------------------------------------------

ACTIVITY_LOG_EXTENSION = "urn:nannos:a2a:activity-log:1.0"
"""Tool usage and delegation status events displayed as a timeline."""

WORK_PLAN_EXTENSION = "urn:nannos:a2a:work-plan:1.0"
"""Structured progress tracking with a todo checklist."""

INTERMEDIATE_OUTPUT_EXTENSION = "urn:nannos:a2a:intermediate-output:1.0"
"""Streaming draft content from sub-agents (may be rewritten by orchestrator)."""

FEEDBACK_REQUEST_EXTENSION = "urn:nannos:a2a:feedback-request:1.0"
"""Non-blocking hint asking clients to prompt for user feedback."""

ALL_EXTENSIONS = [
    ACTIVITY_LOG_EXTENSION,
    WORK_PLAN_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    FEEDBACK_REQUEST_EXTENSION,
]


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def new_activity_log_message(
    text: str,
    context_id: str | None = None,
    task_id: str | None = None,
    source: str | None = None,
) -> Message:
    """Build a Message for an activity-log status update (tool usage, delegation).

    The message carries:
      - A TextPart with the human-readable status text
      - extensions=[ACTIVITY_LOG_EXTENSION] for client classification
      - Optional source attribution in message metadata
    """
    metadata = {}
    if source:
        metadata["source"] = source

    return Message(
        role=Role.agent,
        parts=[Part(root=TextPart(text=text))],
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        task_id=task_id,
        extensions=[ACTIVITY_LOG_EXTENSION],
        metadata=metadata or None,
    )


def new_work_plan_message(
    todos: list[TodoItem],
    context_id: str | None = None,
    task_id: str | None = None,
) -> Message:
    """Build a Message for a work-plan status update (todo checklist).

    The message carries:
      - A DataPart with {"todos": [...]} as structured JSON
      - extensions=[WORK_PLAN_EXTENSION] for client classification
    """
    return Message(
        role=Role.agent,
        parts=[
            Part(
                root=DataPart(
                    data={"todos": [t.model_dump(exclude_none=True) for t in todos]},
                    metadata={"media_type": "application/json"},
                )
            )
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        task_id=task_id,
        extensions=[WORK_PLAN_EXTENSION],
    )


def new_feedback_request_message(
    context_id: str | None = None,
    task_id: str | None = None,
    sub_agents_involved: list[str] | None = None,
) -> Message:
    """Build a Message for a feedback-request status update.

    Emitted as a fire-and-forget hint when a complex task completes.
    Clients render a non-blocking feedback prompt (thumbs up/down).

    The message carries:
      - A DataPart with {"sub_agents": [...]} for attribution
      - extensions=[FEEDBACK_REQUEST_EXTENSION] for client classification
    """
    return Message(
        role=Role.agent,
        parts=[
            Part(
                root=DataPart(
                    data={"sub_agents": sub_agents_involved or []},
                    metadata={"media_type": "application/json"},
                )
            )
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        task_id=task_id,
        extensions=[FEEDBACK_REQUEST_EXTENSION],
    )
