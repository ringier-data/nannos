"""Pydantic models for LLM usage tracking and rate cards."""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator

# Billing unit validation
BILLING_UNIT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")
RESERVED_BILLING_UNITS = {"id", "cost", "total", "timestamp", "count"}

# Rate Card Models


class RateCardEntry(BaseModel):
    """Rate card entry for a specific billing unit."""

    id: int
    provider: str
    model_name: str
    model_name_pattern: str | None = Field(None, description="Optional regex pattern for matching model variants")
    billing_unit: str
    flow_direction: Literal["input", "output", "other"]
    price_per_million: Decimal
    effective_from: datetime
    effective_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RateCardEntryCreate(BaseModel):
    """Create a new rate card entry."""

    provider: str = Field(..., description="Provider name (e.g., 'bedrock', 'openai', 'google')")
    model_name: str = Field(..., description="Model name (e.g., 'claude-sonnet-4.5', 'gpt-4o')")
    billing_unit: str = Field(..., description="Billing unit (e.g., 'input_tokens', 'cache_read', 'requests')")
    flow_direction: Literal["input", "output", "other"] = Field(
        ..., description="Whether this billing unit is input-related, output-related, or other"
    )
    price_per_million: Decimal = Field(..., gt=0, description="Price per million units in USD")
    effective_from: AwareDatetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="When this rate becomes effective"
    )

    @field_validator("price_per_million")
    @classmethod
    def validate_price(cls, v: Decimal) -> Decimal:
        """Ensure price has reasonable precision."""
        if v < 0:
            raise ValueError("Price must be non-negative")
        return v


class RateCardEntryUpdate(BaseModel):
    """Update a rate card entry (creates new version with new effective_from)."""

    price_per_million: Decimal = Field(..., gt=0, description="New price per million units")
    effective_from: datetime = Field(..., description="When this new rate becomes effective")


class RateCardPricingEntry(BaseModel):
    """Pricing entry for a specific billing unit."""

    price_per_million: Decimal = Field(..., gt=0, description="Price per million units in USD")
    flow_direction: Literal["input", "output", "other"] = Field(
        ..., description="Whether this billing unit is input-related, output-related, or other"
    )


class RateCardModelCreate(BaseModel):
    """Create all rate card entries for a new model at once."""

    provider: str
    model_name: str
    model_name_pattern: str | None = Field(
        None,
        description="Optional regex pattern for matching model variants (e.g., '^gpt-4o-mini(-\\d{4}-\\d{2}-\\d{2})?$')",
    )
    effective_from: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pricing: dict[str, RateCardPricingEntry] = Field(
        ...,
        description="Mapping of billing_unit to pricing details (price and flow_direction)",
        examples=[
            {
                "input_tokens": {"price_per_million": Decimal("3.00"), "flow_direction": "input"},
                "output_tokens": {"price_per_million": Decimal("15.00"), "flow_direction": "output"},
                "cache_read": {"price_per_million": Decimal("0.30"), "flow_direction": "output"},
            }
        ],
    )


class RateCardEntriesList(BaseModel):
    """List of rate card entries with pagination."""

    entries: list[RateCardEntry]
    total: int
    page: int
    limit: int


# Usage Models


class BillingUnitDetail(BaseModel):
    """Billing unit detail for a specific usage entry."""

    billing_unit: str
    unit_count: int = Field(..., gt=0, description="Unit count (only non-zero values stored)")


class UsageLog(BaseModel):
    """Usage log entry for agent invocations (LLM and non-LLM)."""

    id: int
    user_id: str
    conversation_id: str | None = None
    sub_agent_id: int | None = None
    sub_agent_name: str | None = None
    sub_agent_config_version_id: int | None = None
    scheduled_job_id: int | None = None
    scheduled_job_name: str | None = None
    catalog_id: str | None = None
    catalog_name: str | None = None
    provider: str | None = None
    model_name: str | None = None
    total_cost_usd: Decimal
    langsmith_run_id: str | None = None
    langsmith_trace_id: str | None = None
    invoked_at: datetime
    logged_at: datetime

    # Billing unit details populated via JOIN
    billing_unit_details: list[BillingUnitDetail] = Field(default_factory=list)


