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
"""Tool usage, delegation, and mid-turn note events displayed as a timeline.

Message metadata may carry ``kind``: absent for a mechanical line ("Using
search…", "Delegating to …"), ``"note"`` for a line the agent wrote for the user
itself through the ``notify_user`` tool. Both keep the task in ``working`` —
neither ends the turn."""

WORK_PLAN_EXTENSION = "urn:nannos:a2a:work-plan:1.0"
"""Structured progress tracking with a todo checklist."""

INTERMEDIATE_OUTPUT_EXTENSION = "urn:nannos:a2a:intermediate-output:1.0"
"""Streaming draft content from sub-agents (may be rewritten by orchestrator)."""

FEEDBACK_REQUEST_EXTENSION = "urn:nannos:a2a:feedback-request:1.0"
"""Non-blocking hint asking clients to prompt for user feedback."""

CLIENT_ACTION_EXTENSION = "urn:nannos:a2a:client-action:1.0"
"""Agent→widget directive targeting a host-registered ontology object.

The message carries a DataPart with {"directive": {...}}:
- kind: "apply" | "highlight" | "navigate"
- target: {"type": <ontology type>, "id": <instance id>}  (apply/highlight)
- values: {...}   (apply — field values written through the host's form layer)
- to: <string>    (navigate)
- confirm: bool   (apply — widget must ask the human before applying)

The widget executes directives ONLY against objects the host app registered
via the Embed SDK; unknown targets are refused client-side (see
embed-sdk core client-action executor).
"""

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

CONVERSATION_ORIGIN_EXTENSION = "urn:nannos:a2a:conversation-origin:1.0"
"""Request-side extension: what a new conversation originates from.

A conversation is sometimes opened *about* prior work the orchestrator never
saw — a scheduled run's delivered output, a reported bug, an old conversation
found in search. Clients describe that origin as a DataPart (identified by its
top-level ``origin`` key, mirroring the ``decisions`` convention of the
human-in-the-loop extension):

  {
    "origin": {
      "kind": "<origin kind>",
      ...kind-specific fields
    }
  }

Clients may attach it on every message of the thread/channel context it
belongs to; the orchestrator consumes it only on the first turn of a
conversation (empty checkpoint), where the kind's registered builder
reconstructs the origin as synthetic history, and ignores it otherwise
(unknown kinds are skipped with a log line, never an error). This carries
*data*, not state: it reconstructs context for the model — it does not fork
or resume the referenced conversation's checkpoint. A kind MAY additionally
enable cross-service conversation adoption (below), but only from ids the
orchestrator re-resolves server-side for the authenticated user — never from
the DataPart's own values, which are client-supplied and untrusted.

Registered kinds:

``scheduled_run`` — a scheduled job run executed on agent-runner whose output
was delivered to the user (the reply arrives under that notification):

  {
    "origin": {
      "kind": "scheduled_run",
      "context_id": "<A2A context id of the run on agent-runner>",
      "scheduled_job_id": 7,
      "scheduled_job_run_id": 42,
      "sub_agent_id": 5,
      "sub_agent_name": "Report Agent",
      "prompt": "<the prompt the run was dispatched with>",
      "result_summary": "<the delivered agent output>",
      "scheduler_status": "success" | "failed",
      "error_message": "<set when failed>",
      "task_state": "completed" | "input_required" | "failed"  // optional: the
        // sub-agent's terminal A2A task state. "input_required" means the run
        // did not finish — it asked the user a question and its conversation
        // is waiting for the answer; the reconstruction then frames the reply
        // as that answer and steers toward forwarding it to the sub-agent.
    }
  }

Reconstructed as a synthetic delegation turn (job prompt -> ``task`` tool
call -> run output). ``context_id`` is provenance data about the sub-agent's
own conversation — it must never be sent as the request's contextId.

The orchestrator additionally attempts conversation adoption: it resolves
the job and run via console-backend under the authenticated user's token
(ownership check + server-stored ``conversation_id``, ignoring the
DataPart's ``context_id``) and seeds ``a2a_tracking`` so a follow-up
delegation to that sub-agent continues the run's own conversation — the
workflow continues (e.g. a run that ended asking the user for input)
instead of the sub-agent starting blank. One contract, two continuity
mechanisms: REMOTE runs resume by contextId on the executing server;
LOCAL/AUTOMATED runs are forked — the run's checkpoint is copied from the
shared checkpoint tables into the conversation's own thread on first
delegation. Automated (scheduler-only) sub-agents become delegable inside
the adopting conversation only. Foundry runs are not adopted (their
continuity is a session rid the provenance does not carry).
"""

