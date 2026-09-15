"""LangChain messages <-> A2A messages, one implementation for every A2A path.

The remote client (``A2AClientRunnable``), the in-process server every local
sub-agent runs behind (``agent_common.a2a.local_server``) and agent-runner all
cross the same boundary. Keeping the conversion here means a message built on
one side is read back identically on the other, whichever transport carried it.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional, Sequence

from a2a.types import Message, Part, Role, TaskStatus
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from langchain_core.messages import HumanMessage
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

from .extensions import (
    CLIENT_ACTION_EXTENSION,
    HUMAN_IN_THE_LOOP_EXTENSION,
    IN_TASK_AUTH_EXTENSION,
)

logger = logging.getLogger(__name__)

#: Message-metadata key under which a client proposes the id of the task a NEW
#: message opens. A2A lets the server mint task ids; this is the one place the
#: orchestrator needs to name a task before it exists — the id is derived from
#: the delegating tool call, so a LangGraph replay of that call finds the task
#: it already opened (``local_server.context_builder``). A server that does not
#: recognise the key simply ignores it.
PROPOSED_TASK_ID_KEY = "nannos_proposed_task_id"

#: Top-level keys that mark a DataPart as an ANSWER to a paused task rather
#: than as content. Mirrors the client contracts documented on the extensions.
_ANSWER_KEYS = ("decisions", "authorization")


def human_messages_to_a2a_message(
    human_messages: Sequence[HumanMessage],
    context_id: Optional[str],
    task_id: Optional[str],
    *,
    scheduled_job_id: Optional[int] = None,
    message_formatting: Optional[str] = None,
    source: str = "Orchestrator",
    extra_metadata: Optional[dict[str, Any]] = None,
) -> Message:
    """Aggregate LangChain HumanMessages into one A2A user Message.

    Processes all messages in order, aggregating their parts while preserving
    content-block order, so a multi-turn hand-off travels as one message.

    Args:
        human_messages: The messages to send.
        context_id: Conversation the message belongs to (A2A ``contextId``).
        task_id: Task the message continues, if any — set when answering a paused
            task, empty when opening a new one.
        scheduled_job_id: Cost-attribution hint for agent-runner dispatches.
        message_formatting: Rendering rules of the delivering channel, sent as
            ``messageFormatting`` — the key an interactive client uses — so the
            receiving agent applies them through its own request-metadata path.
        source: Who is speaking; stamped into the metadata for the receiving side.
        extra_metadata: Further message-metadata entries (e.g. a proposed task id).
    """
    message_metadata: dict[str, Any] = {"source": source, "timestamp": time.time()}
    if scheduled_job_id is not None:
        message_metadata["scheduled_job_id"] = scheduled_job_id
    if message_formatting:
        message_metadata["messageFormatting"] = message_formatting
    if extra_metadata:
        message_metadata.update({k: v for k, v in extra_metadata.items() if v is not None})

    all_parts: list[Part] = []
    for human_message in human_messages:
        content = human_message.content
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        all_parts.append(Part(text=text, metadata=message_metadata))
                elif block_type == "non_standard":
                    value = block.get("value", {})
                    if isinstance(value, dict) and value.get("media_type") == "application/json":
                        all_parts.append(
                            Part(
                                data=ParseDict(value.get("data", {}), Value()),
                                metadata={"media_type": "application/json", **message_metadata},
                            )
                        )
                elif block_type in ("image", "audio", "video", "file"):
                    file_part = content_block_to_file_part(block)
                    if file_part is not None:
                        all_parts.append(file_part)
                else:
                    logger.warning(f"Unsupported content block type: {block_type}. Skipping block.")
        else:
            text = content if isinstance(content, str) else str(content)
            if text:
                all_parts.append(Part(text=text, metadata=message_metadata))

    return Message(
        role=Role.ROLE_USER,
        parts=all_parts,
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        metadata=message_metadata,
    )


def content_block_to_file_part(block: dict) -> Optional[Part]:
    """A LangChain file/image/audio/video block as an A2A ``url`` Part, or None without a URL."""
    url = block.get("url", "")
    if not url:
        return None
    mime_type = block.get("mime_type")
    if mime_type:
        return Part(url=url, media_type=mime_type)
    return Part(url=url)


def a2a_message_to_human_message(message: Message) -> HumanMessage:
    """The HumanMessage a local sub-agent graph reads for an incoming A2A message.

    Text-only messages become a plain string (what every prompt path expects);
    anything carrying files or structured data keeps the typed content blocks
    the multimodal input path validates against the agent's input modes.
    """
    blocks = a2a_parts_to_content(message.parts, text_only=False)
    if all(isinstance(b, dict) and b.get("type") == "text" for b in blocks):
        return HumanMessage(content="\n".join(b.get("text", "") for b in blocks))
    return HumanMessage(content=blocks)  # type: ignore[arg-type]


def answer_from_message(message: Message) -> Any:
    """What a message says in reply to a paused task.

    A structured answer — the ``decisions`` / ``authorization`` DataParts the
    extension contracts define — is returned as the dict it arrived as. Anything
    else is the user's own words as text: both interrupt readers
    (``agent_common.core.hitl_resume``) classify words, so they must survive the
    trip untouched. ``{}`` when the message carries nothing at all, which the
    readers treat as "no answer" (never as consent).
    """
    for part in message.parts:
        if part.WhichOneof("content") == "data":
            data = MessageToDict(part.data)
            if isinstance(data, dict) and any(key in data for key in _ANSWER_KEYS):
                return data
    # Words only: a data part that is not an answer (an empty ``{}`` reply, say)
    # must not be read back as the text "{}".
    text = "\n".join(part.text for part in message.parts if part.WhichOneof("content") == "text").strip()
    return text if text else {}


def answer_to_human_message(answer: Any) -> HumanMessage:
    """The inverse of :func:`answer_from_message`, for a client sending a reply.

    Structured answers ride a JSON ``non_standard`` block (which
    :func:`human_messages_to_a2a_message` turns into a DataPart); words ride as
    text. ``None`` means the resume carried nothing, which travels as ``{}`` so the
    far side reads "no answer" rather than an empty message.
    """
    if isinstance(answer, str):
        return HumanMessage(content=answer)
    payload = answer if isinstance(answer, dict) else {}
    return HumanMessage(
        content=[{"type": "non_standard", "value": {"media_type": "application/json", "data": payload}}]  # type: ignore[list-item]
    )


def interrupt_value_from_status(status: TaskStatus) -> dict[str, Any]:
    """Rebuild the interrupt value a paused task's status message describes.

    The in-process server puts the raw LangGraph interrupt value on the status
    *event* (``metadata.interrupts``), which a live stream reads directly. A
    stored ``Task`` keeps only the status message, so a caller replaying a
    delegation (``tasks/get``) reconstructs the value from the message's
    extension DataPart instead. The shape only has to be faithful enough for
    ``interrupt_kind`` and display: what the user is asked is already settled.
    """
    if not status.HasField("message"):
        return {}
    message = status.message
    data: dict[str, Any] = {}
    for part in message.parts:
        if part.WhichOneof("content") == "data":
            parsed = MessageToDict(part.data)
            if isinstance(parsed, dict):
                data = parsed
                break
    text = a2a_parts_to_content(message.parts, text_only=True)
    extensions = set(message.extensions)
    if HUMAN_IN_THE_LOOP_EXTENSION in extensions or "action_requests" in data:
        return {"action_requests": data.get("action_requests", []), "review_configs": data.get("review_configs", [])}
    if IN_TASK_AUTH_EXTENSION in extensions:
        requirement = data.get("auth_requirement") or {}
        methods = requirement.get("auth_methods") or []
        auth_url = next((m.get("auth_url") for m in methods if isinstance(m, dict) and m.get("auth_url")), "")
        from a2a.types import TaskState  # local import: keep protobuf enum out of the module surface

        return {
            "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
            "message": text,
            "auth_url": auth_url or "",
            "tool": requirement.get("resource") or "",
            "service": requirement.get("service") or "",
        }
    if CLIENT_ACTION_EXTENSION in extensions and isinstance(data.get("request"), dict):
        return {"client_action_request": data["request"]}
    return data
