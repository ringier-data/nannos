"""Pydantic models for message feedback."""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class FeedbackRating(str, Enum):
    """Feedback rating enum."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


class MessageFeedbackCreate(BaseModel):
    """Request model for submitting message feedback."""

    rating: FeedbackRating
    comment: str | None = None
    sub_agent_id: str | None = None
    task_id: str | None = None


class MessageFeedbackResponse(BaseModel):
    """Response model for a single message feedback entry."""

    id: str
    conversation_id: str
    message_id: str
    user_id: str
    rating: FeedbackRating
    comment: str | None = None
    sub_agent_id: str | None = None
    task_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True