IN_TASK_AUTH_EXTENSION = "urn:nannos:a2a:in-task-auth:1.0"
"""Structured auth payload on A2A's own ``auth-required`` task state.

The STATE is protocol (``TaskState.TASK_STATE_AUTH_REQUIRED``, understood by any
A2A client); A2A deliberately leaves the *schema* of what such a status carries
undefined, so that schema is what this extension declares — the ``AuthPayload``
models in ``agent_common.a2a.authentication.in_task_auth``.

The extension is ADDITIVE: the status keeps its TextPart, so a client that never
negotiated the URN sees exactly what it saw before. What it gains is a DataPart::

  {
    "requires_auth": true,
    "auth_requirement": {
      "service": "github",
      "auth_methods": [{"method": "oauth2", "auth_url": "https://…", "description": "…"}],
      "required_scopes": ["repo"]
    },
    "correlation_id": "<the tool call this blocked>"
  }

Only the requirement half ever crosses the wire: ``AuthPayload.client_payload()``
cannot emit ``oauth2_client_config``, which carries a client secret.

Unlike the human-in-the-loop extension this carries no ``review_configs``:
nothing is being proposed, so there is no decision for the gateway to receive.
The client resumes the task with a message once the user has authenticated.
"""

# Keep in sync with the repo-root a2a-extensions.json registry (pinned by
# tests/test_a2a_extensions_conformance.py) — console-backend's negotiation
# header and the embed SDK carry their own copies of the same list.
ALL_EXTENSIONS = [
    ACTIVITY_LOG_EXTENSION,
    WORK_PLAN_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    FEEDBACK_REQUEST_EXTENSION,
    HUMAN_IN_THE_LOOP_EXTENSION,
    CONVERSATION_ORIGIN_EXTENSION,
    CLIENT_ACTION_EXTENSION,
    IN_TASK_AUTH_EXTENSION,
]


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def new_activity_log_message(
    text: str,
    context_id: str | None = None,
    task_id: str | None = None,
    source: str | None = None,
    kind: str | None = None,
) -> Message:
    """Build a Message for an activity-log status update (tool usage, delegation).

    The message carries:
      - A TextPart with the human-readable status text
      - extensions=[ACTIVITY_LOG_EXTENSION] for client classification
      - Optional source attribution in message metadata
      - Optional ``kind`` in message metadata: absent for the ordinary mechanical
        line (a tool ran, work was delegated), ``"note"`` when the agent itself
        addressed the user mid-turn via the ``notify_user`` tool. Clients that
        ignore it still render the line; clients that read it can style the
        agent's own words apart from a tool label.
    """
    metadata = {}
    if source:
        metadata["source"] = source
    if kind:
        metadata["kind"] = kind

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


def new_client_action_message(
    directive: dict,
    context_id: str | None = None,
    task_id: str | None = None,
) -> Message:
    """Build a Message carrying a client-action directive.

    The message carries:
      - A DataPart with {"directive": {...}} as structured JSON
      - extensions=[CLIENT_ACTION_EXTENSION] for client classification
    """
    return Message(
        role=Role.ROLE_AGENT,
        parts=[
            Part(
                data=ParseDict({"directive": directive}, Value()),
                metadata={"media_type": "application/json"},
            )
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        extensions=[CLIENT_ACTION_EXTENSION],
    )


def new_client_action_request_message(
    request: dict,
    context_id: str | None = None,
    task_id: str | None = None,
) -> Message:
    """Build a Message carrying a client-action REQUEST awaiting a result.

    Distinct from ``new_client_action_message`` (fire-and-forget ``{"directive"}``):
    this one is emitted with the ``input_required`` task state and carries
    ``{"request": {"id", "directive"}}``. The Embed SDK executes the directive and
    resumes the turn with a ``{"decisions": [{"id", "type": "approve",
    "client_action_result": {...}}]}`` DataPart — the same channel HITL uses.
    """
    return Message(
        role=Role.ROLE_AGENT,
        parts=[
            Part(
                data=ParseDict({"request": request}, Value()),
                metadata={"media_type": "application/json"},
            )
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        extensions=[CLIENT_ACTION_EXTENSION],
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


def new_auth_required_message(
    description: str,
    auth_payload: dict,
    context_id: str | None = None,
    task_id: str | None = None,
) -> Message:
    """Build the Message for an ``auth-required`` status (in-task authentication).

    The message carries:
      - A TextPart with the human-readable text — kept unconditionally, so a
        client that never negotiated the extension is unaffected
      - A DataPart with an ``AuthPayload.client_payload()`` body
      - extensions=[IN_TASK_AUTH_EXTENSION] for client classification

    The caller is responsible for passing a payload built by ``client_payload()``:
    the full ``AuthPayload`` dump carries a client secret and must not be sent.
    """
    return Message(
        role=Role.ROLE_AGENT,
        parts=[
            Part(text=description),
            Part(
                data=ParseDict(auth_payload, Value()),
                metadata={"media_type": "application/json"},
            ),
        ],
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        extensions=[IN_TASK_AUTH_EXTENSION],
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
