"""Scheduler API router — exposes schedule management as MCP tools.

All endpoints are tagged "MCP" so FastApiMCP auto-exposes them as MCP tools,
allowing the orchestrator to create and manage scheduled jobs conversationally.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError


from ..db.session import DbSession
from ..dependencies import is_admin_mode, require_auth, require_auth_or_bearer_token
from ..models.scheduled_job import (
    GenerateConditionRequest,
    GenerateConditionResponse,
    GenerateJobDraftRequest,
    JobGroupPermissionResponse,
    JobPermissionsUpdate,
    JobRunStatus,
    JobType,
    ResumeRunRequest,
    ValidateArgsExprRequest,
    ValidateArgsExprResponse,
    RunNowResponse,
    RunTrigger,
    ScheduledJob,
    ScheduledJobCreate,
    ScheduledJobDraft,
    ScheduledJobRun,
    ScheduledJobUpdate,
    ScheduleKind,
    SharedJobDefinition,
    SuspendJobRequest,
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
from ..services.llm_gateway import GatewayReplyTruncated, gateway_chat_json
from ringier_a2a_sdk.cost_tracking.attribution import attribution_scope

from ..services.spend_attribution import SERVICE_CONSOLE
from ..services.scheduler_engine import SchedulerEngine
from ..services.scheduler_service import _TRIGGER_FIELDS, _UNSET, SchedulerAccessError, SchedulerService
from ..utils.timezones import resolve_timezone
from .mcp_router import MCPTool, _list_mcp_tools, rank_mcp_tools

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scheduler")

#: How many tools the draft generator shows the model. The registry can hold hundreds
#: of tools across dozens of servers — with every schema included that is on the order
#: of a hundred thousand tokens for an answer of twenty lines, and the one name the
#: model needs is buried in it (the reply came back unparseable, as a 200 of nulls).
#: Fifteen leaves room for a query that matches a family of tools (list/get/search
#: variants across servers) while keeping the prompt within a few thousand tokens.
_DRAFT_TOOL_CANDIDATES = 15


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


#: Thinking off for the draft and condition generators. Both are mechanical JSON-filling
#: from a prompt that already states the rules, and reasoning tokens count against
#: `max_tokens`: on the low tier a reasoning model spent 979 of a 1024 budget thinking
#: and was cut off 41 tokens into the answer, which read as "no usable draft". The proxy
#: drops the parameter for models that have no such control.
_GENERATION_REASONING = "none"

#: What the person sees when the model's reply hit the output budget. Not a request to
#: rephrase — the request was fine.
_TRUNCATED_DETAIL = "The model's reply was cut off before it finished — try again."

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
        # prev bound to the sample itself: the run binds the stored last result, which the
        # sample stands in for. Unbound, even the prompt's own `result != prev` idiom failed
        # to evaluate, and a correct change-detection expression was "repaired" or refused.
        cel = await evaluate_cel(cel_expr, result=payload, now=datetime.now(timezone.utc), prev=payload)
    except CelEvaluationError as exc:
        return False, None, f"it compiles but fails against the sample response: {exc}"
    return True, {"gate": cel.gate, "extracted": cel.value}, None


async def _repair_cel(
    result: dict[str, Any],
    original_prompt: str,
    generate: Callable[[str], Awaitable[dict[str, Any]]],
    user_query: str,
    payload: Any = None,
    judge_fallback: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Make sure the generated cel_expr survives verification, correcting it if not.

    Verification and retry count are `_verify_candidate` and
    `_GENERATE_CONDITION_RETRIES`, the same as /generate-condition uses — the two paths
    used to disagree on both, so an improvement to one silently missed the other.

    What stays different is the failure behaviour, deliberately: /generate-condition may
    return an unverified candidate with a note because a human is about to look at it,
    whereas this path is filling in a whole job draft, so it ends by dropping the
    expression and letting a model judge the whole response against the user's own
    words. The worst case is a working (if pricier) job rather than a broken one.

    Verification is compile-only unless the caller has a sample response (an edited
    job's last run, or a fresh check); with one, a candidate must also evaluate on it.

    `judge_fallback=False` is for an edit: the user's words there describe a change
    ("also tell me when it is cancelled"), which is no condition to judge a whole
    response against, so an expression that cannot be repaired is dropped with nothing
    put in its place and the caller decides what that means.

    Returns the draft and whether the proposed expression survived: False only when one
    was proposed and every attempt failed, so a caller never has to reconstruct that from
    which fields happen to be missing afterwards.
    """
    expression = result.get("cel_expr")
    if not isinstance(expression, str) or not expression.strip():
        return result, True

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
            return candidate, True
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
    if judge_fallback and not repaired.get("llm_condition"):
        repaired["llm_condition"] = user_query
    return repaired, False


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


def _coerce_name(value: object, allowed: set[str]) -> str | None:
    """A generated name, or None when it is not a string or not among the offered ones.

    The string guard matters: a model asked for "the single best-matching tool" answers
    with a list when several match, and a bare `in` on a set would raise on it.
    """
    if not isinstance(value, str):
        if value is not None:
            logger.info("Discarding generated name %r: not a string", value)
        return None
    if value not in allowed:
        logger.info("Discarding generated name %r outside the offered set", value)
        return None
    return value


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


