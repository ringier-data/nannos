"""A2A protocol extension URIs and message builder helpers.

Defines custom extensions for structured streaming events, aligned with the
A2A 1.0.0 specification's extension mechanism (Message.extensions, Part.data).

These extensions are declared in the agent card capabilities and referenced
in Message.extensions on relevant status update events so clients can classify
events without relying on ad-hoc metadata boolean flags.
"""

import uuid

from a2a.types import Message, Part, Role
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
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

HUMAN_IN_THE_LOOP_EXTENSION = "urn:nannos:a2a:human-in-the-loop:1.0"
"""Structured interrupt requiring a human decision before tool execution.

When emitted on a status-update with state=input_required, the message carries:
- extensions=[HUMAN_IN_THE_LOOP_EXTENSION]
- A TextPart with a human-readable description
- A DataPart with the structured review request:
  {
    "action_requests": [
      {"name": "tool_name", "args": {...}, "description": "..."}
    ],
    "review_configs": [
      {"action_name": "tool_name", "allowed_decisions": ["approve", "edit", "reject"]}
    ]
  }

To respond, send a message with a DataPart containing:
  {"decisions": [{"type": "approve"|"edit"|"reject", ...}]}

Decision formats:
  - approve: {"type": "approve"}
  - edit:    {"type": "edit", "edited_action": {"name": "tool_name", "args": {...}}}
  - reject:  {"type": "reject", "message": "reason text"}
"""

SCHEDULED_RUN_CONTEXT_EXTENSION = "urn:nannos:a2a:scheduled-run-context:1.0"
"""Request-side extension: scheduled-run provenance attached to thread replies.

When a user replies under a chat message that delivered a scheduled-run
notification, the client attaches a DataPart carrying the run's provenance
(identified by its top-level ``scheduled_run`` key, mirroring the
``decisions`` convention of the human-in-the-loop extension):

  {
    "scheduled_run": {
      "context_id": "<A2A context id of the run on agent-runner>",
      "scheduled_job_id": 7,
      "scheduled_job_run_id": 42,
      "sub_agent_id": 5,
      "sub_agent_name": "Report Agent",
      "prompt": "<the prompt the run was dispatched with>",
      "result_summary": "<the delivered agent output>",
      "scheduler_status": "success" | "failed",
      "error_message": "<set when failed>"
    }
  }

Clients may attach it on every reply in the thread; the orchestrator consumes
it only on the first turn of a conversation (empty checkpoint), where it
injects a synthetic delegation turn reconstructing the run, and ignores it
otherwise. ``context_id`` is provenance data about the sub-agent's own
conversation — it must never be sent as the request's contextId.
"""

ALL_EXTENSIONS = [
    ACTIVITY_LOG_EXTENSION,
    WORK_PLAN_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    FEEDBACK_REQUEST_EXTENSION,
    HUMAN_IN_THE_LOOP_EXTENSION,
    SCHEDULED_RUN_CONTEXT_EXTENSION,
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
        role=Role.ROLE_AGENT,
        parts=[Part(text=text)],
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        extensions=[ACTIVITY_LOG_EXTENSION],
        metadata=metadata,
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
        role=Role.ROLE_AGENT,
        parts=[
            Part(
                data=ParseDict({"todos": [t.model_dump(exclude_none=True) for t in todos]}, Value()),
                metadata={"media_type": "application/json"},
            )
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
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
        role=Role.ROLE_AGENT,
        parts=[
            Part(
                data=ParseDict({"sub_agents": sub_agents_involved or []}, Value()),
                metadata={"media_type": "application/json"},
            )
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        extensions=[FEEDBACK_REQUEST_EXTENSION],
    )


def new_hitl_interrupt_message(
    description: str,
    action_requests: list[dict],
    review_configs: list[dict],
    context_id: str | None = None,
    task_id: str | None = None,
) -> Message:
    """Build a Message for a human-in-the-loop interrupt (tool approval required).

    The message carries:
      - A TextPart with the human-readable description
      - A DataPart with action_requests + review_configs for structured client rendering
      - extensions=[HUMAN_IN_THE_LOOP_EXTENSION] for client classification

    Clients respond with a DataPart containing {"decisions": [...]}.
    """
    return Message(
        role=Role.ROLE_AGENT,
        parts=[
            Part(text=description),
            Part(
                data=ParseDict(
                    {
                        "action_requests": action_requests,
                        "review_configs": review_configs,
                    },
                    Value(),
                ),
                metadata={"media_type": "application/json"},
            ),
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        extensions=[HUMAN_IN_THE_LOOP_EXTENSION],
    )