class UsageLogCreate(BaseModel):
    """Create a new usage log."""

    conversation_id: str | None = None
    sub_agent_id: int | None = None
    sub_agent_config_version_id: int | None = None
    scheduled_job_id: int | None = None
    catalog_id: str | None = None
    provider: str | None = None
    model_name: str | None = None
    billing_unit_breakdown: dict[str, int] = Field(
        ...,
        description="Mapping of billing_unit to count (only non-zero values)",
        examples=[{"input_tokens": 1234, "output_tokens": 567, "requests": 1}],
    )
    langsmith_run_id: str | None = None
    langsmith_trace_id: str | None = None
    invoked_at: datetime

    @field_validator("billing_unit_breakdown")
    @classmethod
    def validate_billing_unit_breakdown(cls, v: dict[str, int]) -> dict[str, int]:
        """Ensure all unit counts are positive and billing unit names are valid."""
        for billing_unit, count in v.items():
            if count <= 0:
                raise ValueError(f"Unit count for {billing_unit} must be positive (don't send zeros)")

            # Validate billing unit name format
            if not BILLING_UNIT_PATTERN.match(billing_unit):
                raise ValueError(
                    f"Invalid billing unit name '{billing_unit}'. "
                    "Use snake_case (lowercase letters, numbers, underscores), "
                    "starting and ending with alphanumeric characters. "
                    "Examples: input_tokens, premium_api_calls, vector_searches"
                )

            # Check against reserved names
            if billing_unit in RESERVED_BILLING_UNITS:
                raise ValueError(
                    f"Billing unit name '{billing_unit}' is reserved. "
                    f"Reserved names: {', '.join(sorted(RESERVED_BILLING_UNITS))}"
                )

            # Check length
            if len(billing_unit) < 3 or len(billing_unit) > 64:
                raise ValueError(f"Billing unit name '{billing_unit}' must be between 3 and 64 characters")

        return v


class UsageLogBatchCreate(BaseModel):
    """Batch create multiple usage logs."""

    logs: list[UsageLogCreate] = Field(..., max_length=100, description="Up to 100 logs per batch")


class UsageSummary(BaseModel):
    """Summary of usage for a user or time period."""

    total_cost_usd: Decimal
    total_requests: int
    providers: dict[str, int] = Field(default_factory=dict, description="Request count by provider")
    models: dict[str, int] = Field(default_factory=dict, description="Request count by model")
    period_start: datetime
    period_end: datetime


class UsageBySubAgent(BaseModel):
    """Usage breakdown by sub-agent."""

    sub_agent_id: int
    sub_agent_name: str
    total_cost_usd: Decimal
    total_requests: int
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class UsageByConversation(BaseModel):
    """Usage breakdown by conversation."""

    conversation_id: str
    total_cost_usd: Decimal
    total_requests: int
    first_message_at: datetime
    last_message_at: datetime


class UsageByService(BaseModel):
    """Usage breakdown by service type (orchestrator, catalog, scheduler)."""

    service: str
    total_cost_usd: Decimal
    total_requests: int


class BillingUnitBreakdown(BaseModel):
    """Usage breakdown by billing unit type."""

    billing_unit: str
    total_count: int
    percentage: float = Field(..., ge=0, le=100, description="Percentage of total units")


class DetailedUsageReport(BaseModel):
    """Detailed usage report with billing unit breakdowns."""

    summary: UsageSummary
    by_sub_agent: list[UsageBySubAgent] = Field(default_factory=list)
    by_conversation: list[UsageByConversation] = Field(default_factory=list)
    by_service: list[UsageByService] = Field(default_factory=list)
    billing_unit_breakdown: list[BillingUnitBreakdown] = Field(default_factory=list)


class UsageLogsList(BaseModel):
    """Paginated list of usage logs."""

    logs: list[UsageLog]
    total: int
    page: int
    limit: int
