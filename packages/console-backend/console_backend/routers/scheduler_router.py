"""Scheduler API router — exposes schedule management as MCP tools.

All endpoints are tagged "MCP" so FastApiMCP auto-exposes them as MCP tools,
allowing the orchestrator to create and manage scheduled jobs conversationally.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError


from ..db.session import DbSession
from ..dependencies import require_auth, require_auth_or_bearer_token
from ..models.scheduled_job import (
    GenerateConditionRequest,
    GenerateConditionResponse,
    GenerateJobDraftRequest,
    JobType,
    ValidateArgsExprRequest,
    ValidateArgsExprResponse,
    RunNowResponse,
    ScheduledJob,
    ScheduledJobCreate,
    ScheduledJobDraft,
    ScheduledJobRun,
    ScheduledJobUpdate,
    ScheduleKind,
    ValidateConditionRequest,
    ValidateConditionResponse,
)
from ..models.sub_agent import SubAgent
from ..models.user import User
from ..repositories.model_defaults_repository import ModelDefaultsRepository
from ..services.cel_condition import (
    CEL_SYNTAX_HINT,
    CelEvaluationError,
    CelSyntaxError,
    evaluate_cel,
    evaluate_arg_exprs,
    validate_cel_expression,
)
from ..services.llm_gateway import gateway_chat_json
from ..services.scheduler_engine import SchedulerEngine
from ..services.scheduler_service import _UNSET, SchedulerService
from ..utils.timezones import resolve_timezone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scheduler")


#: Why check_args must never carry a date: they are stored once and sent unchanged on
#: every run, so a timestamp that is correct today selects the wrong window on every
#: run after it — the job looks configured and silently watches the past. Stated as
#: its own block because models reach for concrete dates whenever a tool schema shows
#: a time parameter, and the failure only surfaces once the job is live.
_ARGS_RULES = (
    "`check_args` are STATIC: stored once and sent unchanged on every run. NEVER put "
    "an absolute date, timestamp or fixed time window in them — a date that is correct "
    "today is wrong on every later run. An argument that must move with time goes in "
    "`check_args_exprs`: a JSON object mapping the argument name to a CEL expression "
    "over `now` and `prev`, evaluated fresh on every run and merged over check_args. "
    "Example, a rolling 7-day report window:\n"
    '  "check_args_exprs": {"start_date": "strftime(now - duration(\'168h\'), '
    "'%Y-%m-%d')\", \"end_date\": \"strftime(now, '%Y-%m-%d')\"}\n"
    "(strftime formats a timestamp; string(t) renders ISO 8601; duration() only takes "
    "h/m/s units, so 7 days is '168h'.) The keys MUST be the tool's exact argument "
    "names from its input_schema, and the date format must match what the schema or "
    "its descriptions ask for.\n"
    "When the tool REQUIRES a date/time argument, you MUST provide it through "
    "check_args_exprs — infer the window from the request, defaulting to the last 7 "
    "days — never by leaving it empty and never as a literal value. Only a date "
    "argument that is optional AND not implied by the request is omitted. Filtering "
    "that CAN be done on the response belongs in `cel_expr` instead — it keeps the "
    "evidence visible on the run.\n\n"
)

#: Stated before the field list rather than after it, because a model that has already
#: decided on an expression does not revisit it.
_EXPRESSION_RULES = (
    "READ THIS FIRST — how to express a watch condition.\n"
    "Write the condition as `cel_expr`, a CEL (Common Expression Language) expression "
    "over three variables: `result` (the check tool's JSON response), `now` (the "
    "current time in the job's timezone, a timestamp), and `prev` (the previous check "
    "result, null on the first run).\n"
    "Gate rule: a boolean result gates the trigger directly; any other result triggers "
    "when non-empty. PREFER returning the matching items over a bare boolean — what "
    "the expression returns is recorded on the run and handed to the model or agent "
    "as the evidence.\n"
    "CEL does date math and boolean logic deterministically, so conditions like "
    "\"a meeting starts within the hour\" belong here, NOT in llm_condition:\n"
    "  cel_expr: result.events.filter(e, has(e.start.dateTime) && "
    "timestamp(e.start.dateTime) > now && "
    "timestamp(e.start.dateTime) - now < duration('1h'))\n"
    "Change detection: result != prev. Guard optional fields with has(): "
    "has(e.attendees) && e.attendees.exists(a, a.email.contains('.ext')).\n"
    "Reserve `llm_condition` for judgement that is genuinely semantic (tone, intent, "
    "\"looks like a company address\") — never for arithmetic, counting, time windows "
    "or string matching. The two COMPOSE: when both are set, the CEL gate runs first "
    "and the model judges only what the expression returned. Use that split whenever a "
    "condition has a mechanical part and a semantic part.\n"
    "Every watch needs cel_expr, llm_condition, or both.\n\n"
)


#: How many times a generated expression that fails to compile or evaluate is sent
#: back with its error. Two is deliberate: the first retry fixes most syntax slips,
#: and past that the model is guessing — better to hand back the best candidate with
#: a warning than to burn calls.
_GENERATE_CONDITION_RETRIES = 2


async def _verify_candidate(
    cel_expr: str | None,
    payload: Any,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Try a candidate expression: compile always, evaluate when there is a payload.

    Returns (verified, evaluation-for-display, error-to-feed-back). A judge-only
    candidate (no expression) is trivially verified — there is nothing to compile.
    """
    if not cel_expr:
        return True, None, None
    try:
        validate_cel_expression(cel_expr)
    except CelSyntaxError as exc:
        return False, None, f"it does not compile: {exc}"
    if payload is None:
        return True, None, None
    try:
        cel = await evaluate_cel(cel_expr, result=payload, now=datetime.now(timezone.utc))
    except CelEvaluationError as exc:
        return False, None, f"it compiles but fails against the sample response: {exc}"
    return True, {"gate": cel.gate, "extracted": cel.value}, None


