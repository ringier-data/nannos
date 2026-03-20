"""Pydantic models for the scheduler — scheduled jobs, runs, and delivery config."""

import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .sub_agent import ModelName, ThinkingLevel


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
    CONDITION_NOT_MET = "condition_not_met"  # Watch check passed but JSONPath condition was false


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
    interval_seconds: int | None = None
    run_at: datetime | None = None
    next_run_at: datetime
    last_run_at: datetime | None = None
    prompt: str | None = None
    notification_message: str | None = None
    # Watch fields
    check_tool: str | None = None
    check_args: dict[str, Any] | None = None
    condition_expr: str | None = None
    expected_value: str | None = None
    llm_condition: str | None = None
    destroy_after_trigger: bool = True
    last_check_result: dict[str, Any] | None = None
    # Delivery — references a registered delivery channel
    delivery_channel_id: int | None = None
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
    model: ModelName
    system_prompt: str = Field(
        max_length=500,
        description=("System prompt describing the task for the agent."),
    )
    mcp_tools: list[str] | None = Field(
        default=None,
        max_length=3,
        description=(
            "List of MCP tool names that the agent is allowed to call. Leave empty if the task requires no tools. Call the playground_grep_mcp_tools API to get available tools and their input schemas."
        ),
    )
    # Extended thinking configuration (only supported for Claude Sonnet and Gemini models)
    enable_thinking: bool | None = None
    thinking_level: ThinkingLevel | None = None


class ScheduledJobCreate(BaseModel):
    """Request body for creating a new scheduled job."""

    sub_agent_id: int | None = Field(
        default=None,
        description="ID of an existing sub-agent to execute. Alternatively a custom automated sub-agent can be provided through sub_agent_parameters. Required for job_type='task'; optional for 'watch'.",
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
    cron_expr: str | None = Field(default=None, description="Required when schedule_kind='cron'")
    interval_seconds: int | None = Field(
        default=None, ge=60, description="Required when schedule_kind='interval'. Min 60s."
    )
    run_at: datetime | None = Field(default=None, description="Required when schedule_kind='once'")

    prompt: str = Field(
        default="",
        max_length=4000,
        description="Instruction/prompt for the agent to execute (task jobs only). Example: 'Analyze the sales data and create a summary'.",
    )
    notification_message: str = Field(
        default="",
        max_length=4000,
        description="Notification text delivered when watch condition triggers (watch jobs only). If empty, an LLM will generate a message based on the check result.",
    )

    # Watch fields — required when job_type='watch'
    check_tool: str | None = Field(default=None, description="MCP tool name to evaluate the watch condition")
    check_args: dict[str, Any] | None = Field(default=None, description="Arguments for the check tool")
    condition_expr: str | None = Field(
        default=None,
        description="JSONPath expression to extract a value from the tool response.",
    )
    expected_value: str | None = Field(
        default=None,
        description="Expected value to compare against the JSONPath result. If null, checks that result is not null. Otherwise performs exact string comparison.",
    )
    llm_condition: str | None = Field(
        default=None,
        description="Natural language condition for LLM-based evaluation. Use when exact matching is not suitable. Example: 'The status indicates success or completion'.",
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

    max_failures: int = Field(default=3, ge=1, le=20)

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

        # watches require condition fields
        if self.job_type == JobType.WATCH:
            missing = [f for f in ("check_tool", "condition_expr") if not getattr(self, f)]
            if missing:
                raise ValueError(f"Watch jobs require: {', '.join(missing)}")

        # schedule kind config
        if self.schedule_kind == ScheduleKind.CRON and not self.cron_expr:
            raise ValueError("cron_expr is required for schedule_kind='cron'")
        if self.schedule_kind == ScheduleKind.INTERVAL and self.interval_seconds is None:
            raise ValueError("interval_seconds is required for schedule_kind='interval'")
        if self.schedule_kind == ScheduleKind.ONCE and self.run_at is None:
            raise ValueError("run_at is required for schedule_kind='once'")

        return self


class GenerateWatchParamsRequest(BaseModel):
    """Request body for AI-assisted watch parameter generation."""

    tools: list[dict[str, Any]] = Field(
        description="List of available MCP tool objects (name, description, input_schema.)"
    )
    query: str = Field(
        min_length=1,
        max_length=500,
        description="Natural-language description of the condition to watch for.",
    )


class GenerateWatchParamsResponse(BaseModel):
    """AI-generated tool selection, arguments, condition expression and notification text for a watch job."""

    check_tool: str | None = None
    check_args: dict[str, Any] | None = None
    condition_expr: str | None = None
    expected_value: str | None = None
    llm_condition: str | None = None
    notification_message: str | None = None


class ScheduledJobUpdate(BaseModel):
    """Request body for updating an existing scheduled job. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule_kind: ScheduleKind | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    run_at: datetime | None = None
    prompt: str | None = Field(default=None, max_length=4000)
    notification_message: str | None = Field(default=None, max_length=4000)
    sub_agent_id: int | None = None
    check_tool: str | None = None
    check_args: dict[str, Any] | None = None
    condition_expr: str | None = None
    expected_value: str | None = None
    llm_condition: str | None = None
    destroy_after_trigger: bool | None = None
    delivery_channel_id: int | None = None
    enabled: bool | None = None
    max_failures: int | None = Field(default=None, ge=1, le=20)
