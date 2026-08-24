"""Deciding whether a watch job's condition is met.

This used to happen in agent-runner: the scheduler dispatched an agent task on every
poll, and that task called the tool, evaluated the condition and — most of the time —
concluded nothing had happened. Three things were wrong with that.

  * The scheduler could not act on the outcome, because it learned it only after
    dispatching. A voice-call watch therefore rang on every poll, before anything had
    been evaluated, which is why voice calls were restricted to task jobs.
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

from ..models.scheduled_job import ScheduledJob
from ..repositories.model_defaults_repository import ModelDefaultsRepository
from ..services.llm_gateway import gateway_chat
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
    #: How the condition was decided, recorded on the run. A rule can be re-evaluated
    #: against check_result later; a model's reasoning cannot be reconstructed at all, so
    #: it is captured here or lost.
    evaluation: dict[str, Any] | None = None


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
        """Call the job's check tool and decide whether its condition holds."""
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
        evaluation = {
            "met": met,
            "mode": "judge",
            "reasoning": reasoning,
            "extracted": _for_display(check_result),
        }
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

        if not cel.gate or not job.llm_condition:
            evaluation = {
                "met": cel.gate,
                "mode": "cel+judge" if job.llm_condition else "cel",
                "gate_met": cel.gate,
                "extracted": extracted,
            }
            logger.info("Job %d: CEL gate met=%s (judged=no)", job.id, cel.gate)
            return WatchOutcome(condition_met=cel.gate, check_result=check_result, evaluation=evaluation)

        met, reasoning = await self._judge(db, job, cel.value, check_result)
        evaluation = {
            "met": met,
            "mode": "cel+judge",
            "gate_met": True,
            "reasoning": reasoning,
            "extracted": extracted,
        }
        logger.info("Job %d: CEL gate passed, model judged met=%s", job.id, met)
        return WatchOutcome(condition_met=met, check_result=check_result, evaluation=evaluation)

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
            text = await gateway_chat(prompt, model=model, max_tokens=512)
            cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            parsed = json.loads(match.group()) if match else {}
            met = bool(parsed.get("condition_met"))
            reasoning = parsed.get("reasoning")
            reasoning = str(reasoning)[:_MAX_REASONING_CHARS] if reasoning else None
            logger.info("Job %d: model judged condition met=%s (%s)", job.id, met, (reasoning or "")[:200])
            return met, reasoning
        except Exception as exc:
            logger.exception("Job %d: condition judging failed, treating as not met", job.id)
            return False, f"The condition could not be judged: {exc}"