async def _repair_cel(
    result: dict[str, Any],
    original_prompt: str,
    generate: Callable[[str], Awaitable[dict[str, Any]]],
    user_query: str,
) -> dict[str, Any]:
    """Make sure the generated cel_expr survives verification, correcting it if not.

    Verification and retry count are `_verify_candidate` and
    `_GENERATE_CONDITION_RETRIES`, the same as /generate-condition uses — the two paths
    used to disagree on both, so an improvement to one silently missed the other.

    What stays different is the failure behaviour, deliberately: /generate-condition may
    return an unverified candidate with a note because a human is about to look at it,
    whereas this path is filling in a whole job draft, so it ends by dropping the
    expression and letting a model judge the whole response against the user's own
    words. The worst case is a working (if pricier) job rather than a broken one.

    There is no sample response here — the check tool has not been called yet — so
    verification is compile-only. It deepens to evaluation for free if a payload ever
    becomes available at this point.
    """
    payload = None
    expression = result.get("cel_expr")
    if not isinstance(expression, str) or not expression.strip():
        return result

    candidate = result
    # The initial candidate was generated by the caller, so this loop verifies it and
    # spends at most _GENERATE_CONDITION_RETRIES generations correcting it — the same
    # number of retries /generate-condition allows. Every candidate is verified,
    # including the one the last retry produced.
    for attempt in range(1 + _GENERATE_CONDITION_RETRIES):
        expression = candidate.get("cel_expr")
        expression = expression if isinstance(expression, str) and expression.strip() else None
        if not expression:
            break
        verified, _, error = await _verify_candidate(expression, payload)
        if verified:
            return candidate
        if attempt == _GENERATE_CONDITION_RETRIES:
            break
        logger.info("Generated cel_expr %r rejected: %s — retrying", expression, error)
        retry_prompt = (
            f"{original_prompt}\n\n"
            f"Your previous answer used cel_expr {expression!r}, but {error}\n"
            "Return the whole JSON object again with a cel_expr that is valid CEL over "
            "`result`, `now` and `prev`. If the condition cannot be expressed in CEL, omit "
            "cel_expr and put the judgement in `llm_condition`."
        )
        try:
            retried = await generate(retry_prompt)
        except Exception:
            logger.warning("Retry for an unusable cel_expr failed", exc_info=True)
            break
        # Merged so the rest of the draft survives a retry that only restates the
        # condition — but an omitted cel_expr is an answer, not a gap: the retry prompt
        # invites the model to drop it and judge instead. Merging would have resurrected
        # the uncompilable expression, failed it again on the identical error, and burnt
        # the remaining retry before reaching the fallback.
        candidate = {**candidate, **retried}
        if "cel_expr" not in retried:
            candidate.pop("cel_expr", None)

    logger.info("No usable cel_expr after retries — falling back to a judged condition")
    repaired = {k: v for k, v in candidate.items() if k != "cel_expr"}
    repaired["cel_expr"] = None
    if not repaired.get("llm_condition"):
        repaired["llm_condition"] = user_query
    return repaired


