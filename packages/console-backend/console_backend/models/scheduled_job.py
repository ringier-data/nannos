"""Pydantic models for the scheduler — scheduled jobs, runs, and delivery config."""

import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model, field_validator, model_validator

from ..services.cel_condition import CEL_SYNTAX_HINT, CelSyntaxError, validate_cel_expression
from pydantic.fields import FieldInfo

from ..utils.timezones import validate_timezone_name as _validate_timezone_name
from .sub_agent import ModelName, ModelTier, ThinkingLevel


class JobType(str, Enum):
    """Job type: one-shot task (LLM execution) or conditional watch (poll until condition met)."""

    TASK = "task"
    WATCH = "watch"


class ScheduleKind(str, Enum):
    """How the job is scheduled."""

    CRON = "cron"  # Standard cron expression, e.g. "0 9 * * 1-5"
    ONCE = "once"  # Run once at a specific datetime
    INTERVAL = "interval"  # Run every N seconds


class JobRunStatus(str, Enum):
    """Terminal status of a single job execution attempt."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CONDITION_NOT_MET = "condition_not_met"  # Watch check ran but the condition was false


class ConditionEvaluation(BaseModel):
    """How one run decided its watch condition.

    Recorded per run because it explains an occurrence, and because a model's judgement
    cannot be reconstructed afterwards: the reasoning exists only while the run happens.
    Without it "Condition not met" is the entire explanation a user ever gets.
    """

    met: bool
    #: How it was decided. A literal rather than a string so the generated client can
    #: branch on it without widening. "cel" is a CEL expression's own gate;
    #: "cel+judge" is a CEL gate that passed, with a model's verdict on what it matched.
    #: "rule" appears only on runs recorded before conditions became CEL — the
    #: JSONPath+operator machinery is gone, but its run records still explain themselves.
    mode: Literal["rule", "judge", "cel", "cel+judge"]
    #: The value the condition was applied to, serialised and truncated for display.
    #: For CEL modes this is what the expression returned — the evidence itself.
    extracted: Any = None
    #: How many nodes the path matched. Historical "rule"/"judge" runs only.
    match_count: int = 0
    #: The model's own account of its decision. Judge modes only.
    reasoning: str | None = None
    #: What the CEL gate decided, recorded separately from `met` because in
    #: "cel+judge" mode the gate can pass while the model still says no — and a run
    #: must show which stage made the call. CEL modes only.
    gate_met: bool | None = None
    #: Historical "rule" runs only, so those records read on their own terms.
    operator: str | None = None
    expected_value: str | None = None
    #: Rule mode only, so a run can be read without going back to the job — which may
    #: have been edited since.
    operator: str | None = None
    expected_value: str | None = None


class ScheduledJobRun(BaseModel):
    """A single execution record for a scheduled job."""

    id: int
    job_id: int
    started_at: datetime
    completed_at: datetime | None = None
    status: JobRunStatus
    result_summary: str | None = None
    error_message: str | None = None
    conversation_id: str | None = None
    delivered: bool
    condition_evaluation: ConditionEvaluation | None = None


class RunNowResponse(BaseModel):
    """Response for an immediate job trigger (202 Accepted)."""

    job_id: int
    run_id: int
    status: str = "triggered"


class ScheduledJob(BaseModel):
    """Full scheduled job representation returned by the API."""

    id: int
    user_id: str
    sub_agent_id: int | None = None
    name: str
    job_type: JobType
    schedule_kind: ScheduleKind
    cron_expr: str | None = None
    # None on rows migrated without a user-settings timezone — resolved to the
    # deployment default (DEFAULT_TIMEZONE env var) at evaluation time.
    timezone: str | None = None
    interval_seconds: int | None = None
    run_at: datetime | None = None
    next_run_at: datetime
    last_run_at: datetime | None = None
    prompt: str | None = None
    notification_message: str | None = None
    # Watch fields
    check_tool: str | None = None
    check_args: dict[str, Any] | None = None
    check_args_exprs: dict[str, str] | None = None
    cel_expr: str | None = None
    llm_condition: str | None = None
    destroy_after_trigger: bool = True
    last_check_result: dict[str, Any] | None = None
    # Delivery — references a registered delivery channel
    delivery_channel_id: int | None = None
    # Voice call flag
    voice_call: bool = False
    # Control
    enabled: bool
    max_failures: int
    consecutive_failures: int
    paused_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class AutomatedSubAgentConfig(BaseModel):
    """Configuration for an automated sub-agent to execute as part of a scheduled job."""

    # TODO: we should rather suggest to create a system_prompt which is not too long, and to not use too many tools,
    #       so that we could activate the sub-agent without the need of any approval.
    name: str
    description: str = Field(max_length=200, description="Short description of the sub-agent's skill, max 200 chars.")
    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url, Foundry agents use foundry_* fields
    # Bind to either a concrete model alias or a capability tier (mutually exclusive). A tier
    # follows the fleet default for that tier, so it survives a model retirement/upgrade —
    # unlike a pinned alias, which can become unregistered on the gateway.
    model_tier: ModelTier | None = Field(
        default=None,
        description=(
            "PREFERRED way to choose the model. Bind the agent to a capability tier — "
            "'low' (cheap/fast, for trivial tasks), 'standard' (the default, good for most tasks), "
            "or 'premium' (the most capable, for hard reasoning). A tier always resolves to the "
            "current fleet default for that tier, so it keeps working when a model is retired or upgraded. "
            "Use this unless you have a specific reason to pin an exact model. "
            "Exactly one of `model_tier` or `model` must be set — they are mutually exclusive. "
            "If unsure, set model_tier='standard'."
        ),
    )
    model: ModelName | None = Field(
        default=None,
        description=(
            "A concrete model alias registered on the gateway (e.g. 'claude-sonnet-4-6'). "
            "Only set this when the task requires one specific model; a pinned alias can stop working "
            "if that model is later retired. Prefer `model_tier` instead for stability. "
            "Exactly one of `model` or `model_tier` must be set — they are mutually exclusive."
        ),
    )
    system_prompt: str = Field(
        max_length=500,
        description=("System prompt describing the task for the agent."),
    )
    mcp_tools: list[str] | None = Field(
        default=None,
        max_length=3,
        description=(
            "List of MCP tool names that the agent is allowed to call. Leave empty if the task requires no tools. Call the console_grep_mcp_tools API to get available tools and their input schemas."
        ),
    )
    # Extended thinking configuration (only supported for Claude Sonnet and Gemini models)
    enable_thinking: bool | None = None
    thinking_level: ThinkingLevel | None = None

    @model_validator(mode="after")
    def _validate_model_or_tier(self) -> "AutomatedSubAgentConfig":
        if self.model is not None and self.model_tier is not None:
            raise ValueError("set either model or model_tier, not both")
        if self.model is None and self.model_tier is None:
            raise ValueError("one of model or model_tier is required")
        return self


class ScheduledJobCreate(BaseModel):
    """Request body for creating a new scheduled job."""

    sub_agent_id: int | None = Field(
        default=None,
        description=(
            "ID of an existing sub-agent to execute. Alternatively a custom automated "
            "sub-agent can be provided through sub_agent_parameters, for either job type. "
            "Required for job_type='task'; optional for 'watch', where it runs when the "
            "condition is met and its reply replaces the notification."
        ),
    )
    sub_agent_parameters: AutomatedSubAgentConfig | None = Field(
        default=None,
        description=(
            "Optional custom automated sub-agent configuration to execute for a task job. "
            "If provided, this will be used instead of the referenced sub-agent template. "
            "Ignored for watch jobs."
        ),
    )
    name: str = Field(min_length=5, max_length=200)
    job_type: JobType

    # Schedule — exactly one of these groups must be populated (validated below)
    schedule_kind: ScheduleKind
    cron_expr: str | None = Field(
        default=None,
        description=(
            "Required when schedule_kind='cron'. Wall-clock fields are interpreted in the job's "
            "timezone, so '0 8 * * *' means 8am in the user's local time — do not convert to UTC."
        ),
    )
    timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone (e.g. 'Europe/Zurich') in which cron_expr and timezone-naive run_at "
            "values are interpreted. Defaults to the timezone from the user's settings."
        ),
    )
    interval_seconds: int | None = Field(
        default=None, ge=60, description="Required when schedule_kind='interval'. Min 60s."
    )
    run_at: datetime | None = Field(default=None, description="Required when schedule_kind='once'")

    prompt: str = Field(
        default="",
        max_length=4000,
        description="Instruction/prompt for the agent to execute. For watch jobs it applies only when a sub_agent_id is set and instructs the sub-agent triggered by the condition. Example: 'Analyze the sales data and create a summary'.",
    )
    notification_message: str = Field(
        default="",
        max_length=4000,
        description="Notification text delivered when watch condition triggers (watch jobs only). If empty, an LLM will generate a message based on the check result.",
    )

    # Watch fields — required when job_type='watch'
    check_tool: str | None = Field(default=None, description="MCP tool name to evaluate the watch condition")
    check_args: dict[str, Any] | None = Field(default=None, description="Arguments for the check tool")
    check_args_exprs: dict[str, str] | None = Field(
        default=None,
        description=(
            "Dynamic arguments: a JSON object mapping argument names to CEL "
            "expressions over `now` (current time in the job's timezone) and `prev` "
            "(the previous check result). Each is evaluated fresh on every run and "
            "its value becomes that argument, merged over check_args (the expression "
            "wins per key). This is how a rolling time window reaches a tool "
            'argument — e.g. {"start_date": "strftime(now - duration(\'168h\'), '
            "'%Y-%m-%d')\"}. Never put a literal date in check_args; it goes stale."
        ),
    )
    cel_expr: str | None = Field(
        default=None,
        description=(
            "CEL (Common Expression Language) condition over `result` (the tool "
            "response), `now` (current time in the job's timezone) and `prev` (the "
            "previous check result, null on the first run). Does date math, boolean "
            "logic and filtering deterministically. A boolean result gates the trigger "
            "directly; any other result triggers when non-empty — return the matching "
            "items themselves, since what the expression returns is recorded on the "
            "run and handed to the model or agent. jsonpath(result, \"$.a[*].b\") and "
            "eq_ci(a, b) are available for JSONPath extraction and case-insensitive "
            "comparison. Composes with llm_condition: the CEL gate runs first and the "
            "model judges only what passed it. A watch needs cel_expr, llm_condition, "
            "or both."
        ),
    )
    llm_condition: str | None = Field(
        default=None,
        description=(
            "Natural language condition judged by a model on each run. Use only for "
            "genuinely semantic judgement (tone, intent, 'looks external') — never for "
            "arithmetic, counting, time windows or string matching, which belong in "
            "cel_expr. Composes with cel_expr: the CEL gate runs first, and the model "
            "judges what the expression returned."
        ),
    )
    destroy_after_trigger: bool = Field(
        default=True,
        description="If True (default), the watch job will be disabled after the condition is met once. If False, the watch continues indefinitely.",
    )

    # Delivery — optional: the registered delivery channel to push notifications to
    delivery_channel_id: int | None = Field(
        default=None,
        description="ID of a registered delivery channel.  The channel must be visible to the user.",
    )

    # Voice call — when True, the scheduler dispatches via the voice-agent
    voice_call: bool = Field(
        default=False,
        description="When enabled, the job is dispatched as a phone call via the voice-agent.",
    )

    max_failures: int = Field(default=3, ge=1, le=20)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        return _validate_timezone_name(v)

    @field_validator("cel_expr")
    @classmethod
    def validate_cel_expr(cls, v: str | None) -> str | None:
        """Reject a CEL expression that cannot compile.

        Stored unchecked, it yields a job that looks configured and never fires.
        Compiling needs no payload, so it belongs here.
        """
        try:
            validate_cel_expression(v)
        except CelSyntaxError as exc:
            raise ValueError(f"{exc}. {CEL_SYNTAX_HINT}") from exc
        return v

    @field_validator("check_args_exprs")
    @classmethod
    def validate_check_args_exprs(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        """Reject a dynamic-argument expression that cannot compile — per entry,
        naming the argument, so the error points at the field that caused it."""
        for key, expr in (v or {}).items():
            try:
                validate_cel_expression(expr)
            except CelSyntaxError as exc:
                raise ValueError(f"argument {key!r}: {exc}. {CEL_SYNTAX_HINT}") from exc
        return v

    @field_validator("check_args", mode="before")
    @classmethod
    def parse_check_args(cls, v: Any) -> dict[str, Any] | None:
        """Parse check_args from JSON string if needed.

        LLMs sometimes provide check_args as a JSON string instead of an object.
        This validator accepts both formats for better UX.
        """
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if not isinstance(parsed, dict):
                    raise ValueError(f"check_args must be a JSON object, got {type(parsed).__name__}")
                return parsed
            except json.JSONDecodeError as e:
                raise ValueError(f"check_args is not valid JSON: {e}")
        raise ValueError(f"check_args must be a dict or JSON string, got {type(v).__name__}")

    @model_validator(mode="after")
    def validate_consistency(self) -> "ScheduledJobCreate":
        # tasks require an agent
        if self.job_type == JobType.TASK and self.sub_agent_id is None and self.sub_agent_parameters is None:
            raise ValueError("sub_agent_id or sub_agent_parameters is required for job_type='task'")

        # Watches require something to call and something to decide with: a watch
        # with neither a CEL gate nor a judged condition can never fire, and would sit
        # polling forever while looking configured.
        if self.job_type == JobType.WATCH:
            if not self.check_tool:
                raise ValueError("Watch jobs require: check_tool")
            if not self.cel_expr and not self.llm_condition:
                raise ValueError(
                    "Watch jobs require a condition: cel_expr (deterministic), "
                    "llm_condition (judged by a model), or both (gate then judge)"
                )

        # schedule kind config
        if self.schedule_kind == ScheduleKind.CRON and not self.cron_expr:
            raise ValueError("cron_expr is required for schedule_kind='cron'")
        if self.schedule_kind == ScheduleKind.INTERVAL and self.interval_seconds is None:
            raise ValueError("interval_seconds is required for schedule_kind='interval'")
        if self.schedule_kind == ScheduleKind.ONCE and self.run_at is None:
            raise ValueError("run_at is required for schedule_kind='once'")

        return self


class GenerateJobDraftRequest(BaseModel):
    """Request body for generating a scheduled job from a one-line description."""

    tools: list[dict[str, Any]] = Field(
        description="List of available MCP tool objects (name, description, input_schema.)"
    )
    query: str = Field(
        min_length=1,
        max_length=500,
        description="Natural-language description of what the job should do.",
    )


def _draft_field(info: FieldInfo) -> tuple[Any, FieldInfo]:
    """Relax one ScheduledJobCreate field into a draft field.

    Optional, defaulting to None, and stripped of constraints: a draft is what a model
    proposed, so a too-long name or an empty string has to survive being returned and
    shown rather than failing the whole generation. The real constraints apply when the
    job is actually created through ScheduledJobCreate.
    """
    annotation = Any if info.annotation is None else info.annotation | None
    return annotation, FieldInfo(default=None, description=info.description)


#: A partial scheduled job — every field of ScheduledJobCreate, all optional.
#:
#: Derived from ScheduledJobCreate rather than restated so the two cannot drift: a new
#: job field becomes generatable without touching this, and a field cannot be misspelled
#: here into something the create endpoint silently ignores. Nothing is required because
#: a generated draft is allowed to be incomplete — a fabricated schedule would be worse
#: than an empty one — and the consistency rules live on ScheduledJobCreate, which is
#: what the draft is eventually submitted as.
ScheduledJobDraft = create_model(  # type: ignore[call-overload]
    "ScheduledJobDraft",
    __doc__=(
        "A partial scheduled job: every ScheduledJobCreate field, all optional. Fields "
        "the generator could not infer are omitted for the caller to fill in."
    ),
    **{name: _draft_field(info) for name, info in ScheduledJobCreate.model_fields.items()},
)

class GenerateConditionRequest(BaseModel):
    """Ask a model to write (or refine) just the condition of a watch job.

    Narrower than generate-job-draft on purpose: complex CEL is the hard part of
    authoring a watch, and it is best written against the real response shape — which
    the caller has (from "Run check now" or the last stored run) and the draft
    generator does not.
    """

    query: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "What the condition should do, in natural language — either a fresh "
            "description or a refinement of current_cel_expr ('also exclude declined "
            "attendees')."
        ),
    )
    current_cel_expr: str | None = Field(
        default=None,
        description="The expression as it stands, when refining rather than starting fresh.",
    )
    current_llm_condition: str | None = Field(
        default=None,
        description="The judged condition as it stands, so a refinement can keep or adjust the split.",
    )
    result: Any = Field(
        default=None,
        description=(
            "A real tool response to write against and verify with. Without one the "
            "expression is only compile-checked, not evaluated."
        ),
    )
    check_tool: str | None = Field(
        default=None,
        description="Name of the check tool, for context only.",
    )


class GenerateConditionResponse(BaseModel):
    """A generated condition, verified as far as the given material allows."""

    cel_expr: str | None = Field(
        default=None,
        description="The expression. None when the ask is purely semantic (judge-only).",
    )
    llm_condition: str | None = Field(
        default=None,
        description="The semantic stage, when the ask has one; judged on what cel_expr returns.",
    )
    #: What "verified" means depends on what was provided: with a payload, the
    #: expression compiled AND evaluated against it; without one, it only compiled.
    verified: bool = False
    #: The evaluation against the supplied payload, so the caller can show the result
    #: without a second round-trip: {gate, extracted}. None when no payload was given.
    evaluation: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)


class ValidateArgsExprRequest(BaseModel):
    """A dynamic-arguments expression, to resolve without running the job."""

    check_args_exprs: dict[str, str] = Field(
        min_length=1,
        description="Argument name to CEL expression over `now` and `prev`.",
    )
    check_args: dict[str, Any] | None = Field(
        default=None,
        description="The static arguments, so the response shows the merged result.",
    )
    prev: Any = Field(
        default=None,
        description="The previous check result, for expressions that use `prev`.",
    )


class ValidateArgsExprResponse(BaseModel):
    """What the arguments would be if the check ran right now."""

    valid: bool = Field(description="False when the expression could not be compiled or evaluated.")
    error: str | None = None
    #: The merged arguments the tool would be called with — static plus resolved,
    #: expression winning per key. What "Run check now" and the scheduler both use.
    resolved: dict[str, Any] | None = None


class ValidateConditionRequest(BaseModel):
    """A condition plus the payload to try it against."""

    result: Any = Field(
        description=(
            "The tool response to evaluate against — either one returned by "
            "/mcp/tools/invoke or a payload the author pasted in."
        ),
    )
    cel_expr: str | None = Field(
        default=None,
        description=(
            "CEL condition over `result`, `now` and `prev`. `now` is the server's "
            "current time; pass `prev` to try change-detection conditions."
        ),
    )
    prev: Any = Field(
        default=None,
        description="The previous check result, for trying CEL conditions that use `prev`.",
    )
    llm_condition: str | None = Field(
        default=None,
        description=(
            "A judged condition is not judged here — that needs a model call — but its "
            "presence changes what the preview reports: with cel_expr, the gate is "
            "still evaluated, and a false gate means the model would never be asked."
        ),
    )


class ValidateConditionResponse(BaseModel):
    """What a condition does to a given payload."""

    valid: bool = Field(description="False when the expression could not be parsed.")
    error: str | None = None
    extracted: Any = None
    condition_met: bool | None = Field(
        default=None,
        description="None when not evaluated: invalid expression, or judged by a model.",
    )
    notes: list[str] = Field(default_factory=list)


class ScheduledJobUpdate(BaseModel):
    """Request body for updating an existing scheduled job. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule_kind: ScheduleKind | None = None
    cron_expr: str | None = None
    timezone: str | None = Field(
        default=None,
        description="IANA timezone in which cron_expr and timezone-naive run_at values are interpreted.",
    )
    interval_seconds: int | None = Field(default=None, ge=60)
    run_at: datetime | None = None
    prompt: str | None = Field(default=None, max_length=4000)
    notification_message: str | None = Field(default=None, max_length=4000)
    sub_agent_id: int | None = None
    check_tool: str | None = None
    check_args: dict[str, Any] | None = None
    check_args_exprs: dict[str, str] | None = Field(
        default=None,
        description=(
            "Dynamic arguments: argument name to CEL expression over `now`/`prev`, "
            "each resolved on every run and merged over check_args. Null clears them."
        ),
    )
    cel_expr: str | None = Field(
        default=None,
        description=(
            "CEL condition over `result`, `now` and `prev`. Null clears it, leaving "
            "llm_condition as the whole condition — a watch must keep at least one."
        ),
    )
    llm_condition: str | None = None
    destroy_after_trigger: bool | None = None
    delivery_channel_id: int | None = None
    voice_call: bool | None = None
    enabled: bool | None = None
    max_failures: int | None = Field(default=None, ge=1, le=20)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        return _validate_timezone_name(v)

    @field_validator("cel_expr")
    @classmethod
    def validate_cel_expr(cls, v: str | None) -> str | None:
        """Reject a CEL expression that cannot compile — same rule as create."""
        try:
            validate_cel_expression(v)
        except CelSyntaxError as exc:
            raise ValueError(f"{exc}. {CEL_SYNTAX_HINT}") from exc
        return v

    @field_validator("check_args_exprs")
    @classmethod
    def validate_check_args_exprs(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        """Reject a dynamic-argument expression that cannot compile — same rule as create."""
        for key, expr in (v or {}).items():
            try:
                validate_cel_expression(expr)
            except CelSyntaxError as exc:
                raise ValueError(f"argument {key!r}: {exc}. {CEL_SYNTAX_HINT}") from exc
        return v
