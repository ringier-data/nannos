"""Notify-user tool — lets an agent speak to the user MID-TURN, without ending it.

An agent that plans, delegates and calls tools for a minute is silent for that
minute: the user sees mechanical activity lines ("Using search…") but never
learns whether the agent understood the request. This tool is the deliberate
channel for that: one short line, written by the model, addressed to the user,
delivered while the task stays ``working``.

Delivery is fire-and-forget over the LangGraph custom stream — the same
mechanism the todo/work-plan middleware and ``client_action``'s
navigate/highlight directives use. The graph never pauses and no result comes
back; the note is emitted as an activity-log status update
(``urn:nannos:a2a:activity-log:1.0``) carrying ``kind="note"`` in the message
metadata, so clients that already render the activity timeline show it with no
change, and UIs may later style the agent's own words differently from a tool
label.

This is NOT a channel for the answer. The final answer stays in the structured
response (``FinalResponseSchema`` / ``SubAgentResponseSchema``); a note that
carries the answer duplicates it in the transcript.
"""

from __future__ import annotations

import logging

from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

NOTIFY_USER_TOOL_NAME = "notify_user"

USER_NOTE_EVENT = "user_note"
"""Custom-stream event type for a mid-turn note: ``(USER_NOTE_EVENT, {"message": ...})``."""

NOTE_KIND = "note"
"""``kind`` marker travelling with the activity-log message so clients can style it."""

# A note is a glance, not a paragraph — a long one competes with the answer and
# pushes the activity timeline around. Over-long text is truncated rather than
# rejected: the user seeing a clipped note beats the model retrying the call.
MAX_NOTE_CHARS = 400


class NotifyUserInput(BaseModel):
    """Arguments for a mid-turn note."""

    text: str = Field(
        description=(
            "One or two short sentences for the user, in their language. Say what you "
            "understood and what you are about to do — never the answer itself."
        )
    )


async def _notify_user_handler(text: str) -> str:
    note = (text or "").strip()
    if not note:
        return "Nothing was shown (empty note). Continue with the work."
    if len(note) > MAX_NOTE_CHARS:
        note = note[: MAX_NOTE_CHARS - 1].rstrip() + "…"

    try:
        writer = get_stream_writer()
    except Exception:
        writer = None
    if writer is None:
        # Outside a streaming run (a scheduled/batch execution has no client
        # attached). Not an error the model should retry — just say so.
        logger.debug("[NOTIFY-USER] No stream writer in this run; note dropped: %r", note)
        return "No user is watching this run, so the note was not shown. Continue with the work."

    # Custom stream events are (event_type, event_data) tuples — see the
    # orchestrator's consumer loop and TodoStatusMiddleware for the canonical shape.
    writer((USER_NOTE_EVENT, {"message": note}))
    logger.info("[NOTIFY-USER] Note emitted: %s", note)
    return "Note shown to the user. Continue with the work; do not repeat it in your final answer."


def create_notify_user_tool() -> StructuredTool:
    """Create the notify-user tool."""
    return StructuredTool.from_function(
        coroutine=_notify_user_handler,
        name=NOTIFY_USER_TOOL_NAME,
        description=(
            "Tell the user, in one or two short sentences, what you understood and what "
            "you are about to do. The turn continues — this does NOT end it and does NOT "
            "ask a question. Call it once at the start of any request that needs more than "
            "an immediate reply (best in the SAME step as your first real tool call, so no "
            "time is lost), again before a long step, and again if your plan changes. "
            "Never put the answer here: the answer belongs in your final structured "
            "response only."
        ),
        args_schema=NotifyUserInput,
    )
