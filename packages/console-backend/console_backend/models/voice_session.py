"""Voice session model for inbound call tracking and Gemini session resumption."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


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
