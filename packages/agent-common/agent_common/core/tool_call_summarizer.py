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

Every caller runs this immediately BEFORE ``interrupt()``, and LangGraph
re-executes a resumed task from the top — so a naive call is paid twice: once
to draw the card, and once on the resume, for a sentence that can no longer be
shown to anyone. ``attach_summaries`` therefore skips itself while a resume is
pending (see ``_resume_pending``). Measured on an embedded ``client_action``
apply: the wasted call sat in front of the form write for 15.6 s.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from langsmith import traceable
from pydantic import BaseModel, Field

from agent_common.core.client_action_tool import CLIENT_ACTION_TOOL_NAME

logger = logging.getLogger(__name__)

# Keys stamped by the middleware for the client; meaningless to the LLM.
_INTERNAL_ARG_KEYS = frozenset({"_call_id", "_risk_metadata", "_summary", "reason"})

# Tools whose own arguments already say what they do, in a fixed vocabulary the
# client can render itself — and better, because it renders in the user's
# language without a translation round trip. ``client_action`` is the case:
# ``kind`` is one of four literals ("apply" = fills out a form on the page), and
# the embed SDK maps each to a localized sentence. Paying a fast-LLM call to
# rewrite a closed enum bought nothing and cost 3.35 s in front of the card.
_SELF_EVIDENT_TOOLS = frozenset({CLIENT_ACTION_TOOL_NAME})

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

        # Side-channel call: it bypasses the agent middleware stack, so stamp
        # gateway cost attribution (sub_agent, conversation, …) from the run
        # config ourselves or the spend lands on the orchestrator.
        from agent_common.middleware.gateway_attribution_middleware import run_config_attribution_scope

        with run_config_attribution_scope():
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


def _resume_pending() -> bool:
    """True when the next ``interrupt()`` in this task will RETURN, not pause.

    A resumed LangGraph task replays from the top of the node, so everything in
    front of its ``interrupt()`` runs a second time. On that pass the interrupt
    value is discarded (``interrupt`` hands back the stored decision instead of
    raising), which makes any summary computed for it pure waste — and it sits
    on the critical path between the user's click and the tool actually running.

    Indexed, not truthy: ``scratchpad.resume`` is the replay LOG of everything the
    task has already answered, so testing it for emptiness reported "resuming" for
    every interrupt after the first. A multi-round PTC ``eval`` raises a fresh card
    each round, and from the second round on it silently lost its summaries — the
    cards whose raw args are least self-explanatory. See
    ``agent_common.core.hitl_resume.resume_will_return``.
    """
    from agent_common.core.hitl_resume import resume_will_return

    return resume_will_return()


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
    if _resume_pending():
        # Replay of a resumed task: the card was drawn (and answered) on the
        # first pass. Summarizing again would only delay the resumed tool.
        logger.debug("Skipping tool-call summaries: interrupt is resuming")
        return

    # Only the requests that actually need prose reach the model. A batch that is
    # entirely self-evident (the embedded case: one `client_action`) skips the
    # call altogether; a mixed batch still summarizes the rest, and the
    # self-evident ones keep their args untouched for the client to render.
    needs_prose = [r for r in action_requests if r["name"] not in _SELF_EVIDENT_TOOLS]
    if not needs_prose:
        logger.debug("Skipping tool-call summaries: every request is self-evident")
        return

    calls: list[tuple[str, dict[str, Any], str]] = []
    for action_request in needs_prose:
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

    for action_request, summary in zip(needs_prose, summaries):
        if summary:
            action_request["args"] = {**action_request.get("args", {}), "_summary": summary}