def _coerce_enum(enum_cls: type, value: object) -> Any:
    """Map a generated string onto an enum, dropping anything unrecognised.

    The value comes from a model, every field is optional, and the form leaves an
    absent field alone — so dropping a bad value degrades gracefully instead of
    failing the whole generation.
    """
    if not isinstance(value, str):
        return None
    try:
        return enum_cls(value.strip().lower())
    except ValueError:
        logger.info("Discarding unrecognised generated %s %r", enum_cls.__name__, value)
        return None


def _coerce_id(value: object, allowed: set[int]) -> int | None:
    """Map a generated id onto one the user can actually reach.

    The model is given a list to choose from, but it can hallucinate an id; picking a
    sub-agent or channel the user has no access to would be a quiet authorization
    problem, so anything outside the offered set is discarded.
    """
    try:
        candidate = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if candidate not in allowed:
        logger.info("Discarding generated id %r outside the offered set", value)
        return None
    return candidate


def _agent_choices(sub_agents: list[SubAgent]) -> list[dict[str, Any]]:
    """The sub-agents a generated job may pick from, as the prompt sees them.

    A sub-agent's description lives on its config version, not on the agent — and that
    version is a LEFT JOIN, so it is absent for an agent with no default version. Both
    have to be tolerated: an agent with no description is still a valid choice.
    """
    choices: list[dict[str, Any]] = []
    for agent in sub_agents:
        if agent.name == "voice-agent":
            continue
        version = getattr(agent, "config_version", None)
        description = getattr(version, "description", None) or ""
        choices.append({"id": agent.id, "name": agent.name, "description": description[:200]})
    return choices


def _build_draft(generated: dict[str, Any]) -> ScheduledJobDraft:
    """Assemble a draft field by field, dropping only what will not validate.

    Built one field at a time on purpose: a single unusable value (a malformed run_at,
    a string where a number belongs) would otherwise throw away an entire generation
    that was mostly right, and the caller can fill one gap far more easily than retype
    everything.
    """
    accepted: dict[str, Any] = {}
    for key, value in generated.items():
        if value is None:
            continue
        try:
            ScheduledJobDraft(**{key: value})
        except ValidationError:
            logger.info("Discarding generated %s=%r: not a usable value", key, value)
            continue
        accepted[key] = value
    return ScheduledJobDraft(**accepted)


def _get_scheduler_service(request: Request) -> SchedulerService:
    return request.app.state.scheduler_service  # type: ignore[no-any-return]


