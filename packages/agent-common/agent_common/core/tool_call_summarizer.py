"""Plain-language summaries for HITL action requests.

When the HITL middleware is about to interrupt, the raw tool call
(``ls`` + ``path: /group_memories/``) is opaque to non-technical users.
This module makes ONE batched fast-LLM call that turns every pending
action request into a one-sentence summary in the user's language
(e.g. "The assistant wants to list the files in the shared memory folder").

The summary is display-only: the middleware stamps it into the action
request's args as ``_summary`` (next to ``_call_id``/``_risk_metadata``),
and the embed-sdk approval card renders it. Failures never block the
interrupt — callers get ``None`` and show the raw args as before.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from langsmith import traceable
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Keys stamped by the middleware for the client; meaningless to the LLM.
_INTERNAL_ARG_KEYS = frozenset({"_call_id", "_risk_metadata", "_summary", "reason"})

# Keep the prompt bounded even if a tool call carries a huge payload.
_MAX_VALUE_CHARS = 300

_SYSTEM_PROMPT = (
    "You explain pending tool calls to a non-technical user (e.g. a journalist) "
    "who must approve or reject them.\n"
    "For EACH tool call, write ONE short plain-language sentence saying what the "
    "assistant is about to do, mentioning the concrete target (file, folder, "
    "record, recipient) in human terms.\n"
    "Rules:\n"
    "- Write in the language given by the ISO code in the request.\n"
    "- No jargon: never mention tool names, parameters, JSON, or risk scores.\n"
    "- Be factual; do not speculate about intent beyond the call itself.\n"
    "- Return exactly one summary per tool call, in the same order."
)


class ToolCallSummaries(BaseModel):
    """Structured LLM output: one summary per action request, in order."""

    summaries: list[str] = Field(
        description="One plain-language sentence per tool call, same order as the input list.",
    )


def _compact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Drop internal keys and truncate long values before prompting."""
    compact: dict[str, Any] = {}
    for key, value in args.items():
        if key in _INTERNAL_ARG_KEYS:
            continue
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if isinstance(text, str) and len(text) > _MAX_VALUE_CHARS:
            text = text[:_MAX_VALUE_CHARS] + "…"
        compact[key] = text
    return compact


@traceable(name="tool-call-summarize", run_type="tool")
async def summarize_action_requests(
    calls: list[tuple[str, dict[str, Any], str]],
    *,
    language: str = "en",
) -> list[str] | None:
    """Summarize pending tool calls in plain language.

    Args:
        calls: One tuple per action request: (tool_name, args, tool_description).
        language: ISO 639-1 code of the user's preferred language.

    Returns:
        One summary per call (same order), or None if summarization failed —
        callers must treat None as "show raw args" and never block on it.
    """
    if not calls:
        return []

    from agent_common.core.model_factory import create_model, get_default_fast_model, require_default_model

    try:
        model = create_model(get_default_fast_model() or require_default_model(), streaming=False)
        structured_model = model.with_structured_output(ToolCallSummaries)

        lines: list[str] = [f"User language: {language}", ""]
        for idx, (tool_name, args, description) in enumerate(calls, start=1):
            lines.append(f"Tool call {idx}:")
            lines.append(f"  name: {tool_name}")
            if description:
                lines.append(f"  purpose: {description}")
            lines.append(f"  arguments: {json.dumps(_compact_args(args), ensure_ascii=False, default=str)}")
            lines.append("")
        lines.append(f"Summarize these {len(calls)} tool call(s).")

        result: ToolCallSummaries = await structured_model.ainvoke(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ]
        )
    except Exception:
        logger.exception("Tool call summarization failed; falling back to raw args")
        return None

    if len(result.summaries) != len(calls):
        logger.warning(
            "Tool call summarizer returned %d summaries for %d calls; discarding",
            len(result.summaries),
            len(calls),
        )
        return None

    return [summary.strip() for summary in result.summaries]


async def attach_summaries(
    action_requests: list[Any],
    *,
    language: str = "en",
    describe: Callable[[str], str] | None = None,
) -> None:
    """Stamp a plain-language ``_summary`` into each action request's args.

    Shared by BOTH HITL interrupt paths — the normal tool-call path
    (``ConditionalHumanInTheLoopMiddleware.aafter_model``) and the PTC code
    interpreter path (``_build_ptc_hitl_request`` in ``graph_utils``) — so
    approvals render identically wherever they originate. Best-effort: any
    failure leaves the action requests untouched.

    Args:
        action_requests: The ``ActionRequest`` dicts about to be interrupted.
        language: ISO 639-1 code of the user's preferred language.
        describe: Optional tool-name -> description lookup for grounding.
    """
    calls: list[tuple[str, dict[str, Any], str]] = []
    for action_request in action_requests:
        tool_name = action_request["name"]
        description = ""
        if describe is not None:
            try:
                description = describe(tool_name) or ""
            except Exception:  # noqa: BLE001 — description is optional grounding only
                description = ""
        calls.append((tool_name, action_request.get("args", {}), description))

    summaries = await summarize_action_requests(calls, language=language)
    if summaries is None:
        return

    for action_request, summary in zip(action_requests, summaries):
        if summary:
            action_request["args"] = {**action_request.get("args", {}), "_summary": summary}
