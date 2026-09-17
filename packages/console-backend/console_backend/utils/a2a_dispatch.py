"""Call an internal A2A agent (agent-runner) over the native a2a-sdk v1.1.0 client.

Replaces hand-rolled v0.3 JSON-RPC (`message/stream`) payloads + manual SSE parsing across
the scheduler engine, debug-agent service, and skill-security service. The a2a SDK client
speaks the native v1.1.0 wire protocol (``SendStreamingMessage``), so the internal agents it
targets (agent-runner) no longer need v0.3 backward-compat enabled.

The streaming events are consumed to completion and collapsed into a
``{"result": <task-dict>}`` value shaped exactly like the legacy JSON-RPC SSE consumer
produced, so the callers' existing result/verdict parsers are unchanged — only the transport
moves to the SDK client.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from a2a.client import A2ACardResolver
from a2a.client.errors import A2AClientError
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskPushNotificationConfig,
    TaskState,
)
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

logger = logging.getLogger(__name__)

# Terminal TaskState -> coarse state string the legacy consumer emitted (others mean "working").
_TERMINAL_STATES: dict[int, str] = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "failed",
    TaskState.TASK_STATE_REJECTED: "failed",
    # Terminal for the RUN while non-terminal for the TASK: the run stops and reports,
    # and the task stays open so its owner's answer has something to address (ADR-0009).
    TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
}

#: States whose payload arrives as the STATUS MESSAGE rather than an artifact.
#:
#: ``BaseAgentExecutor`` adds an artifact only for ``completed``; ``failed``,
#: ``auth_required`` and ``input_required`` publish their content as the status
#: message alone. Harvesting artifacts only was therefore silently dropping the entire
#: result of every one of those — a failed run reached the scheduler with no error text
#: at all, and a parked run would have lost the ask, the task id and the reply target,
#: leaving the scheduler to record it as an ordinary success.
_STATES_CARRYING_PAYLOAD_IN_STATUS = {
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
}

# Agent cards are static per URL; cache them so we don't refetch on every dispatch.
_card_cache: dict[str, AgentCard] = {}

# Gateway statuses that mean nothing was there to answer, as opposed to an agent that
# answered with an error of its own.
_UNREACHABLE_STATUSES = frozenset({502, 503, 504})


class AgentUnreachable(Exception):
    """The agent could not be reached, or stopped answering mid-stream.

    Raised by :func:`dispatch_streaming` in place of the transport's own errors so a
    caller can tell "the agent died" from "the agent failed" without knowing how the
    a2a SDK wraps httpx. The scheduler records the former as an interruption of the
    run and the latter as a failure of the job; the distinction decides whether the
    run counts against the job (see docs/adr/0007-interrupted-runs-get-one-fresh-attempt.md).
    """


def _is_unreachable(exc: BaseException) -> bool:
    """Whether *exc*, or the httpx error the SDK wrapped in it, means nobody answered.

    The SDK wraps every httpx error: the card resolver in AgentCardResolutionError, the
    transports in A2AClientError / A2AClientTimeoutError, all ``from e``. The wrapped
    cause is where the evidence is, so this looks through one level of wrapping.
    A 500 from a running agent is a genuine failure and stays one.
    """
    for candidate in (exc, exc.__cause__):
        if isinstance(candidate, httpx.TransportError):
            return True
        if isinstance(candidate, httpx.HTTPStatusError):
            return candidate.response.status_code in _UNREACHABLE_STATUSES
    return False


async def _resolve_card(agent_url: str, http_client: httpx.AsyncClient) -> AgentCard:
    card = _card_cache.get(agent_url)
    if card is None:
        resolver = A2ACardResolver(http_client, agent_url.rstrip("/"))
        card = await resolver.get_agent_card()
        _card_cache[agent_url] = card
    return card


def _build_message(
    parts: list[dict[str, Any]],
    metadata: dict[str, Any],
    context_id: str | None,
    task_id: str | None = None,
) -> Message:
    """Build a proto A2A Message from the same {kind,text|data} part dicts the callers used to
    put in the JSON-RPC payload.

    *task_id* addresses an EXISTING task instead of opening a new one — the answer to a
    run parked on its owner (ADR-0009). The server accepts it only while that task is
    non-terminal, which is why a parked run leaves its task open.
    """
    a2a_parts: list[Part] = []
    for p in parts:
        kind = p.get("kind")
        if kind == "text":
            text = p.get("text", "")
            if text:
                a2a_parts.append(Part(text=text))
        elif kind == "data":
            a2a_parts.append(Part(data=ParseDict(p.get("data", {}), Value())))
    return Message(
        role=Role.ROLE_USER,
        parts=a2a_parts,
        message_id=str(uuid.uuid4()),
        context_id=context_id or "",
        task_id=task_id or "",
        metadata=metadata,
    )


def _join_text_parts(proto_parts: Any) -> str:
    """Concatenate the text of all text Parts in the sequence."""
    return "".join(part.text for part in proto_parts if part.WhichOneof("content") == "text" and part.text)


async def dispatch_streaming(
    *,
    agent_url: str,
    access_token: str,
    parts: list[dict[str, Any]],
    metadata: dict[str, Any],
    context_id: str | None = None,
    task_id: str | None = None,
    push_config: dict[str, str] | None = None,
    timeout_read: float = 300.0,
) -> dict[str, Any]:
    """Send a streaming A2A message to an internal agent via the native a2a-sdk v1.1.0 client
    and consume the event stream to completion.

    Args:
        agent_url: Base URL of the target agent (e.g. agent-runner).
        access_token: Bearer token presented on the agent card fetch and every request.
        parts: Message parts as ``{"kind": "text", "text": ...}`` / ``{"kind": "data", "data": {...}}``.
        metadata: Message-level metadata (scheduled_job_id, sub_agent_id, watch config, …).
        task_id: Address an existing, non-terminal task instead of opening a new one —
            how an authorization answer reaches a parked run (ADR-0009).
        push_config: Optional ``{"url", "token"}`` registered as the task's push-notification
            target (the SDK injects it into every request's configuration).
        timeout_read: Per-event read timeout — SSE keeps bytes flowing so this is the inter-event gap.

    Returns:
        ``{"result": <task-dict>}`` matching the legacy SSE consumer shape: a Task with
        ``contextId``, ``status.state``, and a single text artifact carrying the final output.

    Raises:
        AgentUnreachable: nothing answered — connect refused, the stream dropped or timed
            out, or a 502/503/504 from a gateway — whether the SDK wrapped it or not.
        Other transport/HTTP/JSON-RPC errors propagate as raised.
    """
    try:
        return await _dispatch_streaming(
            agent_url=agent_url,
            access_token=access_token,
            parts=parts,
            metadata=metadata,
            context_id=context_id,
            task_id=task_id,
            push_config=push_config,
            timeout_read=timeout_read,
        )
    except (httpx.HTTPError, A2AClientError) as e:
        if _is_unreachable(e):
            raise AgentUnreachable(f"{agent_url}: {e}") from e
        raise


async def _dispatch_streaming(
    *,
    agent_url: str,
    access_token: str,
    parts: list[dict[str, Any]],
    metadata: dict[str, Any],
    context_id: str | None,
    task_id: str | None,
    push_config: dict[str, str] | None,
    timeout_read: float,
) -> dict[str, Any]:
    last_text: str | None = None
    #: Text from a status message that IS the result (see
    #: ``_STATES_CARRYING_PAYLOAD_IN_STATUS``). Kept apart from ``last_text`` so an
    #: ordinary "working" status never overwrites a streamed artifact, and preferred at
    #: the end because for those states there is no artifact to prefer instead.
    status_payload_text: str | None = None
    result_context_id: str | None = context_id
    final_state = "completed"

    timeout = httpx.Timeout(connect=10.0, read=timeout_read, write=30.0, pool=5.0)
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as http_client:
        card = await _resolve_card(agent_url, http_client)

        config_kwargs: dict[str, Any] = {"httpx_client": http_client, "streaming": True}
        if push_config:
            config_kwargs["push_notification_config"] = TaskPushNotificationConfig(
                url=push_config["url"], token=push_config.get("token", "")
            )
        client = ClientFactory(ClientConfig(**config_kwargs)).create(card)

        message = _build_message(parts, metadata, context_id, task_id)
        async for chunk in client.send_message(SendMessageRequest(message=message)):
            payload = chunk.WhichOneof("payload")
            if payload == "artifact_update":
                ev = chunk.artifact_update
                result_context_id = ev.context_id or result_context_id
                text = _join_text_parts(ev.artifact.parts)
                if text:
                    # Honor A2A streaming semantics: append=True continues the prior chunk,
                    # otherwise the artifact replaces it.
                    last_text = (last_text or "") + text if ev.append else text
            elif payload == "status_update":
                ev = chunk.status_update
                result_context_id = ev.context_id or result_context_id
                if ev.status.state in _TERMINAL_STATES:
                    final_state = _TERMINAL_STATES[ev.status.state]
                if ev.status.state in _STATES_CARRYING_PAYLOAD_IN_STATUS and ev.status.HasField("message"):
                    text = _join_text_parts(ev.status.message.parts)
                    if text:
                        status_payload_text = text
            elif payload == "task":
                task = chunk.task
                result_context_id = task.context_id or result_context_id
                if task.status.state in _TERMINAL_STATES:
                    final_state = _TERMINAL_STATES[task.status.state]
                for artifact in task.artifacts:
                    text = _join_text_parts(artifact.parts)
                    if text:
                        last_text = text
            elif payload == "message":
                text = _join_text_parts(chunk.message.parts)
                if text:
                    last_text = text

    task_obj: dict[str, Any] = {
        "kind": "task",
        "contextId": result_context_id,
        "status": {"state": final_state},
        "artifacts": [],
    }
    # The status payload wins where there is one: those states publish no artifact, so
    # anything in ``last_text`` is a leftover from an earlier streamed chunk, not the
    # result. Shaped as an artifact regardless, because that is where ``_parse_result``
    # looks and this function's contract is the legacy consumer shape.
    result_text = status_payload_text if status_payload_text is not None else last_text
    if result_text is not None:
        task_obj["artifacts"] = [{"parts": [{"kind": "text", "text": result_text}]}]
    return {"result": task_obj}