@router.post(
    "/generate-job-draft",
    response_model=ScheduledJobDraft,
    summary="Draft a whole scheduled job from a one-line description.",
    description=(
        "Given the available MCP tools and a natural-language request, returns a partial "
        "ScheduledJobCreate: job type, schedule, check tool and arguments, condition, "
        "outcome and delivery. Fields it cannot infer are omitted for the caller to fill in."
    ),
)
async def generate_job_draft(
    data: GenerateJobDraftRequest,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
) -> ScheduledJobDraft:
    """Generate a whole job from one sentence: type, schedule, tool, condition, outcome.

    The sub-agents and delivery channels the model may choose from are read here rather
    than accepted from the caller, so a generated job can only ever reference something
    the user can already reach.
    """
    tools_summary = json.dumps(
        [
            {"name": t.get("name"), "description": t.get("description"), "input_schema": t.get("input_schema")}
            for t in data.tools
        ],
        indent=2,
    )
    # Offer only what this user can reach: the model picks from these, and anything
    # outside the offered ids is discarded after the call.
    # The same set create_job validates against — offering anything wider produces a
    # job the user cannot save, which is exactly what an is_admin=True offer did.
    sub_agents = await _get_scheduler_service(request).schedulable_sub_agents(db, current_user.id)
    agent_choices = _agent_choices(sub_agents)
    channels = await request.app.state.delivery_channel_repository.list_all_channels(db)
    channel_choices = [
        {"id": c.id, "name": c.name, "description": (c.description or "")[:120]} for c in channels
    ]
    allowed_agent_ids = {c["id"] for c in agent_choices}
    allowed_channel_ids = {c["id"] for c in channel_choices}

    # The model is told the time, in the user's timezone. Without it, any request that
    # implies a date ("this week's meetings") gets one invented from training data —
    # which is how generated check_args ended up watching windows in the past.
    try:
        user_tz = await _get_scheduler_service(request)._resolve_timezone(db, None, current_user.id)
    except (ValueError, RuntimeError):
        user_tz = None
    try:
        tzinfo = resolve_timezone(user_tz)
    except ValueError:
        tzinfo = resolve_timezone(None)
    user_tz = str(tzinfo)
    local_now = datetime.now(timezone.utc).astimezone(tzinfo)

    prompt = (
        "You are a scheduling-assistant. Given the available MCP tools, sub-agents and "
        "delivery channels below, turn the user's request into a scheduled job. Fill only "
        "the fields the request implies and omit the rest.\n\n"
        f"The current date and time is {local_now.isoformat()} ({user_tz}). Anything "
        "before this is the past.\n\n"
        + _ARGS_RULES
        + _EXPRESSION_RULES +
        "A) `job_type`: 'watch' when the request is about noticing a condition and then "
        "acting ('tell me when…', 'check whether…'); 'task' when it is about doing "
        "something on a cadence regardless of any condition.\n"
        "B) `name`: a short job name (max 8 words).\n"
        "C) `schedule_kind`: 'cron', 'interval' or 'once'. With 'cron' give `cron_expr` "
        "(five fields, and use ranges for working hours, e.g. '0 7-18 * * 1-5'); with "
        "'interval' give `interval_seconds` (min 60); with 'once' give `run_at` (ISO 8601).\n"
        "D) `sub_agent_id`: the id of the sub-agent that should do the work, chosen from "
        "the list, when the request needs something done rather than just reported; "
        "`prompt`: the instruction for it. Leave both out for a plain notification.\n"
        "E) `delivery_channel_id`: the id of the channel to deliver to, chosen from the "
        "list, when the request names a destination (Slack, email, …).\n"
        "F) `destroy_after_trigger`: false when the request wants to be told every time "
        "the condition holds; true (the default) when once is enough.\n\n"
        "For a watch job also generate:\n"
        "1. `check_tool`: the **name** of the single best-matching tool from the list.\n"
        "2. `check_args`: a minimal JSON object with the required STATIC arguments to "
        "call that tool — no dates, per the rules at the top.\n"
        "3. `check_args_exprs`: argument name → CEL expression over `now`/`prev`, for "
        "arguments that must move with time. MANDATORY when the tool has required "
        "date/time arguments — they must come from here, not from check_args and not "
        "left empty. Omit it only when no argument involves time.\n"
        "4. `cel_expr`: the CEL condition, per the rules at the top. Prefer an expression "
        "that returns the matching items.\n"
        "5. `llm_condition`: only when part of the condition is genuinely semantic; it "
        "judges what cel_expr returned. Omit it otherwise.\n"
        "6. `notification_message`: a concise notification text that will be sent to the user when "
        "the condition is met. Provide context about what was achieved (e.g., 'Pull request #123 has been merged').\n\n"
        f"Available tools:\n{tools_summary}\n\n"
        f"Available sub-agents:\n{json.dumps(agent_choices, indent=2)}\n\n"
        f"Available delivery channels:\n{json.dumps(channel_choices, indent=2)}\n\n"
        f"User request: {data.query}\n\n"
        "Respond ONLY with a JSON object, no markdown fences, e.g.:\n"
        '{"job_type": "watch", "name": "External invitees check", "schedule_kind": "cron", '
        '"cron_expr": "*/15 7-18 * * 1-5", "check_tool": "tool_name", '
        '"check_args": {"param": "value"}, '
        '"check_args_exprs": {"start_date": "strftime(now - duration(\'168h\'), \'%Y-%m-%d\')"}, '
        '"cel_expr": "result.events.filter(e, has(e.start.dateTime) && '
        "timestamp(e.start.dateTime) > now && timestamp(e.start.dateTime) - now < duration('1h'))\", "
        '"llm_condition": "an attendee looks external to the company", "sub_agent_id": 3, '
        '"prompt": "Research the attendees and write a short report", '
        '"delivery_channel_id": 2, "notification_message": "Upcoming meeting has external attendees"}'
    )

    # Resolve the model from the admin-managed fleet defaults rather than an env-pinned alias:
    # watch-param generation is cheap, high-volume work, so it rides the 'chat:low' tier
    # (falling back to standard 'chat') — the same model_defaults source of truth catalog
    # summarization uses (see catalog.sync.resolve_summarization_alias). Unlike summarization
    # this is a synchronous user request with no fallback path, so a missing default fails closed.
    defaults = await ModelDefaultsRepository().get_all(db)
    model = defaults.get("chat:low") or defaults.get("chat")
    if not model:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No chat model is configured. An admin must set the 'chat' default in the console.",
        )

    async def _generate(instruction: str) -> dict[str, Any]:
        return await gateway_chat_json(
            instruction,
            model=model,
            max_tokens=1024,
            metadata={"user_sub": current_user.sub},  # OIDC subject — the gateway/proxy attributes by sub, not internal id
        )

    try:
        result = await _generate(prompt)
    except Exception as exc:
        logger.warning("Watch-param generation via gateway failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI generation service unavailable",
        ) from exc

    # Check the generated expression before it reaches the form. Stating the language
    # in the prompt reduces the mistake but does not remove it, and an expression that
    # cannot compile produces a job that looks configured and never fires.
    result = await _repair_cel(result, prompt, _generate, data.query)

    # A broken dynamic-argument expression is dropped rather than repaired: unlike
    # the condition, the job is usable without it (the field is simply left for the
    # user), and the form's live resolution is where the author would refine it.
    args_exprs = result.get("check_args_exprs")
    if isinstance(args_exprs, dict):
        kept: dict[str, str] = {}
        for key, expr in args_exprs.items():
            if not isinstance(expr, str) or not expr.strip():
                continue
            try:
                validate_cel_expression(expr)
                kept[key] = expr
            except CelSyntaxError as exc:
                logger.info("Discarding uncompilable generated check_args_exprs[%r] %r: %s", key, expr, exc)
        result["check_args_exprs"] = kept or None

    # Fields the model is never allowed to set: an inline sub-agent would be created for
    # real, a voice call places an outbound phone call and is left for the person to tick
    # deliberately, and the rest are deployment concerns rather than things a sentence
    # implies. (voice_call was excluded because the old runner would have rung on every
    # poll; that is no longer true — evaluation happens before dispatch now, so a watch
    # rings only when its condition is met. It stays excluded for the reason above.)
    generated = {
        key: value
        for key, value in result.items()
        if key in ScheduledJobDraft.model_fields
        and key not in ("sub_agent_parameters", "voice_call", "max_failures", "timezone")
    }
    # Values that cannot be trusted as given: enums may be invented, ids may point at
    # something this user cannot reach, and check_args arrives as a JSON string often
    # enough that ScheduledJobCreate carries a validator for it.
    generated["job_type"] = _coerce_enum(JobType, result.get("job_type"))
    generated["schedule_kind"] = _coerce_enum(ScheduleKind, result.get("schedule_kind"))
    generated["sub_agent_id"] = _coerce_id(result.get("sub_agent_id"), allowed_agent_ids)
    generated["delivery_channel_id"] = _coerce_id(result.get("delivery_channel_id"), allowed_channel_ids)
    if isinstance(generated.get("check_args"), str):
        try:
            generated["check_args"] = json.loads(generated["check_args"])
        except json.JSONDecodeError:
            generated["check_args"] = None

    return _build_draft(generated)


