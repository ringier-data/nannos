"""Client-action tool — lets an agent act on ontology objects the host
application registered on the user's screen (Embedded Nannos).

Shared across the orchestrator and any LOCAL sub-agent (the embedded domain
agent). The tool does NOT touch any backend: it emits a `client-action` directive
over the LangGraph custom stream (same mechanism as the todo/work-plan
middleware); the orchestrator executor wraps it in a
`urn:nannos:a2a:client-action:1.0` status message and the Embed SDK widget
executes it against host-registered handles (a write-scope ``apply`` goes through
the host's own form layer and the human still submits).

Register per-turn ONLY when the client sent a non-empty ``clientObjects``
manifest with the message.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CLIENT_ACTION_TOOL_NAME = "client_action"


class ClientActionInput(BaseModel):
    """Arguments for a client-action directive."""

    kind: Literal["apply", "highlight", "navigate"] = Field(
        description=(
            "apply: write field values into a registered on-screen object (e.g. fill a form); "
            "highlight: draw the user's attention to a registered object/field; "
            "navigate: ask the host app to open a path."
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


async def _client_action_handler(
    kind: str,
    target_type: str | None = None,
    target_id: str | None = None,
    values: dict[str, Any] | None = None,
    field: str | None = None,
    to: str | None = None,
    confirm: bool = True,
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
    if kind == "apply":
        return (
            "Directive sent to the client. The values will be written into the form "
            "(the user may be asked to confirm, and still reviews/saves manually). "
            "Do not assume persistence — the user submits the form themselves."
        )
    return "Directive sent to the client."


def create_client_action_tool() -> StructuredTool:
    """Create the per-turn client-action tool (only when a manifest is present)."""
    return StructuredTool.from_function(
        coroutine=_client_action_handler,
        name=CLIENT_ACTION_TOOL_NAME,
        description=(
            "Act on an object currently visible in the user's application (listed in "
            "<client_objects>). Use kind='apply' to fill/update a registered form with "
            "values (the user reviews and saves — nothing is persisted directly), "
            "kind='highlight' to point at an object/field, kind='navigate' to open a path. "
            "Only target objects present in the manifest."
        ),
        args_schema=ClientActionInput,
    )
