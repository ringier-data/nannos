"""Voice session model for inbound call tracking and Gemini session resumption."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from .usage import UsageLogCreate


class VoiceSessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class VoiceSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    sub_agent_id: int | None = None
    phone_number: str
    call_sid: str | None = None
    gemini_session_handle: str | None = None
    status: VoiceSessionStatus = VoiceSessionStatus.ACTIVE
    use_session_memory: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VoiceSessionCreate(BaseModel):
    user_id: str
    sub_agent_id: int | None = None
    phone_number: str
    call_sid: str | None = None
    use_session_memory: bool = False


class VoiceSessionHandleUpdate(BaseModel):
    gemini_session_handle: str


class VoiceSessionResponse(BaseModel):
    data: VoiceSession


class VoiceUsageEntry(BaseModel):
    """One model's measured consumption during a voice call.

    Inherits `billing_unit_breakdown`'s validation (snake_case names, no zeros, no
    reserved names) from the usage models rather than re-implementing it. Carries no
    attribution: the endpoint derives user + sub-agent from the voice session record,
    so the voice agent cannot mis-attribute spend.
    """

    provider: str = Field(..., description="Provider family, e.g. 'vertex_ai'")
    model_name: str = Field(..., description="Model the tokens were spent on")
    billing_unit_breakdown: dict[str, int] = Field(
        ...,
        description="Mapping of billing_unit to count (only non-zero values)",
        examples=[{"audio_input_tokens": 1234, "audio_output_tokens": 5678}],
    )

    _validate_breakdown = field_validator("billing_unit_breakdown")(
        UsageLogCreate.validate_billing_unit_breakdown.__func__
    )


class VoiceUsageReport(BaseModel):
    """Batch of per-model usage for a single voice session."""

    entries: list[VoiceUsageEntry] = Field(..., max_length=20)