@router.post(
    "/generate-condition",
    response_model=GenerateConditionResponse,
    summary="Write or refine a watch condition with a model, verified against a real payload.",
    description=(
        "Given a natural-language description (and optionally the current condition and "
        "a sample tool response), returns a CEL expression and/or an llm_condition. "
        "Narrower than generate-job-draft: it sees the real response shape, so it can "
        "write field paths that exist — and every candidate is compiled, evaluated "
        "against the sample, and repaired with the error fed back before it is returned."
    ),
)
async def generate_condition(
    data: GenerateConditionRequest,
    db: DbSession,
    current_user: User = Depends(require_auth),
) -> GenerateConditionResponse:
    """Generate or refine just the condition, against the caller's own material."""
    # Expression-writing is the hard end of what this router asks of a model, so it
    # rides the standard chat tier and only falls back to the low one — the reverse
    # of the draft generator's order.
    defaults = await ModelDefaultsRepository().get_all(db)
    model = defaults.get("chat") or defaults.get("chat:low")
    if not model:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No chat model is configured. An admin must set the 'chat' default in the console.",
        )

    payload_section = (
        "A real response from the check tool, to write against — use field paths that "
        f"exist in it:\n{json.dumps(data.result, indent=2, default=str)[:8000]}\n\n"
        if data.result is not None
        else "No sample response is available — guard every field access with has().\n\n"
    )
    current_section = ""
    if data.current_cel_expr or data.current_llm_condition:
        current_section = (
            "The condition as it stands, to refine rather than replace (keep what the "
            "request does not ask to change):\n"
            f"cel_expr: {data.current_cel_expr or '(none)'}\n"
            f"llm_condition: {data.current_llm_condition or '(none)'}\n\n"
        )

    prompt = (
        "You write the condition of a scheduled watch job.\n\n"
        # So timestamps in the sample payload read as past or future, not as
        # training-data guesses. The expression itself must still use `now`, which
        # moves with each run — never a literal timestamp.
        f"The current date and time is {datetime.now(timezone.utc).isoformat()}. "
        "Never write a literal date into the expression; use `now`.\n\n"
        + _EXPRESSION_RULES
        + (f"Check tool: {data.check_tool}\n\n" if data.check_tool else "")
        + payload_section
        + current_section
        + f"Request: {data.query}\n\n"
        'Respond ONLY with a JSON object, no markdown fences: {"cel_expr": "…" | null, '
        '"llm_condition": "…" | null}. Set llm_condition only for the genuinely '
        "semantic part of the request, if any."
    )

    async def _generate(instruction: str) -> dict[str, Any]:
        return await gateway_chat_json(
            instruction,
            model=model,
            max_tokens=1024,
            metadata={"user_sub": current_user.sub},
        )

    notes: list[str] = []
    candidate: dict[str, Any] = {}
    instruction = prompt
    verified = False
    evaluation: dict[str, Any] | None = None
    for _ in range(1 + _GENERATE_CONDITION_RETRIES):
        try:
            candidate = await _generate(instruction)
        except Exception as exc:
            logger.warning("Condition generation via gateway failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI generation service unavailable",
            ) from exc
        cel_expr = candidate.get("cel_expr")
        cel_expr = cel_expr if isinstance(cel_expr, str) and cel_expr.strip() else None
        candidate["cel_expr"] = cel_expr
        verified, evaluation, error = await _verify_candidate(cel_expr, data.result)
        if verified:
            break
        logger.info("Generated cel_expr %r rejected: %s — retrying", cel_expr, error)
        instruction = (
            f"{prompt}\n\nYour previous answer used cel_expr {cel_expr!r}, but {error}\n"
            "Return the whole JSON object again with a corrected expression."
        )
    else:
        notes.append(
            "The expression could not be verified — it is returned as the best "
            "candidate, but check it in the tester before saving."
        )

    llm_condition = candidate.get("llm_condition")
    llm_condition = llm_condition if isinstance(llm_condition, str) and llm_condition.strip() else None
    if not candidate.get("cel_expr") and not llm_condition:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The model produced no usable condition — rephrase the request.",
        )
    if verified and evaluation is None and candidate.get("cel_expr") and data.result is None:
        notes.append("Compiled, but not evaluated: run the check first to verify against a real response.")

    return GenerateConditionResponse(
        cel_expr=candidate.get("cel_expr"),
        llm_condition=llm_condition,
        verified=verified,
        evaluation=evaluation,
        notes=notes,
    )