async def _candidate_tools(request: Request, user: User, query: str, pinned: str | None = None) -> list[MCPTool]:
    """The tools worth offering the draft generator for `query`: the user's own catalogue,
    ranked by the search endpoint's scorer, cut to `_DRAFT_TOOL_CANDIDATES`.

    `pinned` is an edited job's current tool, offered whether or not it ranks: a change
    like "also tell me when it is cancelled" shares no words with the tool, and a tool
    missing from the offer is discarded as an invention — taking the job's arguments and
    condition with it.

    Read here rather than accepted from the caller for the same reason the sub-agents
    and channels are: a generated job may only reference what this user can reach, and
    the request body is not where that is decided.
    """
    try:
        catalogue = await _list_mcp_tools(request, user)
    except HTTPException:
        raise
    except Exception as exc:
        # When the gateway starts failing this is the only line an operator gets, so
        # it carries the upstream status where there is one, and the traceback.
        upstream = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(
            "Could not read the tool catalogue for draft generation (%s, upstream status %s)",
            type(exc).__name__,
            upstream,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not read the tool catalogue — try again shortly.",
        ) from exc
    candidates = rank_mcp_tools(catalogue.tools, query, _DRAFT_TOOL_CANDIDATES)
    if pinned and all(t.name != pinned for t in candidates):
        # Only from the user's own catalogue: a tool they can no longer reach is not
        # offered back just because the job still names it.
        candidates += [t for t in catalogue.tools if t.name == pinned][:1]
    # An empty offer is legitimate — a task job needs no tool, and the request may
    # simply share no vocabulary with the catalogue — but it is worth its own line, since
    # the model can then only name a tool from memory, which is discarded afterwards.
    if not candidates:
        logger.info(
            "Draft generation offers no tools for query %r (%d in the catalogue%s)",
            query,
            len(catalogue.tools),
            "" if catalogue.tools else " — empty, e.g. under impersonation",
        )
    else:
        logger.info(
            "Draft generation offers %d of %d tools for query %r: %s",
            len(candidates),
            len(catalogue.tools),
            query,
            [t.name for t in candidates],
        )
    return candidates


#: The fields an edit may change, per job type: the job's definition. Schedule and
#: delivery have their own owners on a shared job (the trigger policy, each subscriber),
#: and the type of an existing job does not change — so a draft that proposed them would
#: show the user a change the page then silently does not apply.
#: console-frontend's `lib/watchDraft.ts` writes back exactly the watch set; a test pins
#: it, so a change here fails until the frontend is told.
_EDITABLE_DRAFT_FIELDS: dict[JobType, frozenset[str]] = {
    JobType.WATCH: frozenset(
        {
            "check_tool",
            "check_args",
            "check_args_exprs",
            "cel_expr",
            "llm_condition",
            "notification_message",
            "prompt",
            "sub_agent_id",
            "destroy_after_trigger",
        }
    ),
    JobType.TASK: frozenset({"sub_agent_id", "prompt"}),
}


def _edited_type(current: ScheduledJobDraft) -> JobType:  # type: ignore[valid-type]
    """The type of the job being edited: as sent, else read off its shape."""
    stated = _coerce_enum(JobType, getattr(current.job_type, "value", current.job_type))
    if stated is not None:
        return stated
    return JobType.WATCH if current.check_tool else JobType.TASK


#: How much of a sample response goes into the prompt. The same cut /generate-condition
#: makes: enough to show the shape, not a whole mailbox.
_DRAFT_SAMPLE_CHARS = 8000

_EDIT_REPLY_NOTE = (
    "\nThat example is a new job. For this EDIT, reply with only the fields that change, "
    'e.g. {"cel_expr": "<the whole new expression>", "llm_condition": null}.'
)


def _sample_section(sample: Any) -> str:
    """The check tool's real response, when the caller has one, so paths in cel_expr exist."""
    if sample is None:
        return ""
    rendered = json.dumps(sample, separators=(",", ":"), default=str)[:_DRAFT_SAMPLE_CHARS]
    return f"A real response of the check tool. Field paths in cel_expr MUST exist in it:\n{rendered}\n\n"


def _editing_section(current: ScheduledJobDraft) -> str:  # type: ignore[valid-type]
    """Tell the model it is changing a job, not writing one, and how to say so."""
    job_type = _edited_type(current)
    editable = _EDITABLE_DRAFT_FIELDS[job_type]
    shown = {k: v for k, v in current.model_dump(exclude_none=True, mode="json").items() if k in editable}
    watch_rules = (
        "When you change cel_expr, give the whole new expression (the current one with the "
        "change folded in), never a fragment. When you change check_tool, also give its "
        "check_args and cel_expr: the current ones were written for the old tool. "
        if job_type is JobType.WATCH
        else ""
    )
    return (
        f"You are EDITING an existing {job_type.value} job, not creating one. The job as it stands:\n"
        f"{json.dumps(shown, separators=(',', ':'))}\n"
        "Read the request below as a change to this job. Reply with ONLY the fields that "
        "change, with their new values — a field you leave out is kept exactly as it is. "
        "To remove a field, set it to null. "
        + watch_rules
        + f"Only these fields can change: {', '.join(sorted(editable))}.\n\n"
    )


