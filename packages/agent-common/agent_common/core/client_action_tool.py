"""Client-action tool — lets an agent act on ontology objects the host
application registered on the user's screen (Embedded Nannos).

Shared across the orchestrator and any LOCAL sub-agent (the embedded domain
agent). The tool does NOT touch any backend — the browser executes every
directive against host-registered handles. Two delivery modes:

- ``navigate`` / ``highlight`` — fire-and-forget: emitted over the LangGraph
  custom stream (same mechanism as the todo/work-plan middleware); the executor
  wraps it in a `urn:nannos:a2a:client-action:1.0` status message. No result
  comes back (the user sees the effect immediately).

- ``apply`` — a ROUND TRIP: the tool ``interrupt()``s with the directive in the
  interrupt value; the executor emits it as ``input_required`` (same extension,
  ``{"request": ...}`` payload), the SDK executes it and auto-resumes with a
  ``client_action_result`` decision, which this tool returns to the model. The
  agent therefore KNOWS which fields landed and which were rejected, instead of
  assuming success. The directive rides the interrupt value ONLY — nothing is
  emitted before ``interrupt()``, so the resume replay of this handler cannot
  double-execute (a write-scope ``apply`` still goes through the host's own
  form layer and the human still submits).

Register per-turn ONLY when the client sent a non-empty ``clientObjects``
manifest with the message.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, Optional

from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.config import get_stream_writer
from langgraph.types import interrupt
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CLIENT_ACTION_TOOL_NAME = "client_action"


class ClientActionInput(BaseModel):
    """Arguments for a client-action directive."""

    kind: Literal["apply", "highlight", "navigate", "read_current_page"] = Field(
        description=(
            "apply: write field values into a registered on-screen object (e.g. fill a form) — "
            "returns which fields landed vs. were rejected; "
            "highlight: draw the user's attention to a registered object/field; "
            "navigate: ask the host app to open a path; "
            "read_current_page: ask the application for a snapshot of what the user currently "
            "sees (page state the host exposes: rows, filters, unsaved values) — use when "
            "<current_page>/<client_objects> lack the detail you need."
        )
    )
    target_type: Optional[str] = Field(
        default=None, description="Ontology type of the target object (from the client objects manifest)."
    )
    target_id: Optional[str] = Field(
        default=None, description="Instance id of the target object (from the client objects manifest)."
    )
    values: Optional[dict[str, Any]] = Field(
        default=None,
        description="apply only: field values to write. Keys must match the object's fields.",
    )
    field: Optional[str] = Field(default=None, description="highlight only: specific field to highlight.")
    to: Optional[str] = Field(default=None, description="navigate only: the path/route to open.")
    confirm: bool = Field(
        default=True,
        description="apply only: ask the user to confirm before writing (keep true unless trivially safe).",
    )
    tool_call_id: Annotated[str, InjectedToolCallId] = Field(default="")


def _render_client_action_result(kind: str, result: Any) -> str:
    """Render the client's result payload into honest prose for the model."""
    if not isinstance(result, dict):
        return f"The client returned no usable result for '{kind}'; do not assume it succeeded."
    if not result.get("ok"):
        reason = result.get("reason") or "unknown"
        detail = result.get("detail") or result.get("message") or ""
        if reason == "unknown-target":
            return (
                "The action FAILED: the target object is no longer on the user's screen "
                "(they may have navigated away). Check <current_page>/<client_objects> and adjust."
            )
        if reason == "no-result":
            return (
                "No result came back from the application (the user may have replied instead, "
                "or closed the page). Do NOT assume the action happened."
            )
        return f"The action FAILED ({reason}). {detail}".strip()
    if kind == "read_current_page":
        content = result.get("content")
        if isinstance(content, str) and content.strip():
            return "Current page state, as reported by the application (sanitized client-side):\n" + content
        return "The application returned an empty page snapshot."
    if kind == "apply":
        applied = result.get("applied") or []
        rejected = result.get("rejected") or []
        lines = ["The client executed the apply."]
        if applied:
            lines.append(f"Fields written into the form: {', '.join(str(f) for f in applied)}.")
        if rejected:
            rendered = "; ".join(
                f"{r.get('field')}" + (f" ({r.get('reason')})" if r.get("reason") else "")
                for r in rejected
                if isinstance(r, dict)
            )
            lines.append(
                f"Fields REJECTED by the form's validation (NOT written): {rendered}. "
                "Correct these values and apply again, or tell the user."
            )
        lines.append("Nothing is persisted — the user still reviews and saves the form themselves.")
        return " ".join(lines)
    return f"The client executed '{kind}' successfully."


async def _client_action_handler(
    kind: str,
    target_type: str | None = None,
    target_id: str | None = None,
    values: dict[str, Any] | None = None,
    field: str | None = None,
    to: str | None = None,
    confirm: bool = True,
    tool_call_id: str = "",
) -> str:
    directive: dict[str, Any] = {"kind": kind}
    if kind in ("apply", "highlight"):
        if not target_type or not target_id:
            return "Error: apply/highlight require target_type and target_id from the client objects manifest."
        directive["target"] = {"type": target_type, "id": target_id}
    if kind == "apply":
        if not values:
            return "Error: apply requires non-empty values."
        directive["values"] = values
        directive["confirm"] = confirm
    if kind == "highlight" and field:
        directive["field"] = field
    if kind == "navigate":
        if not to:
            return "Error: navigate requires 'to'."
        directive["to"] = to

    if kind in ("apply", "read_current_page"):
        # ROUND TRIP: pause the graph until the browser reports what happened.
        # The directive rides the interrupt value (NOT the custom stream): the
        # resume replays this handler from the top, and anything emitted before
        # ``interrupt()`` would fire twice. ``tool_call_id`` is injected and
        # stable across that replay — it is the id the client echoes back on
        # its ``client_action_result`` decision.
        logger.info(f"[CLIENT-ACTION] Awaiting result for directive: {directive}")
        result = interrupt({"client_action_request": {"id": tool_call_id, "directive": directive}})
        logger.info(f"[CLIENT-ACTION] Result received: {result}")
        return _render_client_action_result(kind, result)

    try:
        writer = get_stream_writer()
    except Exception:
        writer = None
    if writer is None:
        return "Error: client-action channel unavailable in this run."

    # Custom stream events are (event_type, event_data) tuples (see the executor's
    # consumer loop and TodoStatusMiddleware for the canonical shape).
    writer(("client_action", {"directive": directive}))
    logger.info(f"[CLIENT-ACTION] Emitted directive: {directive}")
    return "Directive sent to the client."


def create_client_action_tool() -> StructuredTool:
    """Create the per-turn client-action tool (only when a manifest is present)."""
    return StructuredTool.from_function(
        coroutine=_client_action_handler,
        name=CLIENT_ACTION_TOOL_NAME,
        description=(
            "Act on the user's application. Use kind='apply' to fill/update a registered "
            "on-screen form with values (listed in <client_objects>; the user reviews and "
            "saves — nothing is persisted directly; the result tells you which fields "
            "landed vs. were rejected), kind='highlight' to point at an object/field, "
            "kind='navigate' to open a path, kind='read_current_page' to get a sanitized "
            "snapshot of what the user currently sees. apply/highlight only target objects "
            "present in the manifest."
        ),
        args_schema=ClientActionInput,
    )