@router.post(
    "/validate-args-expr",
    response_model=ValidateArgsExprResponse,
    summary="Resolve a dynamic-arguments expression without running the job.",
    description=(
        "Evaluates a check_args_expr against the current time and returns the merged "
        "arguments the check tool would be called with right now — the same resolution "
        "the scheduler performs on each run."
    ),
)
async def validate_args_expr(
    data: ValidateArgsExprRequest,
    _current_user: User = Depends(require_auth),
) -> ValidateArgsExprResponse:
    """Resolve dynamic arguments for preview. Pure computation, nothing stored."""
    try:
        dynamic = await evaluate_arg_exprs(
            data.check_args_exprs, now=datetime.now(timezone.utc), prev=data.prev
        )
    except (CelSyntaxError, CelEvaluationError) as exc:
        return ValidateArgsExprResponse(valid=False, error=str(exc))
    return ValidateArgsExprResponse(valid=True, resolved={**(data.check_args or {}), **dynamic})


@router.post(
    "/validate-condition",
    response_model=ValidateConditionResponse,
    summary="Try a watch condition against a payload without creating a job.",
    description=(
        "Evaluates a CEL condition against a tool response and reports what it "
        "extracts and whether it would gate the trigger. Exists so an expression is "
        "seen working against a real payload before a job depends on it."
    ),
)
async def validate_condition(
    data: ValidateConditionRequest,
    _current_user: User = Depends(require_auth),
) -> ValidateConditionResponse:
    """Evaluate a condition against a payload the caller supplies.

    Pure computation on data the caller already has: no tool is called, nothing is
    stored, and no other user's data is reachable. It is authenticated only so it is
    not an open expression evaluator.
    """
    if data.cel_expr:
        return await _validate_cel_condition(data)
    if data.llm_condition:
        # A judged condition cannot be previewed without a model call; what the author
        # needs to see is what the model will be given, which is the whole response.
        return ValidateConditionResponse(
            valid=True,
            extracted=data.result,
            condition_met=None,
            notes=["This is what the model will be given to judge on each run."],
        )
    return ValidateConditionResponse(
        valid=False,
        error="Nothing to evaluate: provide cel_expr, llm_condition, or both.",
        notes=[CEL_SYNTAX_HINT],
    )