def _apply_edit(
    current: ScheduledJobDraft,  # type: ignore[valid-type]
    proposed: dict[str, Any],
    draft: ScheduledJobDraft,  # type: ignore[valid-type]
) -> ScheduledJobDraft:  # type: ignore[valid-type]
    """The edited job: `current` with the model's changes applied, returned whole.

    The model answers with changes only — omitted means kept, null means removed — and
    the merge happens here rather than in each client, so "what did the edit do" has one
    answer. Returned whole so a client can diff it against what it sent.

    `draft` holds the proposed values that survived coercion; `proposed` is the raw reply,
    read only for its explicit nulls. A value dropped by coercion (an unreachable
    sub-agent, an invented tool) is therefore neither applied nor read as a removal: the
    current value stands. A value restated as it already was is no change either — models
    echo the fields around the one they edit, and counting the echo as a change made the
    exclusivity rules below keep both outcomes.
    """
    job_type = _edited_type(current)
    editable = _EDITABLE_DRAFT_FIELDS[job_type]
    before = current.model_dump(exclude_none=True)
    after = dict(before)
    removed = {
        key
        for key, value in proposed.items()
        # check_tool is what a watch is about; nulling it leaves nothing to edit.
        if value is None and key in editable and key != "check_tool" and key in before
    }
    for key in removed:
        after.pop(key)
    changed = {key for key in draft.model_fields_set & editable if getattr(draft, key) != before.get(key)}
    for key in changed:
        after[key] = getattr(draft, key)
    touched = changed | removed

    if job_type is JobType.WATCH:
        # A new tool returns a different shape: what was written for the old one goes
        # unless the edit restated it — the rule the form applies when the tool is picked
        # by hand.
        if "check_tool" in changed:
            for key in ("check_args", "check_args_exprs", "cel_expr"):
                if key not in changed:
                    after.pop(key, None)
        # The two outcomes are exclusive; the one the edit chose wins over the one it kept.
        if "sub_agent_id" in changed and "notification_message" not in touched:
            after.pop("notification_message", None)
        if "notification_message" in changed and "sub_agent_id" not in touched:
            after.pop("sub_agent_id", None)
        # A watch with nothing to decide with cannot be saved; say so here, where the
        # edit made it so, rather than as a save error about a state the AI created.
        had_condition = before.get("cel_expr") or before.get("llm_condition")
        if had_condition and not after.get("cel_expr") and not after.get("llm_condition"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That change would leave the job with no condition — describe what it should trigger on.",
            )

    if after == before:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The model proposed no change to this job — describe the change differently.",
        )
    return ScheduledJobDraft(**after)


