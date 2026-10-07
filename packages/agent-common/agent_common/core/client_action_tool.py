"""Client-action tool — lets an agent act on ontology objects the host
application registered on the user's screen (Embedded Nannos).

Shared across the orchestrator and any LOCAL sub-agent (the embedded domain
agent). The tool does NOT touch any backend — the browser executes every
directive against host-registered handles. Two delivery modes:

- ``highlight`` — fire-and-forget: emitted over the LangGraph
  custom stream (same mechanism as the todo/work-plan middleware); the executor
  wraps it in a `urn:nannos:a2a:client-action:1.0` status message. No result
  comes back (the user sees the effect immediately).

- ``apply`` / ``submit`` / ``read_current_page`` / ``navigate`` / ``invoke`` — a ROUND TRIP: the tool
  ``interrupt()``s with the directive in the
  interrupt value; the executor emits it as ``input_required`` (same extension,
  ``{"request": ...}`` payload), the SDK executes it and auto-resumes with a
  ``client_action_result`` decision, which this tool returns to the model. The
  agent therefore KNOWS which fields landed and which were rejected, instead of
  assuming success. The directive rides the interrupt value ONLY — nothing is
  emitted before ``interrupt()``, so the resume replay of this handler cannot
  double-execute. ``apply`` only writes into the host's form (validated, unsaved);
  ``submit`` asks the host to SAVE the form through its own save action — the one
  kind that persists anything, so it is the one the risk gate always asks about.
  ``invoke`` runs a named action a registered object lists in the manifest (enter
  edit mode, open a create dialog, start a check the user completes). By the host
  contract an action never saves — unless the host marked it ``requiresApproval``, and
  then the user approves it with a click, like a save; the result carries the page after it settled, so
  the model sees the form the action opened. ``navigate`` is refused while the
  assistant has unsaved changes on an open form, unless ``discard_changes`` is set
  — which the model may only do after the user said to discard.

Register per-turn ONLY when the client sent a non-empty ``clientObjects``
manifest with the message.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Optional

from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.config import get_stream_writer
from langgraph.types import interrupt
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CLIENT_ACTION_TOOL_NAME = "client_action"


class ClientActionInput(BaseModel):
    """Arguments for a client-action directive."""

    kind: Literal["apply", "highlight", "invoke", "navigate", "read_current_page", "submit"] = Field(
        description=(
            "apply: write field values into a registered on-screen object (e.g. fill a form) — "
            "nothing is saved, the changed fields are marked for the user; returns which fields "
            "landed vs. were rejected; "
            "submit: save a registered form through the application's own save action (only for "
            "objects marked `submittable`; the user approves first) — use after apply when the "
            "user wants it saved; "
            "highlight: draw the user's attention to a registered object/field; "
            "navigate: ask the host app to open a path (refused while you have unsaved changes on "
            "an open form — see discard_changes); "
            "invoke: run a named action a registered object offers — only one its manifest entry lists "
            "under `actions` (e.g. `edit` to put a detail page in edit mode before apply, open a create "
            "dialog that has no route, run a watch's check). An action never saves anything — except one marked "
            "`requires approval`: it saves, the user approves it with a click like a save, so only use it "
            "when they want that done; the result "
            "carries the page after it ran, including any form it opened; "
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
    discard_changes: bool = Field(
        default=False,
        description=(
            "navigate only: throw away the unsaved changes you made on an open form. Set it ONLY after "
            "a navigate was refused for unsaved changes AND the user said to discard them."
        ),
    )
    action: str | None = Field(
        default=None, description="invoke only: the action name, exactly as the object's `actions` list it."
    )
    args: dict[str, Any] | None = Field(
        default=None, description="invoke only: arguments for the action, matching its declared params."
    )
    confirm: bool = Field(
        default=True,
        description="apply only: ask the user to confirm before writing (keep true unless trivially safe).",
    )
    tool_call_id: Annotated[str, InjectedToolCallId] = Field(default="")


def describe_directive(directive: Mapping[str, Any]) -> str:
    """The directive for a log line: its shape, never what the user typed.

    ``values`` and ``params`` are form content — a phone number, a secret's description,
    a system prompt — so only their field names are logged. Everything else (kind, target,
    action, field, route) says what happened without saying what was entered.
    """
    out: dict[str, Any] = {"kind": directive.get("kind")}
    for key in ("target", "action", "field", "to", "discard_changes"):
        if key in directive:
            out[key] = directive[key]
    for key in ("values", "params"):
        content = directive.get(key)
        if isinstance(content, Mapping):
            out[f"{key}_fields"] = sorted(str(k) for k in content)
    return str(out)


def describe_result(result: Any) -> str:
    """The result for a log line: outcome and field names, never page or form content."""
    if not isinstance(result, Mapping):
        return f"<{type(result).__name__}>"
    out: dict[str, Any] = {}
    for key in ("ok", "reason", "detail", "applied", "discarded"):
        if key in result:
            out[key] = result[key]
    rejected = result.get("rejected")
    if isinstance(rejected, list):
        out["rejected"] = [r.get("field") if isinstance(r, Mapping) else r for r in rejected]
    previous = result.get("previous")
    if isinstance(previous, Mapping):
        out["previous_fields"] = sorted(str(k) for k in previous)
    content = result.get("content")
    if isinstance(content, str):
        out["content_chars"] = len(content)
    return str(out)


def render_client_action_result(kind: str, result: Any) -> str:
    """Render the client's result payload into honest prose for the model.

    Public because the HITL middleware needs it too: when the browser executes an
    approved directive at approve-time and returns the outcome on the decision,
    the middleware answers the tool call with this same prose instead of letting
    the tool interrupt for a result it already has
    (``conditional_hitl._client_action_tool_message``).
    """
    if not isinstance(result, dict):
        return f"The client returned no usable result for '{kind}'; do not assume it succeeded."
    if not result.get("ok"):
        reason = result.get("reason") or "unknown"
        detail = result.get("detail") or result.get("message") or ""
        if reason == "not-submittable":
            if detail:
                # The form closed under the approval (reload, edit mode left): the
                # fill is gone, so "use the page's own button" points at nothing.
                return (
                    f"The action FAILED: nothing was saved. {detail} Tell the user plainly; to save, "
                    "fill the form again and ask for approval again."
                )
            return (
                "The action FAILED: this object cannot be saved by the assistant. Ask the user to "
                "save it with the page's own button."
            )
        if reason == "unknown-target":
            return (
                "The action FAILED: the target object is no longer on the user's screen "
                "(they may have navigated away). Check <current_page>/<client_objects> and adjust."
            )
        if reason == "unknown-action":
            return (
                "The action FAILED: the object offers no such action. Only actions listed under the "
                "object's `actions` in <client_objects> (or the latest page snapshot) can be invoked. " + detail
            ).strip()
        if reason == "unsaved-changes":
            return (
                "NOT NAVIGATED: an open form holds unsaved changes (your fills, or edits the user "
                "typed)"
                + (f" ({detail})" if detail else "")
                + ". Ask the user whether to save them first or discard them. Never discard on your "
                "own: call navigate again with discard_changes=true only after the user said to "
                "discard; if they want them saved, save first (kind='submit' when the form is "
                "`submittable`, otherwise they save it themselves)."
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
        previous = result.get("previous")
        if isinstance(previous, dict) and previous:
            was = "; ".join(
                f"{field} was {'empty' if value in (None, '') else json.dumps(value, ensure_ascii=False)}"
                for field, value in previous.items()
            )
            lines.append(
                f"Before this apply: {was}. To undo your fill, apply these values back (each marked field "
                "also offers the user an Undo link). A Page `refresh` does NOT discard unsaved form values."
            )
        lines.append(
            "Nothing is saved yet — the changed fields are marked for the user. If they want it "
            "saved and the object is `submittable`, use kind='submit'; otherwise they save it themselves."
        )
        return " ".join(lines)
    if kind == "submit":
        detail = result.get("detail") or result.get("message") or ""
        return ("The application saved the form." + (f" {detail}" if detail else "")).strip()
    if kind == "navigate":
        content = result.get("content")
        landed = (
            f" The user is now on this page, with these objects open (newer than <current_page> and "
            f"<client_objects> from the start of this turn — act on these): {content}"
            if isinstance(content, str) and content.strip()
            else " The application reported no details about the new page."
        )
        discarded = result.get("discarded")
        lost = (
            f" The unsaved changes on the page you left were DISCARDED ({discarded}) — tell the user they "
            "were not saved and are gone."
            if isinstance(discarded, str) and discarded.strip()
            else ""
        )
        return "Navigation done — do not navigate there again." + lost + landed
    if kind == "invoke":
        detail = result.get("detail") or result.get("message") or ""
        content = result.get("content")
        landed = (
            f" The page after the action (newer than <current_page> and <client_objects> from the "
            f"start of this turn — act on these objects): {content}"
            if isinstance(content, str) and content.strip()
            else " The application reported no details about the page afterwards."
        )
        if result.get("saved") is True:
            # An action the host marked as saving, approved by the user's click.
            return (
                "The application ran the action and it SAVED the change (the user approved it). It is "
                "done: do not run it again." + (f" {detail}" if detail else "") + landed
            )
        return "The application ran the action — nothing was saved by it." + (f" {detail}" if detail else "") + landed
    return f"The client executed '{kind}' successfully."


async def _client_action_handler(
    kind: str,
    target_type: str | None = None,
    target_id: str | None = None,
    values: dict[str, Any] | None = None,
    field: str | None = None,
    to: str | None = None,
    confirm: bool = True,
    discard_changes: bool = False,
    action: str | None = None,
    args: dict[str, Any] | None = None,
    tool_call_id: str = "",
) -> str:
    directive: dict[str, Any] = {"kind": kind}
    if kind in ("apply", "highlight", "submit", "invoke"):
        if not target_type or not target_id:
            return (
                "Error: apply/highlight/submit/invoke require target_type and target_id from the client "
                "objects manifest."
            )
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
        # Only when set: the browser refuses a navigate that would drop the
        # assistant's unsaved form changes unless this rides the directive.
        if discard_changes:
            directive["discard_changes"] = True
    if kind == "invoke":
        if not action:
            return "Error: invoke requires 'action' — a name from the object's `actions` in the manifest."
        directive["action"] = action
        if args:
            directive["args"] = args

    # ``navigate`` too: the page context and open objects the model sees were taken
    # when the turn began, so without the landed page in the result it cannot tell a
    # navigation happened — and navigates again until loop detection stops it.
    if kind in ("apply", "read_current_page", "submit", "navigate", "invoke"):
        # ROUND TRIP: pause the graph until the browser reports what happened.
        # The directive rides the interrupt value (NOT the custom stream): the
        # resume replays this handler from the top, and anything emitted before
        # ``interrupt()`` would fire twice. ``tool_call_id`` is injected and
        # stable across that replay — it is the id the client echoes back on
        # its ``client_action_result`` decision.
        logger.info(f"[CLIENT-ACTION] Awaiting result for directive: {describe_directive(directive)}")
        result = interrupt({"client_action_request": {"id": tool_call_id, "directive": directive}})
        logger.info(f"[CLIENT-ACTION] Result received: {describe_result(result)}")
        return render_client_action_result(kind, result)

    try:
        writer = get_stream_writer()
    except Exception:
        writer = None
    if writer is None:
        return "Error: client-action channel unavailable in this run."

    # Custom stream events are (event_type, event_data) tuples (see the executor's
    # consumer loop and TodoStatusMiddleware for the canonical shape).
    writer(("client_action", {"directive": directive}))
    logger.info(f"[CLIENT-ACTION] Emitted directive: {describe_directive(directive)}")
    return "Directive sent to the client."


def create_client_action_tool() -> StructuredTool:
    """Create the per-turn client-action tool (only when a manifest is present)."""
    return StructuredTool.from_function(
        coroutine=_client_action_handler,
        name=CLIENT_ACTION_TOOL_NAME,
        description=(
            "Act on the user's application. Use kind='apply' to fill/update a registered "
            "on-screen form with values (listed in <client_objects>; nothing is saved — the "
            "changed fields are marked for the user; the result tells you which fields "
            "landed vs. were rejected), kind='submit' to save a `submittable` form through the "
            "application's own save action (the user approves first — only when they want it "
            "saved), kind='invoke' to run an action an object lists under `actions` (enter edit "
            "mode, open a dialog, start a check — never saves unless marked `requires approval`), "
            "kind='highlight' to point at an "
            "object/field, kind='navigate' to open a path, kind='read_current_page' to get a "
            "sanitized snapshot of what the user currently sees. apply/highlight/submit/invoke only "
            "target objects present in the manifest."
        ),
        args_schema=ClientActionInput,
    )