async def _validate_cel_condition(data: ValidateConditionRequest) -> ValidateConditionResponse:
    """Preview a CEL condition: same evaluator, same gate rule as the scheduler's run.

    `now` is the server's current time — a preview is asked "would this fire right
    now?", and any other clock would make it disagree with the run it predicts.
    """
    assert data.cel_expr is not None
    try:
        cel = await evaluate_cel(
            data.cel_expr,
            result=data.result,
            now=datetime.now(timezone.utc),
            prev=data.prev,
        )
    except CelSyntaxError as exc:
        return ValidateConditionResponse(valid=False, error=str(exc), notes=[CEL_SYNTAX_HINT])
    except CelEvaluationError as exc:
        return ValidateConditionResponse(
            valid=True,
            error=f"The expression compiles but failed against this payload: {exc}",
            notes=[
                "On a scheduled run this fails the run (and counts toward max failures) "
                "rather than reading as 'not met'. Guard optional fields with has().",
            ],
        )

    notes: list[str] = []
    if cel.is_boolean:
        notes.append("The expression returned a boolean, which is the gate itself.")
        if data.llm_condition:
            notes.append(
                "With an llm_condition on top, the model would receive only this boolean "
                "as the extracted value — prefer returning the matching items so the "
                "model judges evidence."
            )
    else:
        notes.append(
            "The expression returned a value, so the gate is 'non-empty'. What you see "
            "extracted is what a run records and hands to the model or agent."
        )

    if data.llm_condition:
        if not cel.gate:
            notes.append("The gate is not met, so the model would never be asked on this payload.")
            return ValidateConditionResponse(
                valid=True, extracted=cel.value, condition_met=False, notes=notes
            )
        notes.append(
            "The gate is met — on a run, the model would now judge the extracted value. "
            "That needs a model call, so it is not simulated here."
        )
        return ValidateConditionResponse(
            valid=True, extracted=cel.value, condition_met=None, notes=notes
        )

    return ValidateConditionResponse(
        valid=True, extracted=cel.value, condition_met=cel.gate, notes=notes
    )