@router.post(
    "/generate-job-draft",
    response_model=ScheduledJobDraft,
    summary="Draft a whole scheduled job from a one-line description.",
    description=(
        "Given a natural-language request, returns a partial ScheduledJobCreate: job type, "
        "schedule, check tool and arguments, condition, outcome and delivery. The tools, "
        "sub-agents and channels the draft may reference are the caller's own, read "
        "server-side. Fields it cannot infer are omitted for the caller to fill in; a "
        "generation that infers nothing at all is an error, not an empty draft."
    ),
)
async def generate_job_draft(
    data: GenerateJobDraftRequest,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
) -> ScheduledJobDraft:
    """Generate a whole job from one sentence: type, schedule, tool, condition, outcome.

    The tools, sub-agents and delivery channels the model may choose from are read here
    rather than accepted from the caller, so a generated job can only ever reference
    something the user can already reach. Tools are additionally ranked against the
    request and cut to a handful — see `_DRAFT_TOOL_CANDIDATES`.
    """
    current = data.current
    candidate_tools = await _candidate_tools(
        request, current_user, data.query, pinned=current.check_tool if current else None
    )
    # Compact on purpose: pretty-printed schema is a third more tokens for whitespace the
    # model does not need, and the whole prompt is re-sent on every CEL repair round.
    tools_summary = json.dumps(
        [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in candidate_tools],
        separators=(",", ":"),
    )
    allowed_tool_names = {t.name for t in candidate_tools}
    # Offer only what this user can reach: the model picks from these, and anything
    # outside the offered ids is discarded after the call.
    # The same set create_job validates against — offering anything wider produces a
    # job the user cannot save, which is exactly what an is_admin=True offer did.
    sub_agents = await _get_scheduler_service(request).schedulable_sub_agents(db, current_user.id)
    agent_choices = _agent_choices(sub_agents)
    # Draft offers must list every channel the user could pick, so no page here.
    channels, _ = await request.app.state.delivery_channel_repository.list_all_channels(db)
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
        "`prompt`: the instruction for it. Leave `sub_agent_id` out for a plain "
        "notification; then `prompt` may carry a brief for how the notification is "
        "written (which fields to name, how to build a link from them) when the request "
        "says how it wants to be told.\n"
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
        "6. `notification_message`: for a plain notification, a concise fixed text sent "
        "verbatim when the condition is met (e.g., 'Pull request #123 has been merged'). "
        "Leave it out when a brief in `prompt` says how the message is to be written from "
        "the matched items — give one of the two, never both.\n\n"
        f"Available tools:\n{tools_summary}\n\n"
        f"Available sub-agents:\n{json.dumps(agent_choices, indent=2)}\n\n"
        f"Available delivery channels:\n{json.dumps(channel_choices, indent=2)}\n\n"
        + _sample_section(data.result)
        + (_editing_section(current) if current else "")
        + (f"Requested change: {data.query}\n\n" if current else f"User request: {data.query}\n\n")
        + "Respond ONLY with a JSON object, no markdown fences, e.g.:\n"
        '{"job_type": "watch", "name": "External invitees check", "schedule_kind": "cron", '
        '"cron_expr": "*/15 7-18 * * 1-5", "check_tool": "tool_name", '
        '"check_args": {"param": "value"}, '
        '"check_args_exprs": {"start_date": "strftime(now - duration(\'168h\'), \'%Y-%m-%d\')"}, '
        '"cel_expr": "result.events.filter(e, has(e.start.dateTime) && '
        "timestamp(e.start.dateTime) > now && timestamp(e.start.dateTime) - now < duration('1h'))\", "
        '"llm_condition": "an attendee looks external to the company", "sub_agent_id": 3, '
        '"prompt": "Research the attendees and write a short report", '
        '"delivery_channel_id": 2}'
        + (_EDIT_REPLY_NOTE if current else "")
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

    # Everything below is console-backend doing this user's work, so it runs in their
    # attribution rather than each gateway call being handed a payer: a call added here
    # later is attributed by construction, and forgetting would not misclassify the spend
    # but lose it — the proxy discards a record with no subject. `service` because a draft
    # carries no job id (the job it drafts does not exist yet) and would otherwise be
    # derived as agent spend.
    with attribution_scope(user_sub=current_user.sub, service=SERVICE_CONSOLE):
        async def _generate(instruction: str) -> dict[str, Any]:
            return await gateway_chat_json(
                instruction,
                model=model,
                max_tokens=1024,
                # Payer and service come from the scope above.
                reasoning_effort=_GENERATION_REASONING,
            )

        try:
            result = await _generate(prompt)
        except GatewayReplyTruncated as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_TRUNCATED_DETAIL) from exc
        except Exception as exc:
            logger.warning("Watch-param generation via gateway failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI generation service unavailable",
            ) from exc

        # Check the generated expression before it reaches the form. Stating the language
        # in the prompt reduces the mistake but does not remove it, and an expression that
        # cannot compile produces a job that looks configured and never fires.
        # On an edit, only the job type's own fields count. The rest are dropped here, not
        # at the merge: a broken cel_expr volunteered on a task edit would otherwise go
        # through repair and refuse the edit over a field the job cannot have.
        if current is not None:
            editable = _EDITABLE_DRAFT_FIELDS[_edited_type(current)]
            result = {key: value for key, value in result.items() if key in editable}

        # The sample describes the call it came from. An edit that moves to another tool
        # writes its expression for that tool's response, which the sample is not — so the
        # expression is only compile-checked rather than failed against the wrong shape.
        proposed_tool = result.get("check_tool")
        sample = data.result
        if current is not None and proposed_tool and proposed_tool != current.check_tool:
            sample = None
        result, expression_survived = await _repair_cel(
            result, prompt, _generate, data.query, payload=sample, judge_fallback=current is None
        )
        # On an edit, an expression that could not be repaired is not replaced by anything:
        # not by a judge of the user's words ("also tell me when it is cancelled" is a
        # change, not a condition), and not by a judgement the model offered on a retry,
        # which would quietly turn a free deterministic gate into a model call per poll.
        # Keeping the old expression would apply the rest of the edit without its point,
        # so the edit is refused and the person refines the expression itself.
        if current is not None and not expression_survived:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Could not write a working expression for that change — refine the "
                    "expression directly, with the response in view."
                ),
            )

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
            # An emptied set is dropped, not nulled: on an edit an explicit null reads as
            # "remove the job's expressions", and a model that restated them with a typo
            # did not ask for that.
            if kept:
                result["check_args_exprs"] = kept
            else:
                result.pop("check_args_exprs", None)

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
        # A tool name outside the offered set is an invention — the model saw only the
        # candidates — and would produce a job whose check fails on its first run. The
        # arguments and condition were written against that invented tool, so they go with
        # it: the form applies each of them independently, and pre-filled arguments under a
        # tool the user then picks by hand are exactly the first-run failure being avoided.
        generated["check_tool"] = _coerce_name(result.get("check_tool"), allowed_tool_names)
        if generated["check_tool"] is None and result.get("check_tool") is not None:
            for key in ("check_args", "check_args_exprs", "cel_expr", "llm_condition"):
                generated.pop(key, None)
        if isinstance(generated.get("check_args"), str):
            try:
                generated["check_args"] = json.loads(generated["check_args"])
            except json.JSONDecodeError:
                generated["check_args"] = None

        draft = _build_draft(generated)
        if current is not None:
            return _apply_edit(current, result, draft)
        # A draft with nothing in it is not "the request implied nothing" — it is a
        # generation that produced no usable output (no JSON in the reply, or none of the
        # fields asked for). Rendering that as a 200 left the form silently empty and the
        # logs silent with it. A draft the model filled only partly is a different outcome
        # and stays a success. 422 rather than 503, as /generate-condition answers the same
        # case: the service is up, and a retry-on-503 layer must not re-run a content miss.
        if not draft.model_fields_set:
            logger.warning(
                "Draft generation produced nothing usable for query %r (model %s, %d candidate tools, raw keys %s)",
                data.query,
                model,
                len(candidate_tools),
                sorted(result),
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The model produced no usable draft — rephrase the request.",
            )
        return draft


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

    # As in `generate_job_draft`: the request's own work runs in the caller's attribution.
    with attribution_scope(user_sub=current_user.sub, service=SERVICE_CONSOLE):
        async def _generate(instruction: str) -> dict[str, Any]:
            return await gateway_chat_json(
                instruction,
                model=model,
                max_tokens=1024,
                # Payer and service come from the scope above.
                reasoning_effort=_GENERATION_REASONING,
            )

        notes: list[str] = []
        candidate: dict[str, Any] = {}
        instruction = prompt
        verified = False
        evaluation: dict[str, Any] | None = None
        for _ in range(1 + _GENERATE_CONDITION_RETRIES):
            try:
                candidate = await _generate(instruction)
            except GatewayReplyTruncated as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_TRUNCATED_DETAIL) from exc
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
        "Cron expressions are evaluated in the job's `timezone`, so write them as the user's local "
        "wall-clock time — never convert to UTC. Leave `timezone` unset unless the user named a zone: "
        "unset means each subscriber's own, so the job stays correct if it is later shared to a group. "
        "The creator is the job's owner and its first subscriber; it can be shared later with "
        "`scheduler_share_job`."
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
    summary="List my scheduled jobs.",
    description=(
        "Returns every scheduled job the current user is subscribed to — their own and shared "
        "ones they have activated. `effective_permission` says whether they may edit what the "
        "job does ('owner'/'write') or only their own schedule, delivery and enabled state ('read'). "
        "Jobs shared with the user but not yet activated are listed by `scheduler_list_shared_jobs`."
    ),
    tags=["MCP"],
    operation_id="scheduler_list_jobs",
)
async def list_jobs(
    request: Request,
    db: DbSession,
    response: Response,
    current_user: User = Depends(require_auth_or_bearer_token),
    search: str | None = Query(None, description="Search by job name or prompt"),
    page: int = Query(1, ge=1, description="Page number"),
    # Unbounded by default so the MCP callers keep listing a user's whole schedule.
    limit: int | None = Query(None, ge=1, le=100, description="Items per page"),
) -> list[ScheduledJob]:
    service = _get_scheduler_service(request)
    jobs, total = await service.list_jobs(
        db=db, user_id=current_user.id, search=search, page=page, limit=limit
    )
    # The body stays a bare array: this is an MCP tool, and wrapping it in an
    # envelope would change what every agent calling it receives. The console
    # reads the count it needs for pagination off the header instead.
    response.headers["X-Total-Count"] = str(total)
    return jobs


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
    description=(
        "Partial update — only supplied fields are changed. Each field is routed to where it "
        "lives: what the job DOES (name, prompt, agent, check, condition, max_failures, "
        "trigger_policy) needs write permission; `enabled` and `delivery_channel_id` are always "
        "the caller's own. Schedule fields (schedule_kind, cron_expr, interval_seconds, run_at, "
        "timezone) are ambiguous only when the job has OTHER subscribers: pass `scope='mine'` to "
        "change just the caller's schedule or `scope='everyone'` to change the job's default; "
        "without a scope, 'mine' is assumed — ask the user which they meant first."
    ),
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

    # A trigger field is "touched" only when it arrives non-null, so an explicit null
    # trigger used to be indistinguishable from an absent one: the request returned 200
    # and changed nothing, which reads as "your override is cleared" when it is not.
    # Clearing one is a real operation now, and it has its own route.
    explicit_trigger_nulls = {f for f in _TRIGGER_FIELDS if f in data.model_fields_set}
    if explicit_trigger_nulls and all(getattr(data, f) is None for f in explicit_trigger_nulls):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Sending a null schedule does not clear your own schedule. To follow the job's "
                "default again, call scheduler_follow_default_schedule; to do it for every "
                "subscriber, call scheduler_reset_job_schedules (needs write permission)."
            ),
        )

    # Use model_fields_set to detect which fields were explicitly provided
    # If field is in model_fields_set, pass its value (including None to clear)
    # If field is not in model_fields_set, pass _UNSET to keep current value
    try:
        job = await service.update_job(
            db=db,
            job_id=job_id,
            data=data,
            actor=current_user,
            is_admin=is_admin_mode(request, current_user),
            name=data.name if "name" in data.model_fields_set else _UNSET,
            prompt=data.prompt if "prompt" in data.model_fields_set else _UNSET,
            notification_message=(
                data.notification_message if "notification_message" in data.model_fields_set else _UNSET
            ),
            check_tool=data.check_tool if "check_tool" in data.model_fields_set else _UNSET,
            check_args_exprs=data.check_args_exprs if "check_args_exprs" in data.model_fields_set else _UNSET,
            cel_expr=data.cel_expr if "cel_expr" in data.model_fields_set else _UNSET,
            llm_condition=data.llm_condition if "llm_condition" in data.model_fields_set else _UNSET,
            destroy_after_trigger=(
                data.destroy_after_trigger if "destroy_after_trigger" in data.model_fields_set else _UNSET
            ),
            check_args=data.check_args if "check_args" in data.model_fields_set else _UNSET,
            delivery_channel_id=(
                data.delivery_channel_id if "delivery_channel_id" in data.model_fields_set else _UNSET
            ),
            sub_agent_id=data.sub_agent_id if "sub_agent_id" in data.model_fields_set else _UNSET,
        )
    except SchedulerAccessError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
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
    description=(
        "For a job the caller owns: deletes it for EVERY subscriber. For a shared job the "
        "caller merely subscribes to: removes only the caller's subscription (unsubscribe)."
    ),
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
    ok = await service.delete_job(
        db=db, job_id=job_id, actor=current_user, is_admin=is_admin_mode(request, current_user)
    )
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

    # Suspension holds EVERY subscription out of the claim; run-now bypasses the claim,
    # so it has to honour the same hold or a read-level subscriber could run a job its
    # writer stopped for everyone.
    if job.suspended_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job is suspended for everyone; it cannot be run until it is resumed.",
        )

    # A job blocked on its owner must not be run again until they answer. The schedule
    # hold does that for scheduled occurrences by way of claim_due_jobs, but run-now
    # bypasses the claim entirely — so without this check a few presses of "Run now"
    # leave several parked runs, each with its own live ask. ADR-0009 states that at most
    # one run of a job is ever parked and leans on it: no dedupe, no supersession, no
    # cancel sweep. The invariant has to hold on every dispatch path, not just the one
    # the claim covers.
    #
    # A resumed run that parks again is NOT a second park: it consumed the ask it was
    # answering before it started, so there is still exactly one.
    if await engine._repo.answerable_parked_run(db, job_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This job is waiting for your authorization. Answer that first — running "
                "it again would stop at the same point."
            ),
        )

    # Recorded as MANUAL so that, should it be interrupted, neither _finalize nor the
    # healer treats a test press as a scheduled occurrence owed a retry.
    run_id: int = await engine._repo.create_run(db, job_id, trigger=RunTrigger.MANUAL)
    await db.commit()
    background_tasks.add_task(engine.run_job_now, job, run_id)
    return RunNowResponse(job_id=job_id, run_id=run_id)


