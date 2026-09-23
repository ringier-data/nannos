"""Deciding whether a watch job's condition is met.

This used to happen in agent-runner: the scheduler dispatched an agent task on every
poll, and that task called the tool, evaluated the condition and — most of the time —
concluded nothing had happened. Three things were wrong with that.

  * The scheduler could not act on the outcome, because it learned it only after
    dispatching. A voice-call watch therefore rang on every poll, before anything had
    been evaluated, which is why voice calls were restricted to task jobs. That
    restriction is gone: a watch reaches dispatch only once its condition is met, so a
    watch can ring a phone and it rings because something happened.
  * Every quiet poll cost a full agent run. An hourly watch that fires monthly spent
    ~700 agent invocations a month deciding to do nothing.
  * "Would this condition trigger?" — which the console answers while a job is being
    written — was computed by a different service than the one that would run it.

The decision belongs with the scheduler: it owns *when* and *whether*, agent-runner owns
doing agent work. So the check runs here, and a poll that does not trigger dispatches
nothing at all.

Delivery is the one thing that did not move, on purpose. A triggered watch — even one
that only notifies and runs no agent — is still dispatched to agent-runner, because the
notification is delivered by the a2a-sdk's push sender as an A2A Task envelope that
three client services normalise (client-slack has a dedicated a2aPushPayload.ts for it).
Sending that from here would mean re-implementing a contract three receivers depend on,
to save one hop on the rare poll that actually triggers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from .cel_condition import CelEvaluationError, CelSyntaxError, evaluate_arg_exprs, evaluate_cel

from ..models.scheduled_job import ConditionEvaluation, ScheduledJob
from ..repositories.model_defaults_repository import ModelDefaultsRepository
from ..services.llm_gateway import gateway_chat_json
from ..services.mcp_tool_client import GatewayError, call_tool, token_for
from ..utils.timezones import resolve_timezone

logger = logging.getLogger(__name__)

#: The model's account of its decision is shown to a person, not parsed, so it only needs
#: to be long enough to be an explanation.
_MAX_REASONING_CHARS = 2000

#: The extracted value is recorded for display beside the reasoning. The full response is
#: already on the job, so this only has to be readable.
_MAX_EXTRACTED_CHARS = 4000


def _for_display(extracted: Any) -> Any:
    """Shrink an extracted value to something worth storing on a run.

    A path matching a large subtree would otherwise put a copy of the response on every
    run, and the response itself is already kept on the job.
    """
    if extracted is None or isinstance(extracted, (bool, int, float)):
        return extracted
    if isinstance(extracted, str):
        return extracted[:_MAX_EXTRACTED_CHARS]
    serialised = json.dumps(extracted, default=str)
    if len(serialised) <= _MAX_EXTRACTED_CHARS:
        return extracted
    return serialised[:_MAX_EXTRACTED_CHARS] + "… (truncated)"


def _job_now(job: ScheduledJob) -> datetime:
    """The current time in the job's timezone.

    Time-relative conditions ("starts within the hour") are decided against this —
    both by CEL, where it is the `now` variable, and by the judge, whose prompt states
    it. A job with a broken stored timezone still gets a correct instant (UTC), just
    not local wall-clock: the condition must not fail over a display concern.
    """
    utc_now = datetime.now(timezone.utc)
    try:
        return utc_now.astimezone(resolve_timezone(job.timezone))
    except ValueError:
        return utc_now


@dataclass
class WatchOutcome:
    """What one evaluation concluded."""

    condition_met: bool
    #: The tool response, kept whether or not the condition held: it is shown on the job
    #: and seeds the next evaluation.
    check_result: dict[str, Any] | None = None
    #: Set when the check could not be performed at all — an unreachable gateway, a tool
    #: that no longer exists. Distinct from "condition not met", which is a normal outcome.
    error: str | None = None
    #: How the condition was decided, recorded on the run. Typed, because this is what
    #: ScheduledJobRun validates on the way back out of the database: as a bare dict, a
    #: misspelled key serialised happily and then read back as a default — a run whose
    #: expression matched would explain itself as one that matched nothing, with nothing
    #: raising anywhere. A rule can be re-evaluated
    #: against check_result later; a model's reasoning cannot be reconstructed at all, so
    #: it is captured here or lost.
    evaluation: ConditionEvaluation | None = None
    #: The items the condition's expression matched — a list or a map, untruncated. A CEL
    #: condition is both the gate and the filter — the author wrote it to pick out what
    #: matters from the response — so this is what a triggered watch hands on (to the
    #: agent, to the notification writer) in place of the whole response. None when there
    #: is nothing narrower than ``check_result`` worth acting on: a boolean gate, a
    #: judge-only watch, and a scalar extraction (a bare ``"FAILED"`` or a count carries
    #: none of the ids or names a reader needs, so the whole response goes instead). With
    #: a judge stacked on the expression the judge is shown both, but what the run hands
    #: on is still what the expression matched: the judge decides, it does not widen.
    #: Kept apart from ``evaluation.extracted``, which is the same value shrunk for
    #: storage on the run.
    evidence: Any = None
    #: Set when the check tool answered ``need-credentials``: the owner has not authorized
    #: the tool, and nothing in the runtime can. Neither an error nor a verdict — the
    #: engine parks the run on it (ADR-0009), which is why it is not folded into ``error``:
    #: an error is evidence about the job and counts toward ``max_failures``; a missing
    #: credential is evidence about what the owner has authorized, fixable in one click.
    #: The value is the ask in the shape the console and the chat clients already render
    #: (``AuthPayload.client_payload()``), built here because console-backend does not
    #: depend on agent-common.
    auth_ask: dict[str, Any] | None = None


#: The gateway's structured refusal when a tool needs the caller's own credential. The
#: same payload ``AuthErrorDetectionMiddleware`` detects on the agent path; only the
#: field check is mirrored here, since the tool client has already parsed the JSON.
_NEED_CREDENTIALS = "need-credentials"
_NEED_CREDENTIALS_FIELD_RE = re.compile(r'"errorCode"\s*:\s*"need-credentials"')
_AUTHORIZE_URL_RE = re.compile(r'"authorizeUrl"\s*:\s*"([^"]+)"')
_AUTH_MESSAGE_RE = re.compile(r'"message"\s*:\s*"([^"]+)"')


def need_credentials_ask(tool_name: str, check_result: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ask a ``need-credentials`` tool result amounts to, or None for any other result.

    Matched on the actual ``errorCode`` field, never on the words appearing somewhere in
    a payload: a tool's business data may legitimately mention credentials.

    Shaped as the in-task-auth ``client_payload`` so ``parked_payload`` on the run reads
    the same whether the ask came from an agent's interrupt or from this check: the
    console's ``readParkedAsk`` and the chat clients' cards look for
    ``auth_requirement.auth_methods[].auth_url`` and ``auth_requirement.resource``. The
    resource is the tool, which is the one thing that tells the owner WHAT they are
    authorizing. The service is left empty exactly as the agent path leaves it (the
    gateway's payload names no service), so a card built from either ask names the tool.
    """
    if not isinstance(check_result, dict):
        return None
    if check_result.get("errorCode") == _NEED_CREDENTIALS:
        authorize_url = check_result.get("authorizeUrl")
        message = check_result.get("message")
    else:
        # The refusal wrapped in prose, or one of several content blocks: the tool client
        # folds anything that is not a single JSON object under ``output``, so the field
        # is looked for in that text — the field, still, not the words.
        output = check_result.get("output")
        if not isinstance(output, str) or not _NEED_CREDENTIALS_FIELD_RE.search(output):
            return None
        url_match = _AUTHORIZE_URL_RE.search(output)
        msg_match = _AUTH_MESSAGE_RE.search(output)
        authorize_url = url_match.group(1) if url_match else None
        message = msg_match.group(1) if msg_match else None
    return {
        "requires_auth": True,
        "auth_requirement": {
            "service": "",
            "resource": tool_name,
            "auth_methods": [
                {
                    "method": "oauth2",
                    "description": message if isinstance(message, str) and message else f"Authorization required for {tool_name}",
                    **({"auth_url": authorize_url} if isinstance(authorize_url, str) and authorize_url else {}),
                }
            ],
            "required_scopes": [],
            "token_type": "Bearer",
        },
    }


