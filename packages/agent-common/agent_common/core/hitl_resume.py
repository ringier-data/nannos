"""Reading a resume value that may have been meant for a different question.

An ``interrupt()`` asks one question and the resume value is its answer. Two
different questions travel this channel:

- a **tool approval** — ``{"action_requests": [...]}`` out, ``{"decisions": [...]}`` back;
- an **authorization** — ``{"task_state": auth-required, ...}`` out,
  ``{"authorization": {"decision": ...}}`` back (or the user's own words, when the
  client never negotiated the in-task-auth extension and they simply kept typing).

They can be crossed. A sub-agent that gets its authorization declined re-runs the
blocked tool, its risk guard raises a *new* approval interrupt, and the answer the
orchestrator is holding — written for the auth prompt — is delivered to that one
instead. Every reader used ``interrupt(...)["decisions"]``, so the mismatch killed
the sub-agent with ``KeyError('decisions')``, surfaced to the user as
"sub-agent execution encountered a system error ('decisions')".

Readers go through :func:`decisions_from_resume` instead, which never raises:

- a real decision list passes through untouched;
- an authorization answer becomes an explicit **rejection** carrying the reason —
  someone who skipped the authorization did not approve running the tool either;
- free-form words are handed to a small fast-LLM classifier, so "no, forget it"
  rejects and "done, go ahead" approves rather than both falling through as noise;
- anything unreadable rejects, with the tool left unexecuted and the model told why.

The mirror-image translation (an approval answer arriving at an auth prompt) lives
in the orchestrator's ``_build_subagent_resume_command``, which knows both the
answer and the interrupt it is about to be keyed to.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from a2a.types import TaskState
from langsmith import traceable
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

KIND_HITL = "hitl"
KIND_AUTH = "auth"
KIND_OTHER = "other"

# The ``decision`` values the in-task-auth extension can send, normalized.
_APPROVED_WORDS = frozenset({"approved", "approve", "completed", "done"})
_DECLINED_WORDS = frozenset({"declined", "decline", "reject", "rejected", "skipped", "skip"})


def interrupt_kind(value: Any) -> str:
    """Which question an interrupt value asks: ``hitl``, ``auth`` or ``other``.

    ``other`` covers the client-action round trip and anything a future middleware
    invents — callers must leave those alone rather than guess at their shape.
    """
    if not isinstance(value, dict):
        return KIND_OTHER
    if "action_requests" in value:
        return KIND_HITL
    if value.get("task_state") == TaskState.TASK_STATE_AUTH_REQUIRED:
        return KIND_AUTH
    return KIND_OTHER


def authorization_verdict(payload: Any) -> tuple[str | None, str]:
    """``("approved"|"declined"|None, message)`` from an authorization answer.

    ``None`` means the payload is not an authorization answer at all (or carries a
    verdict this build does not know), never that the user said no.
    """
    if not isinstance(payload, dict):
        return None, ""
    decision = payload.get("authorization")
    if not isinstance(decision, dict):
        return None, ""
    verdict = str(decision.get("decision") or "").lower()
    message = str(decision.get("message") or "")
    if verdict in _APPROVED_WORDS:
        return "approved", message
    if verdict in _DECLINED_WORDS:
        return "declined", message
    return None, message


def _call_id(action_request: Any) -> Any:
    """The per-call id the HITL builders stamp into ``args._call_id``, if any."""
    if not isinstance(action_request, dict):
        return None
    return (action_request.get("args") or {}).get("_call_id")


def reject_decisions(action_requests: list[Any], message: str) -> list[dict[str, Any]]:
    """One explicit rejection per pending call, id-keyed where the id exists.

    ``message`` reaches the model verbatim as the tool result (see the base
    ``HumanInTheLoopMiddleware._process_decision``), so it must say what happened
    and what not to do next — a rejection the model cannot read the reason for is
    the thing that makes it retry.
    """
    decisions: list[dict[str, Any]] = []
    for action_request in action_requests:
        decision: dict[str, Any] = {"type": "reject", "message": message}
        call_id = _call_id(action_request)
        if call_id is not None:
            decision["id"] = call_id
        decisions.append(decision)
    return decisions


def _approve_decisions(action_requests: list[Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for action_request in action_requests:
        decision: dict[str, Any] = {"type": "approve"}
        call_id = _call_id(action_request)
        if call_id is not None:
            decision["id"] = call_id
        decisions.append(decision)
    return decisions


# Tool names that mean nothing to a user — sandbox and orchestration plumbing.
# The gateway call that needs authorization usually runs INSIDE one of them (a
# `need-credentials` from an MCP call made in the sandbox is reported against
# ``eval``), so naming them misinforms: the model reads "eval was declined" and
# tells the user the real tool does not exist. Mirrors the panel's own filter in
# ``embed-sdk/src/panel/components/auth-required-card.tsx``.
OPAQUE_TOOL_NAMES = frozenset({"eval", "task", "client_action", "python", "bash", "shell", "call_tool"})


def name_or_nothing(tool_name: str | None) -> str:
    """The tool as a user/model-facing name, or "" when it is plumbing."""
    if not tool_name or tool_name in OPAQUE_TOOL_NAMES:
        return ""
    return tool_name


# Every refusal says this: the model's failure mode is to conclude the tool is
# missing rather than blocked, and to answer the user "I don't have a GitHub tool
# in this environment" — about a tool it just tried to call. The two blocked
# states have different causes, so they get different sentences; the "never say it
# is missing" half is what both are for.
_TOOL_EXISTS = "The tool is NOT missing or unavailable — it exists and works; "
_NEVER_DENY = (
    " Never tell the user it does not exist, that you do not have it, or that it "
    "is unavailable in this environment."
)

#: Blocked for want of a credential (the authorization paths).
NOT_MISSING_CLAUSE = _TOOL_EXISTS + "it simply has no authorization." + _NEVER_DENY

#: Blocked for want of an approval (the HITL paths) — nothing to do with credentials.
NOT_APPROVED_CLAUSE = _TOOL_EXISTS + "it simply was not approved to run." + _NEVER_DENY

# ...and the failure mode right after that one: told the credential is missing and
# asked to "offer another way forward", the agent invents remedies from outside
# this product — a personal access token, a `curl` command, "run this in an
# environment where you can grant OAuth". None of them exist for a user sitting in
# a chat panel; the authorization prompt is the whole mechanism.
NO_WORKAROUND_CLAUSE = (
    "Do not propose ways around it: no personal access tokens or API keys, no "
    "curl/CLI commands, no code or configuration for the user to run, no other "
    "environment. The ONLY way to get this credential is the authorization prompt "
    "in this conversation, which the user can accept the next time they ask for "
    "this. Offer alternatives only if you can carry them out yourself with the "
    "tools you already have."
)

_SKIPPED_AUTH_MESSAGE = (
    "The user skipped the authorization this call needs, so it was NOT executed. "
    + NOT_MISSING_CLAUSE
    + " Do not retry it and do not send the authorization link again. Say plainly "
    "what you cannot do without it. "
    + NO_WORKAROUND_CLAUSE
)

_AUTHORIZED_BUT_UNAPPROVED_MESSAGE = (
    "The user answered the authorization prompt, but has not approved this call, "
    "so it was NOT executed. If it is still needed, say so and ask for it again. "
    + NOT_APPROVED_CLAUSE
)

_NO_ANSWER_MESSAGE = (
    "No usable approval was received for this call, so it was NOT executed. "
    "Do not assume it succeeded; ask the user again if it is still needed. "
    + NOT_APPROVED_CLAUSE
)


class ReplyIntent(BaseModel):
    """Structured verdict on a free-form reply to a pending approval."""

    intent: Literal["approve", "reject", "question", "unclear"] = Field(
        description=(
            "approve = the user agrees to run the pending call now; "
            "reject = the user refuses it; "
            "question = the user is ASKING about the pending call rather than "
            "answering (what is it, what does it do, is it safe); "
            "unclear = the reply says nothing about the pending call."
        )
    )


_CLASSIFIER_SYSTEM_PROMPT = (
    "An assistant paused and asked its user to approve a tool call before running it.\n"
    "Instead of clicking Approve or Reject, the user typed a message.\n"
    "Decide what that message means FOR THE PENDING CALL — nothing else.\n"
    "Rules:\n"
    "- 'approve' only when the reply agrees to this call going ahead now "
    "(e.g. 'yes', 'go ahead', 'done, try again', 'I authorized it').\n"
    "- 'reject' when the reply REFUSES it or cancels it (e.g. 'no', 'stop', "
    "'forget it', 'those permissions are too wide').\n"
    "- 'question' when the reply ASKS about the pending call instead of answering "
    "it (e.g. \'what\'s this?\', \'what does it do?\', \'why do you need it?\', "
    "\'is that safe?\'). A question is NOT a refusal, however sceptical it sounds.\n"
    "- 'unclear' when the reply is about something else entirely, or you cannot tell.\n"
    "- Never infer approval from politeness, from a question, or from silence."
)


def _classifier_prompt(reply: str, action_requests: list[Any], question: str | None) -> list[dict[str, str]]:
    lines = [question] if question else []
    if action_requests:
        lines.append("Pending call(s) awaiting approval:")
    for action_request in action_requests or []:
        name = action_request.get("name", "?") if isinstance(action_request, dict) else "?"
        args = action_request.get("args") or {} if isinstance(action_request, dict) else {}
        summary = args.get("_summary") or args.get("description") or ""
        shown = {k: v for k, v in args.items() if not k.startswith("_")}
        lines.append(f"- {name}: {summary}".rstrip(": "))
        if shown:
            lines.append(f"  arguments: {json.dumps(shown, ensure_ascii=False, default=str)[:300]}")
    lines += ["", "The user replied:", reply.strip()[:1000]]
    return [
        {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _classifier_model() -> Any:
    from agent_common.core.model_factory import create_model, get_default_fast_model, require_default_model

    model = create_model(get_default_fast_model() or require_default_model(), streaming=False)
    return model.with_structured_output(ReplyIntent)


@traceable(name="hitl-reply-classify", run_type="tool")
async def classify_reply(reply: str, action_requests: list[Any], *, question: str | None = None) -> str | None:
    """``"approve"`` / ``"reject"`` / ``None`` for words typed instead of a click.

    ``None`` on an unclear reply or any failure — the caller must treat it as "not
    approved" and never as consent.
    """
    if not isinstance(reply, str) or not reply.strip():
        return None
    from agent_common.middleware.gateway_attribution_middleware import run_config_attribution_scope

    try:
        # Side-channel call: stamp gateway cost attribution from the run config or
        # the spend lands on the orchestrator (same as tool_call_summarizer).
        with run_config_attribution_scope():
            result: ReplyIntent = await _classifier_model().ainvoke(_classifier_prompt(reply, action_requests, question))
    except Exception:
        logger.exception("[HITL] Reply classification failed; treating the reply as no approval")
        return None
    return None if result.intent == "unclear" else result.intent


@traceable(name="hitl-reply-classify", run_type="tool")
def classify_reply_sync(reply: str, action_requests: list[Any], *, question: str | None = None) -> str | None:
    """Blocking twin of :func:`classify_reply` for the sync ``after_model`` path."""
    if not isinstance(reply, str) or not reply.strip():
        return None
    from agent_common.middleware.gateway_attribution_middleware import run_config_attribution_scope

    try:
        with run_config_attribution_scope():
            result: ReplyIntent = _classifier_model().invoke(_classifier_prompt(reply, action_requests, question))
    except Exception:
        logger.exception("[HITL] Reply classification failed; treating the reply as no approval")
        return None
    return None if result.intent == "unclear" else result.intent


def structural_decisions(resume: Any, action_requests: list[Any]) -> list[dict[str, Any]] | None:
    """Decisions readable without asking a model, or ``None`` if words are all we have."""
    if isinstance(resume, dict):
        decisions = resume.get("decisions")
        if isinstance(decisions, list):
            return decisions
        verdict, message = authorization_verdict(resume)
        if verdict == "declined":
            reason = f" They said: {message}" if message.strip() else ""
            logger.info("[HITL] Authorization was declined; rejecting %d pending call(s)", len(action_requests))
            return reject_decisions(action_requests, _SKIPPED_AUTH_MESSAGE + reason)
        if verdict == "approved":
            # Answering the auth prompt is not approving this call: the approval
            # question was never put to the user. Reject, and let the model ask.
            logger.info("[HITL] Authorization answer arrived at an approval prompt; rejecting")
            return reject_decisions(action_requests, _AUTHORIZED_BUT_UNAPPROVED_MESSAGE)
        return None if isinstance(resume.get("authorization"), dict) else _no_answer(resume, action_requests)
    if isinstance(resume, str):
        return None
    return _no_answer(resume, action_requests)


def _no_answer(resume: Any, action_requests: list[Any]) -> list[dict[str, Any]]:
    logger.warning(
        "[HITL] Resume value %r carries no usable decision; rejecting %d pending call(s)",
        resume,
        len(action_requests),
    )
    return reject_decisions(action_requests, _NO_ANSWER_MESSAGE)


def _from_intent(intent: str | None, reply: str, action_requests: list[Any]) -> list[dict[str, Any]]:
    """Turn a classified reply into decisions.

    Three outcomes, not two. A refusal and a *non-answer* both leave the call
    unexecuted, but they are different facts and the model must act on them
    differently: a refusal is a decision to respect, while an unrelated message
    (or a classifier that could not run) is simply no answer yet — treating it as
    a refusal consumed the approval AND forbade asking again, so an off-topic
    question in the composer silently killed the task.
    """
    said = f" They said: {reply.strip()}" if isinstance(reply, str) and reply.strip() else ""
    if intent == "approve":
        logger.info("[HITL] Classified the user's reply as an approval")
        return _approve_decisions(action_requests)
    if intent == "reject":
        logger.info("[HITL] Classified the user's reply as a refusal")
        return reject_decisions(
            action_requests,
            f"The user REFUSED this call, so it was NOT executed.{said} "
            f"Do not retry it. Take their answer into account and continue from there. "
            f"{NOT_APPROVED_CLAUSE}",
        )
    if intent == "question":
        logger.info("[HITL] The user asked a question about the pending call")
        return reject_decisions(
            action_requests,
            f"The user ASKED A QUESTION about this call instead of answering the "
            f"approval request, so it was NOT executed — this is NOT a refusal."
            f"{said} ANSWER THEIR QUESTION first: say plainly what this call would "
            "do, on what data, and why you need it. Then ask them to approve it "
            f"again. {NOT_APPROVED_CLAUSE}",
        )
    logger.info("[HITL] The user's reply was not an answer to the approval request")
    return reject_decisions(
        action_requests,
        f"This call was NOT executed: the user's reply was not an answer to the "
        f"approval request.{said} This is not a refusal — they simply have not "
        "answered it. Respond to what they actually said, and ask for the call "
        f"again if you still need it. Never assume it ran. {NOT_APPROVED_CLAUSE}",
    )


async def decisions_from_resume(resume: Any, action_requests: list[Any]) -> list[dict[str, Any]]:
    """A decision per pending call, whatever shape the resume value arrived in."""
    structural = structural_decisions(resume, action_requests)
    if structural is not None:
        return structural
    reply = resume if isinstance(resume, str) else str((resume or {}).get("authorization", {}).get("message", ""))
    return _from_intent(await classify_reply(reply, action_requests), reply, action_requests)


def decisions_from_resume_sync(resume: Any, action_requests: list[Any]) -> list[dict[str, Any]]:
    """Blocking twin of :func:`decisions_from_resume` for the sync HITL path."""
    structural = structural_decisions(resume, action_requests)
    if structural is not None:
        return structural
    reply = resume if isinstance(resume, str) else str((resume or {}).get("authorization", {}).get("message", ""))
    return _from_intent(classify_reply_sync(reply, action_requests), reply, action_requests)


def authorization_from_decisions(decisions: Any) -> dict[str, Any] | None:
    """An authorization answer equivalent to a tool-approval answer, if there is one.

    Only a rejection carries over: "do not run this call" and "do not authorize"
    are the same "no", and the auth middleware can act on it instead of falling
    through with "(no reply)" and telling the model to try again. An *approval*
    does not carry over — the user approved a call, they were never asked about
    the authorization — so this returns ``None`` and the caller must not guess.
    """
    if not isinstance(decisions, list) or not decisions:
        return None
    types = {d.get("type") for d in decisions if isinstance(d, dict)}
    if types == {"reject"}:
        message = next((d.get("message", "") for d in decisions if isinstance(d, dict) and d.get("message")), "")
        return {"decision": "declined", "message": message}
    return None


# ---------------------------------------------------------------------------
# Reading LangGraph's resume state without disturbing it.
#
# ``scratchpad.resume`` is a REPLAY LOG, not a queue of unanswered questions: it
# holds the values every ``interrupt()`` in this task has already returned, plus —
# when the caller resumed with an id-keyed map — the new answer appended at the
# end. ``interrupt()`` itself indexes into it by interrupt number and only falls
# back to the null-resume queue past the end (langgraph ``types.interrupt``).
#
# So its truthiness answers "has this task ever answered an interrupt", which is
# not the question anyone here wants to ask. Believing otherwise makes the second
# HITL card of a multi-round PTC ``eval`` look like a replay (it is not: its
# ``interrupt()`` is about to RAISE) and skip its plain-language summaries.
# ---------------------------------------------------------------------------


def _scratchpad() -> Any:
    from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD
    from langgraph.config import get_config

    return get_config()["configurable"][CONFIG_KEY_SCRATCHPAD]


_COUNT_REPR_RE = re.compile(r"^count\((\d+)\)$")


def _next_interrupt_index(scratchpad: Any) -> int:
    """Which interrupt the next ``interrupt()`` will be, WITHOUT advancing it.

    The counter is an ``itertools.count.__next__``; calling it would consume the
    index the real ``interrupt()`` needs, so the position is read off the count's
    ``repr`` ("count(3)") — the only non-mutating view CPython 3.14 leaves open
    (``count`` is no longer copyable or picklable). A count that has never been
    called has no bound object yet, which is index 0; anything unrecognized raises
    and the callers fall back to their safe answer.
    """
    counter = getattr(scratchpad, "interrupt_counter", None)
    inner = getattr(counter, "_counter", None)
    count_obj = getattr(inner, "__self__", None)
    if count_obj is None:  # never called yet (or a shape we do not recognize)
        return 0
    match = _COUNT_REPR_RE.match(repr(count_obj))
    if match is None:
        msg = f"unrecognized interrupt counter: {count_obj!r}"
        raise TypeError(msg)
    return int(match.group(1))


def resume_will_return() -> bool:
    """True when the NEXT ``interrupt()`` returns a value instead of raising.

    Callers use it to skip work whose only purpose is to decorate a question that
    is not going to be asked. Any failure answers False — asking again is wasteful,
    never wrong.
    """
    try:
        scratchpad = _scratchpad()
        if _next_interrupt_index(scratchpad) < len(scratchpad.resume or []):
            return True
        return scratchpad.get_null_resume(False) is not None
    except Exception:  # noqa: BLE001 — best-effort probe over private internals
        logger.debug("[HITL] Resume probe unavailable", exc_info=True)
        return False


def pending_authorization_answer() -> dict[str, Any] | None:
    """The latest authorization answer available to this task, if any.

    Scans every resume value the task holds, newest first, for the self-identifying
    ``{"authorization": {...}}`` shape. Position-independent by design: the answer
    to an auth prompt is rarely the value the *next* ``interrupt()`` will consume
    (a tool-approval replay usually comes first), and a caller that needs to honor
    a refusal BEFORE running the tool cannot wait for the index to come round.

    Reads only; nothing is consumed, and every ``interrupt()`` still returns
    exactly what it would have returned.
    """
    try:
        scratchpad = _scratchpad()
        values = list(scratchpad.resume or [])
        null_resume = scratchpad.get_null_resume(False)
        if null_resume is not None:
            values.append(null_resume)
    except Exception:  # noqa: BLE001 — best-effort probe over private internals
        logger.debug("[HITL] Resume probe unavailable", exc_info=True)
        return None
    for value in reversed(values):
        if isinstance(value, dict) and isinstance(value.get("authorization"), dict):
            return value
    return None
