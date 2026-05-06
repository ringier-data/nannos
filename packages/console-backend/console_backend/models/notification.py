"""Notification models for in-app notification inbox."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class NotificationType(str, Enum):
    """Notification type enum matching database enum."""

    AGENT_ACTIVATED = "agent_activated"
    AGENT_DEACTIVATED = "agent_deactivated"
    AGENT_PERMISSION_CHANGED = "agent_permission_changed"
    GROUP_ADDED = "group_added"
    GROUP_REMOVED = "group_removed"
    ROLE_UPDATED = "role_updated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_COMPLETED = "approval_completed"
    APPROVAL_REJECTED = "approval_rejected"
    AGENT_SHARED = "agent_shared"
    AGENT_ACCESS_REVOKED = "agent_access_revoked"
    SECRET_SHARED = "secret_shared"
    SECRET_ACCESS_REVOKED = "secret_access_revoked"
    SECRET_PERMISSION_CHANGED = "secret_permission_changed"
    SYSTEM_ANNOUNCEMENT = "system_announcement"


class ActivationSource(str, Enum):
    """Activation source enum matching database enum."""

    USER = "user"
    GROUP = "group"
    ADMIN = "admin"


class UserNotification(BaseModel):
    """User notification model."""

    id: int
    user_id: str
    type: NotificationType
    title: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: v.isoformat()}


class NotificationCreate(BaseModel):
    """Request model for creating a notification."""

    type: NotificationType
    title: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class NotificationData(BaseModel):
    """Data model for bulk notification creation."""

    user_id: str
    notification_type: NotificationType
    title: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class NotificationListResponse(BaseModel):
    """Response model for listing notifications."""

    items: list[UserNotification]
    total: int
    unread_count: int


class NotificationMarkReadRequest(BaseModel):
    """Request model for marking notifications as read."""

    notification_ids: list[int]


class UnreadCountResponse(BaseModel):
    """Response model for unread notification count."""

    count: int
