"""Pydantic models for bug reports."""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from .user import PaginationMeta


class BugReportStatus(str, Enum):
    """Bug report status enum."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"


class BugReportSource(str, Enum):
    """Bug report source enum."""

    ORCHESTRATOR = "orchestrator"
    CLIENT = "client"


class BugReportCreate(BaseModel):
    """Request model for creating a bug report."""

    conversation_id: str
    message_id: str | None = None
    task_id: str | None = None
    description: str | None = None
    source: BugReportSource = BugReportSource.CLIENT


class BugReportStatusUpdate(BaseModel):
    """Request model for updating bug report status."""

    status: BugReportStatus


class BugReportResponse(BaseModel):
    """Response model for a single bug report."""

    id: str
    conversation_id: str
    message_id: str | None = None
    task_id: str | None = None
    user_id: str
    source: BugReportSource
    description: str | None = None
    status: BugReportStatus
    external_link: str | None = None
    debug_conversation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class BugReportListResponse(BaseModel):
    """Paginated bug report list response."""

    data: list[BugReportResponse]
    meta: PaginationMeta