@router.post(
    "/jobs/{job_id}/pause",
    response_model=ScheduledJob,
    summary="Pause a scheduled job for me.",
    description=(
        "Disables the caller's own subscription; on a shared job nobody else is affected. "
        "To stop a shared job for everyone, use `scheduler_suspend_job` (needs write)."
    ),
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


@router.post(
    "/jobs/{job_id}/follow-default-schedule",
    response_model=ScheduledJob,
    summary="Follow the job's default schedule again, dropping your own.",
    description=(
        "Clears the caller's own schedule for a shared job so it follows the job's default "
        "once more, including later changes to it. Only the caller's subscription is touched — "
        "other subscribers and the job's default are untouched, and it needs no write "
        "permission. Idempotent: a job you already follow the default for is returned "
        "unchanged. Use `scheduler_reset_job_schedules` to do this to every subscriber (needs "
        "write)."
    ),
    tags=["MCP"],
    operation_id="scheduler_follow_default_schedule",
)
async def follow_default_schedule(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    try:
        job = await service.follow_default_schedule(db=db, job_id=job_id, actor=current_user)
    except ValueError as e:
        # An unresolvable stored timezone, as on resume: actionable, not a 500.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.post(
    "/jobs/{job_id}/runs/{run_id}/resume",
    response_model=RunNowResponse,
    status_code=202,
    summary="Answer a run parked on the owner's authorization.",
    description=(
        "Continues a run that stopped because a tool needed the owner's credential. For a "
        "run parked by its agent, the answer is delivered to the parked agent-runner task, "
        "the agent retries what was blocked (or is told to stop, on a decline), and the "
        "result is delivered to the job's channel like any other run. For a watch parked by "
        "its own check tool, an approval runs the check again at once and a decline releases "
        "the schedule. Returns 202 with the id of the new RESUMED run "
        "that carries the continued work — it is created before this responds, so the id "
        "is real and pollable; the parked run keeps its own record and stays "
        "`auth_required` for good.\n\n"
        "This is the endpoint the in-task-auth card posts to. It exists because a chat "
        "turn cannot answer a parked scheduled run — the orchestrator would open a new "
        "task on a thread that is already waiting, and the executor rejects it."
    ),
)
async def resume_parked_run(
    job_id: int,
    run_id: int,
    body: ResumeRunRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> RunNowResponse:
    """Deliver the owner's answer to a parked run and continue it in the background."""
    service = _get_scheduler_service(request)
    # Both lookups are scoped to the caller, so another user's job or run is a 404
    # rather than a permission error — the same shape every other route here uses.
    job = await service.get_job(db=db, job_id=job_id, user_id=current_user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    run = await service.get_run(db=db, job_id=job_id, run_id=run_id, user_id=current_user.id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    # A run that is not parked has nothing waiting for this answer. 409 rather than 400:
    # the request is well formed and was valid when the card was rendered — the run has
    # since been answered, superseded or closed. Cards are durable and clicks are late,
    # so this is an ordinary outcome and the client says "already handled".
    if run.status != JobRunStatus.AUTH_REQUIRED or not run.parked_task_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run is not waiting for authorization; it may already have been answered.",
        )

    if job.suspended_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job is suspended for everyone; answer again once it is resumed.",
        )

    engine: SchedulerEngine = request.app.state.scheduler_engine
    # Claim the ask HERE, before backgrounding, so a second click is answered with a 409
    # rather than disappearing into a task nobody is waiting on. The parked run keeps its
    # AUTH_REQUIRED status — it is a true record of how that occurrence ended — so the
    # task id is what says a question is still outstanding, and clearing it is the claim.
    # The check above is the cheap path; this conditional write is what actually decides,
    # because two clicks can pass the check together.
    if not await engine._repo.clear_parked_task(db, run_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run has already been answered.",
        )
    # The resumed run row is written in the SAME transaction that consumes the ask, so
    # the two cannot come apart. Backgrounding the insert instead left a window — a pod
    # roll or an OOM between the commit and the task — where the ask was spent and no run
    # existed: the card vanished (it needs ``parked_task_id``), a second click 409'd, and
    # nothing swept it, because the healer only looks at ``running`` rows. Writing the row
    # here means a death in that window leaves exactly such a row, and a ``resumed`` run
    # earns the fresh attempt ADR-0007 gives any interrupted one.
    resumed_run_id = await engine._repo.create_run(db, job_id, trigger=RunTrigger.RESUMED)
    await db.commit()

    background_tasks.add_task(
        engine.resume_parked_run, job, run, body.decision, body.reply_to, resumed_run_id
    )
    return RunNowResponse(job_id=job_id, run_id=resumed_run_id, status="resuming")


@router.get(
    "/jobs/{job_id}/runs",
    response_model=list[ScheduledJobRun],
    summary="List execution history for a scheduled job.",
    description=(
        "Execution runs for the given job, newest first, one page at a time. "
        "`X-Total-Count` carries how many runs match. Run history only grows, so "
        "this list is always paged — there is no 'return everything' mode."
    ),
)
async def list_runs(
    job_id: int,
    request: Request,
    db: DbSession,
    response: Response,
    current_user: User = Depends(require_auth_or_bearer_token),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    run_status: JobRunStatus | None = Query(
        None, alias="status", description="Filter by run status"
    ),
    search: str | None = Query(None, description="Search by result summary or error message"),
) -> list[ScheduledJobRun]:
    service = _get_scheduler_service(request)
    result = await service.list_runs(
        db=db,
        job_id=job_id,
        user_id=current_user.id,
        limit=limit,
        page=page,
        status=run_status.value if run_status else None,
        search=search,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    runs, total = result
    # Bare array for the same reason as the sibling list endpoints.
    response.headers["X-Total-Count"] = str(total)
    return runs


@router.get(
    "/jobs/{job_id}/runs/{run_id}",
    response_model=ScheduledJobRun,
    summary="Get a single execution run of a scheduled job.",
    description="Returns one run by id, however old — the run listing is paged, newest first.",
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


# ---------------------------------------------------------------------------
# Sharing (ADR-0010): definitions, subscriptions, permissions
# ---------------------------------------------------------------------------


def _translate(e: Exception) -> HTTPException:
    """The service's exceptions as HTTP: not-found, forbidden, bad request."""
    if isinstance(e, LookupError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e) or "Job not found")
    if isinstance(e, SchedulerAccessError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get(
    "/definitions",
    response_model=list[SharedJobDefinition],
    summary="List scheduled job definitions available to me.",
    description=(
        "Every job definition the caller can reach — their own, public templates, and jobs "
        "shared with one of their groups — with `subscription_id` set when they are already "
        "subscribed. Subscribe with `scheduler_subscribe_job` to run one under your own account, "
        "or copy it with `scheduler_copy_job` to make an independent version you own."
    ),
    tags=["MCP"],
    operation_id="scheduler_list_shared_jobs",
)
async def list_shared_definitions(
    request: Request,
    db: DbSession,
    response: Response,
    current_user: User = Depends(require_auth_or_bearer_token),
    search: str | None = Query(None, description="Search by job name or prompt"),
    subscribed: bool | None = Query(
        None, description="Only definitions the caller has (or has not) already activated"
    ),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int | None = Query(None, ge=1, le=100, description="Items per page"),
) -> list[SharedJobDefinition]:
    service = _get_scheduler_service(request)
    definitions, total = await service.list_available_definitions(
        db, current_user.id, search=search, subscribed=subscribed, page=page, limit=limit
    )
    # Bare array for the same reason as scheduler_list_jobs above.
    response.headers["X-Total-Count"] = str(total)
    return definitions


@router.post(
    "/definitions/{definition_id}/subscribe",
    response_model=ScheduledJob,
    status_code=status.HTTP_201_CREATED,
    summary="Subscribe to a shared scheduled job.",
    description=(
        "Activates the job for the caller: it will run under THEIR account, with their "
        "credentials, on the job's default schedule (in their own timezone unless the job pins "
        "one), delivering to their own DM. Returns the caller's job (subscription). Idempotent."
    ),
    tags=["MCP"],
    operation_id="scheduler_subscribe_job",
)
async def subscribe_definition(
    definition_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    try:
        return await service.subscribe(db, definition_id, current_user, is_admin=is_admin_mode(request, current_user))
    except (LookupError, SchedulerAccessError, ValueError) as e:
        raise _translate(e) from e


@router.post(
    "/definitions/{definition_id}/unsubscribe",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unsubscribe from a shared scheduled job.",
    description="Removes only the caller's own subscription; the job itself and other subscribers are untouched.",
    tags=["MCP"],
    operation_id="scheduler_unsubscribe_job",
)
async def unsubscribe_definition(
    definition_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> None:
    service = _get_scheduler_service(request)
    if not await service.unsubscribe(db, definition_id, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You are not subscribed to this job")


@router.delete(
    "/definitions/{definition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a scheduled job definition for every subscriber.",
    description=(
        "Owner or administrator only. Removes the definition and every subscription to it. "
        "The owner's own job id does the same through `scheduler_delete_job`; this is the path "
        "for an owner who has unsubscribed, and for administrators."
    ),
)
async def delete_definition(
    definition_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
) -> None:
    service = _get_scheduler_service(request)
    try:
        await service.delete_definition(db, definition_id, current_user, is_admin=is_admin_mode(request, current_user))
    except (LookupError, SchedulerAccessError) as e:
        raise _translate(e) from e


@router.post(
    "/definitions/{definition_id}/copy",
    response_model=ScheduledJob,
    status_code=status.HTTP_201_CREATED,
    summary="Copy a scheduled job into one I own.",
    description=(
        "A new, independent job owned by the caller, initialised from one they can read, with "
        "no link back: later edits to the original do not reach it. Use this to diverge; "
        "subscribe instead to keep following the author's version."
    ),
    tags=["MCP"],
    operation_id="scheduler_copy_job",
)
async def copy_definition(
    definition_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> ScheduledJob:
    service = _get_scheduler_service(request)
    try:
        return await service.copy_definition(
            db, definition_id, current_user, is_admin=is_admin_mode(request, current_user)
        )
    except (LookupError, SchedulerAccessError, ValueError) as e:
        raise _translate(e) from e


@router.get(
    "/definitions/{definition_id}/permissions",
    response_model=list[JobGroupPermissionResponse],
    summary="Group permissions on a scheduled job definition.",
)
async def get_definition_permissions(
    definition_id: int,
    request: Request,
    db: DbSession,
    response: Response,
    current_user: User = Depends(require_auth),
    search: str | None = Query(None, description="Search by group name"),
    page: int = Query(1, ge=1, description="Page number"),
    # Unbounded by default: the permissions dialog replaces the whole grant set
    # on save, so it must be able to load every grant.
    limit: int | None = Query(None, ge=1, le=100, description="Items per page"),
) -> list[JobGroupPermissionResponse]:
    service = _get_scheduler_service(request)
    try:
        perms, total = await service.list_permissions(
            db,
            definition_id,
            current_user,
            is_admin=is_admin_mode(request, current_user),
            search=search,
            page=page,
            limit=limit,
        )
    except (LookupError, SchedulerAccessError) as e:
        raise _translate(e) from e
    # Bare array for the same reason as the sibling list endpoints.
    response.headers["X-Total-Count"] = str(total)
    return [JobGroupPermissionResponse(**p) for p in perms]


@router.put(
    "/definitions/{definition_id}/permissions",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Share a scheduled job with groups.",
    description=(
        "Replaces the job's group permissions. 'read' lets members subscribe (run it under their "
        "own account) or copy it; 'write' additionally lets them edit what it does, suspend it and "
        "share it on. Needs write on the job. A job whose sub-agent the group cannot reach cannot "
        "be shared to it — share the agent first; sharing a job never grants agent access. "
        "Members of groups gaining or losing access are notified."
    ),
    tags=["MCP"],
    operation_id="scheduler_share_job",
)
async def update_definition_permissions(
    definition_id: int,
    data: JobPermissionsUpdate,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> None:
    service = _get_scheduler_service(request)
    try:
        await service.update_permissions(
            db,
            definition_id,
            [{"user_group_id": gp.user_group_id, "permissions": gp.permissions} for gp in data.group_permissions],
            current_user,
            is_admin=is_admin_mode(request, current_user),
        )
    except (LookupError, SchedulerAccessError, ValueError) as e:
        raise _translate(e) from e


@router.post(
    "/definitions/{definition_id}/suspend",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Suspend a scheduled job for every subscriber.",
    description=(
        "Stops the job for EVERYONE until it is resumed with `scheduler_unsuspend_job`; each "
        "subscriber's own enabled/disabled choice is preserved. Needs write on the job. To stop "
        "only your own copy, use `scheduler_pause_job`."
    ),
    tags=["MCP"],
    operation_id="scheduler_suspend_job",
)
async def suspend_definition(
    definition_id: int,
    request: Request,
    db: DbSession,
    data: SuspendJobRequest | None = None,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> None:
    service = _get_scheduler_service(request)
    try:
        await service.suspend(
            db,
            definition_id,
            current_user,
            reason=data.reason if data else None,
            is_admin=is_admin_mode(request, current_user),
        )
    except (LookupError, SchedulerAccessError) as e:
        raise _translate(e) from e


@router.post(
    "/definitions/{definition_id}/unsuspend",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Lift a scheduled job's suspension.",
    description="The job runs again for every subscriber who has it enabled. Needs write on the job.",
    tags=["MCP"],
    operation_id="scheduler_unsuspend_job",
)
async def unsuspend_definition(
    definition_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> None:
    service = _get_scheduler_service(request)
    try:
        await service.unsuspend(db, definition_id, current_user, is_admin=is_admin_mode(request, current_user))
    except (LookupError, SchedulerAccessError) as e:
        raise _translate(e) from e


@router.post(
    "/definitions/{definition_id}/reset-overrides",
    summary="Reset every subscriber's schedule to the job's default.",
    description=(
        "Clears each subscriber's own schedule so all of them follow the job's default again "
        "(including future changes to it). Needs write on the job. Affected subscribers are "
        "notified. Returns how many were reset."
    ),
    tags=["MCP"],
    operation_id="scheduler_reset_job_schedules",
)
async def reset_definition_overrides(
    definition_id: int,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth_or_bearer_token),
) -> dict[str, int]:
    service = _get_scheduler_service(request)
    try:
        count = await service.reset_overrides(
            db, definition_id, current_user, is_admin=is_admin_mode(request, current_user)
        )
    except (LookupError, SchedulerAccessError) as e:
        raise _translate(e) from e
    return {"reset": count}


@router.put(
    "/definitions/{definition_id}/public",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Publish or unpublish a job definition as an org-wide template (admin).",
)
async def set_definition_public(
    definition_id: int,
    request: Request,
    db: DbSession,
    is_public: bool,
    current_user: User = Depends(require_auth),
) -> None:
    """Unlike sub-agents, only an administrator (in admin mode) may publish a job: a
    public definition is a curated template visible to the whole organisation."""
    if not is_admin_mode(request, current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator (admin mode) required")
    service = _get_scheduler_service(request)
    try:
        await service.set_public(db, definition_id, current_user, is_public)
    except LookupError as e:
        raise _translate(e) from e
