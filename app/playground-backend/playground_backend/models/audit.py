"""Pydantic models for audit logs."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .user import PaginationMeta


class AuditEntityType(str, Enum):
    """Audit entity type enum."""

    USER = "user"
    GROUP = "group"
    SUB_AGENT = "sub_agent"
    SESSION = "session"  # For session-related events like admin mode activation
    SECRET = "secret"  # For secrets management operations


class AuditAction(str, Enum):
    """Audit action enum."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    APPROVE = "approve"
    REJECT = "reject"
    ASSIGN = "assign"
    UNASSIGN = "unassign"
    ADMIN_MODE_ACTIVATED = "admin_mode_activated"
    SUBMIT_FOR_APPROVAL = "submit_for_approval"
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"
    SET_DEFAULT = "set_default"
    REVERT = "revert"
    PERMISSION_UPDATE = "permission_update"


class AuditLog(BaseModel):
    """Audit log entry."""

    id: int
    actor_sub: str
    entity_type: AuditEntityType
    entity_id: str
    action: AuditAction
    changes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class AuditLogListResponse(BaseModel):
    """Paginated audit log list response."""

    data: list[AuditLog]
    meta: PaginationMeta