class WatchEvaluator:
    """Runs a watch job's check and evaluates its condition."""

    @staticmethod
    def can_evaluate(job: ScheduledJob) -> bool:
        """Whether this job has a check to perform.

        Every watch with a tool qualifies, including the `console_*` tools this backend
        serves itself — those are reached over loopback with a token minted for our own
        audience, so they no longer need agent-runner's MCP client.
        """
        return job.job_type.value == "watch" and bool(job.check_tool)

    async def evaluate(
        self,
        db: AsyncSession,
        job: ScheduledJob,
        access_token: str,
    ) -> WatchOutcome:
        """Call the job's check tool and decide whether its condition holds.

        The judge's gateway spend is attributed by the `attribution_scope` the engine
        opens around the dispatch, so nothing about billing is threaded through here.
        """
        tool_name = job.check_tool or ""
        try:
            token = await token_for(tool_name, access_token)
        except ValueError as exc:
            return WatchOutcome(condition_met=False, error=str(exc))

        # Dynamic arguments resolve before the call: a rolling window ("the last 7
        # days") is an expression over `now`, evaluated fresh on each poll, merged
        # over the static arguments. Failing to resolve fails the run — calling the
        # tool with half-built arguments would produce a payload the condition then
        # judges as if it were real.
        check_args = dict(job.check_args or {})
        if job.check_args_exprs:
            try:
                check_args |= await evaluate_arg_exprs(
                    job.check_args_exprs, now=_job_now(job), prev=job.last_check_result
                )
            except (CelSyntaxError, CelEvaluationError) as exc:
                return WatchOutcome(
                    condition_met=False,
                    error=f"Dynamic arguments failed: {exc}",
                )

        try:
            call = await call_tool(token, tool_name, check_args)
        except GatewayError as exc:
            # A failed check is a failed run, not a quiet one: a watch that cannot see
            # its subject must not look like a watch whose condition is false.
            return WatchOutcome(condition_met=False, error=str(exc))

        check_result = call.result
        ask = need_credentials_ask(tool_name, check_result)
        if ask is not None:
            # Not a failure of the check: the owner has not authorized this tool. The
            # engine parks the run on the ask rather than recording a failed poll — and
            # the raw payload stays off the run, where its authorize URL would otherwise
            # sit in an error banner (the same reason the chat cards render the ask
            # instead of the tool's words).
            return WatchOutcome(condition_met=False, auth_ask=ask)

        if call.is_error:
            # The tool ran and reported its own failure. The payload usually says why, so
            # keep it on the run for whoever debugs the job.
            return WatchOutcome(
                condition_met=False,
                check_result=check_result,
                error=f"'{job.check_tool}' reported an error: {json.dumps(check_result, default=str)[:500]}",
            )

        if job.cel_expr:
            return await self._evaluate_cel(db, job, check_result)

        if not job.llm_condition:
            # Validation refuses to store a watch like this, so reaching here means the
            # row predates the CEL migration and was somehow missed by it. Fail loudly:
            # a conditionless watch polling forever while looking configured is exactly
            # the silent failure the validators exist to prevent.
            return WatchOutcome(
                condition_met=False,
                check_result=check_result,
                error="This watch has no condition (neither cel_expr nor llm_condition) and can never fire.",
            )

        met, reasoning = await self._judge(db, job, check_result, check_result)
        evaluation = ConditionEvaluation(
            met=met,
            mode="judge",
            reasoning=reasoning,
            extracted=_for_display(check_result),
        )
        logger.info("Job %d: watch condition met=%s", job.id, met)
        return WatchOutcome(condition_met=met, check_result=check_result, evaluation=evaluation)

    async def _evaluate_cel(
        self,
        db: AsyncSession,
        job: ScheduledJob,
        check_result: dict[str, Any],
    ) -> WatchOutcome:
        """Decide a CEL-conditioned watch: the expression extracts and gates in one.

        With an llm_condition on top, the gate is a necessary condition and the judge
        the sufficient one: a false gate ends the evaluation with no model call at all
        (which is the point — the mechanical part of a condition should not cost an LLM
        invocation 96 times a day), and a passed gate hands the judge the evidence the
        expression returned rather than the raw response.

        Fails closed on any evaluation problem: an expression that cannot see its
        subject (a field the payload lost, a timeout) is an error that counts toward
        max_failures, never a quiet "not met".
        """
        assert job.cel_expr is not None
        try:
            cel = await evaluate_cel(
                job.cel_expr,
                result=check_result,
                now=_job_now(job),
                prev=job.last_check_result,
            )
        except (CelSyntaxError, CelEvaluationError) as exc:
            return WatchOutcome(
                condition_met=False,
                check_result=check_result,
                error=f"CEL condition {job.cel_expr!r} failed: {exc}",
            )

        extracted = _for_display(cel.value)
        evidence = cel.value if isinstance(cel.value, (list, dict)) else None

        if not cel.gate or not job.llm_condition:
            evaluation = ConditionEvaluation(
                met=cel.gate,
                mode="cel+judge" if job.llm_condition else "cel",
                gate_met=cel.gate,
                extracted=extracted,
            )
            logger.info("Job %d: CEL gate met=%s (judged=no)", job.id, cel.gate)
            return WatchOutcome(
                condition_met=cel.gate, check_result=check_result, evaluation=evaluation, evidence=evidence
            )

        met, reasoning = await self._judge(db, job, cel.value, check_result)
        evaluation = ConditionEvaluation(
            met=met,
            mode="cel+judge",
            gate_met=True,
            reasoning=reasoning,
            extracted=extracted,
        )
        logger.info("Job %d: CEL gate passed, model judged met=%s", job.id, met)
        return WatchOutcome(condition_met=met, check_result=check_result, evaluation=evaluation, evidence=evidence)

    async def _judge(
        self,
        db: AsyncSession,
        job: ScheduledJob,
        extracted: Any,
        check_result: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Ask a small model whether a natural-language condition holds.

        Returns the verdict and the model's account of it. The account is the only
        explanation that will ever exist for this run — unlike a rule, the decision cannot
        be reproduced from the stored response — so it is returned rather than logged.

        Fails closed: a model that cannot be reached must not trigger a job, because a
        false trigger sends a notification (or runs an agent) for something that did not
        happen, and repeats every poll until someone notices.
        """
        defaults = await ModelDefaultsRepository().get_all(db)
        model = defaults.get("chat:low") or defaults.get("chat")
        if not model:
            logger.error("Job %d: no chat model configured, cannot judge condition", job.id)
            return False, "No chat model is configured, so the condition could not be judged."

        # The judge is told the time. Without it, a time-relative condition ("starts
        # within the hour") is decided against a clock the model infers from payload
        # timestamps — a run that can never be right twice a day.
        now = _job_now(job)
        # A CEL gate hands the judge what it extracted; a judge-only watch reads the
        # whole response, in which case a separate "extracted" section would just be
        # the response twice.
        extracted_section = (
            f"Value the condition's expression extracted:\n{json.dumps(extracted, indent=2, default=str)[:4000]}\n\n"
            if extracted is not check_result
            else ""
        )
        prompt = (
            "You are evaluating whether a condition is met, for a scheduling system. "
            "Answer only with a JSON object: {\"condition_met\": true|false, \"reasoning\": \"…\"}.\n\n"
            f"The current time is {now.isoformat()}"
            + (f" ({job.timezone})" if job.timezone else "")
            + ".\n\n"
            f"Condition:\n{job.llm_condition}\n\n"
            + extracted_section
            + f"Full tool response:\n{json.dumps(check_result, indent=2, default=str)[:8000]}"
        )
        try:
            parsed = await gateway_chat_json(prompt, model=model, max_tokens=512)
            met = bool(parsed.get("condition_met"))
            reasoning = parsed.get("reasoning")
            reasoning = str(reasoning)[:_MAX_REASONING_CHARS] if reasoning else None
            logger.info("Job %d: model judged condition met=%s (%s)", job.id, met, (reasoning or "")[:200])
            return met, reasoning
        except Exception as exc:
            logger.exception("Job %d: condition judging failed, treating as not met", job.id)
            return False, f"The condition could not be judged: {exc}"