@router.post(
    "/jobs",
    response_model=ScheduledJob,
    status_code=status.HTTP_201_CREATED,
    summary="Create a scheduled job with push notifications to slack, email or google chat.",
    description=(
        "Create a new scheduled job that will run on behalf of the current user. "
        "For `job_type='task'`, supply a `sub_agent_id` referencing an `automated` sub-agent. "
        "For `job_type='watch'`, supply `check_tool`, `check_args`, and a condition — `cel_expr` "
        "(a CEL expression over the tool response that extracts and gates deterministically), "
        "`llm_condition` (judged by a model), or both (the CEL gate runs first, the model judges "
        "what it returned) — so the scheduler can poll before optionally invoking an agent. "
        "Supply a `delivery_channel_id` referencing a registered delivery channel. "
        "Cron expressions are evaluated in the job's `timezone` (defaults to the user's settings timezone), "
        "so write them as the user's local wall-clock time — never convert to UTC."
    ),
    operation_id="scheduler_create_job",
    tags=["MCP"],
)
async def create_job(
    request: Request,
    data: ScheduledJobCreate,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    """Create a new scheduled job for the authenticated user."""
    service = _get_scheduler_service(request)
    try:
        return await service.create_job(db=db, data=data, actor=current_user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.get(
    "/jobs",
    response_model=list[ScheduledJob],
    summary="List scheduled jobs.",
    description="Returns all scheduled jobs owned by the current user.",
    tags=["MCP"],
    operation_id="scheduler_list_jobs",
)
async def list_jobs(
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> list[ScheduledJob]:
    service = _get_scheduler_service(request)
    return await service.list_jobs(db=db, user_id=current_user.id)


@router.get(
    "/jobs/{job_id}",
    response_model=ScheduledJob,
    summary="Get a scheduled job.",
    tags=["MCP"],
    operation_id="scheduler_get_job",
)
async def get_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.patch(
    "/jobs/{job_id}",
    response_model=ScheduledJob,
    summary="Update a scheduled job.",
    description="Partial update — only supplied fields are changed.",
    tags=["MCP"],
    operation_id="scheduler_update_job",
)
async def update_job(
    job_id: int,
    data: ScheduledJobUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)

    # Use model_fields_set to detect which fields were explicitly provided
    # If field is in model_fields_set, pass its value (including None to clear)
    # If field is not in model_fields_set, pass _UNSET to keep current value
    try:
        job = await service.update_job(
            db=db,
            job_id=job_id,
            data=data,
            actor=current_user,
            name=data.name if "name" in data.model_fields_set else _UNSET,
            prompt=data.prompt if "prompt" in data.model_fields_set else _UNSET,
            notification_message=(
                data.notification_message if "notification_message" in data.model_fields_set else _UNSET
            ),
            check_tool=data.check_tool if "check_tool" in data.model_fields_set else _UNSET,
            check_args_exprs=data.check_args_exprs if "check_args_exprs" in data.model_fields_set else _UNSET,
            cel_expr=data.cel_expr if "cel_expr" in data.model_fields_set else _UNSET,
            llm_condition=data.llm_condition if "llm_condition" in data.model_fields_set else _UNSET,
            check_args=data.check_args if "check_args" in data.model_fields_set else _UNSET,
            delivery_channel_id=(
                data.delivery_channel_id if "delivery_channel_id" in data.model_fields_set else _UNSET
            ),
            sub_agent_id=data.sub_agent_id if "sub_agent_id" in data.model_fields_set else _UNSET,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except IntegrityError as e:
        # Backstop: the service validates schedule combinations up front, but any
        # constraint violation that still reaches the database must come back as an
        # actionable client error, not an opaque 500 (MCP callers can self-correct
        # on a message, not on "Internal Server Error").
        detail = str(getattr(e, "orig", e)).splitlines()[0]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Update rejected by a database constraint: {detail}",
        ) from e
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.delete(
    "/jobs/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a scheduled job.",
    tags=["MCP"],
    operation_id="scheduler_delete_job",
)
async def delete_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> None:
    service = _get_scheduler_service(request)
    ok = await service.delete_job(db=db, job_id=job_id, actor=current_user)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")


@router.post(
    "/jobs/{job_id}/run-now",
    response_model=RunNowResponse,
    status_code=202,
    summary="Trigger an immediate test run for a scheduled job.",
    description=(
        "Dispatches the job asynchronously through the full execution pipeline: resolves the "
        "user's offline token, calls agent-runner (A2A), evaluates the watch condition if "
        "applicable, delivers the configured webhook notification, and records the run. "
        "Returns 202 immediately; the result is delivered via the scheduler_notification "
        "WebSocket event when execution completes."
    ),
)
async def run_job_now(
    job_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> RunNowResponse:
    """Immediately dispatch a job in the background and return 202."""
    service = _get_scheduler_service(request)
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    engine: SchedulerEngine = request.app.state.scheduler_engine
    run_id: int = await engine._repo.create_run(db, job_id)
    await db.commit()
    background_tasks.add_task(engine.run_job_now, job, run_id)
    return RunNowResponse(job_id=job_id, run_id=run_id)


@router.post(
    "/jobs/{job_id}/pause",
    response_model=ScheduledJob,
    summary="Pause a scheduled job.",
    tags=["MCP"],
    operation_id="scheduler_pause_job",
)
async def pause_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    ok = await service.pause_job(db=db, job_id=job_id, actor=current_user)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    assert job is not None
    return job


@router.post(
    "/jobs/{job_id}/resume",
    response_model=ScheduledJob,
    summary="Resume a paused scheduled job.",
    description="Re-enables the job and resets the failure counter.",
    tags=["MCP"],
    operation_id="scheduler_resume_job",
)
async def resume_job(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    try:
        ok = await service.resume_job(db=db, job_id=job_id, actor=current_user)
    except ValueError as e:
        # Completed once-jobs and unresolvable stored timezones must surface as
        # an actionable 400, not an opaque 500.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    assert job is not None
    return job


@router.get(
    "/jobs/{job_id}/runs",
    response_model=list[ScheduledJobRun],
    summary="List execution history for a scheduled job.",
    description="Returns the most recent execution runs (up to 50) for the given job.",
)
async def list_runs(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> list[ScheduledJobRun]:
    service = _get_scheduler_service(request)
    runs = await service.list_runs(db=db, job_id=job_id, user_id=current_user.id)
    if runs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return runs


@router.get(
    "/jobs/{job_id}/runs/{run_id}",
    response_model=ScheduledJobRun,
    summary="Get a single execution run of a scheduled job.",
    description="Returns one run by id, however old — the run listing is capped to the most recent 50.",
)
async def get_run(
    job_id: int,
    run_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJobRun:
    service = _get_scheduler_service(request)
    run = await service.get_run(db=db, job_id=job_id, run_id=run_id, user_id=current_user.id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run
