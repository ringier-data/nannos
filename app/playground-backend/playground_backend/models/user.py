"""User model."""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class OrchestratorThinkingLevel(str, Enum):
    """Thinking depth level for extended thinking mode."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class UserStatus(str, Enum):
    """User status enum."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class UserRole(str, Enum):
    """User role enum defining system-wide capabilities.

    - member: Baseline user, can view groups they're in
    - approver: Can approve sub-agents where they have write group access
    - admin: Full system administration (create groups, manage users)
    """

    MEMBER = "member"
    APPROVER = "approver"
    ADMIN = "admin"


class User(BaseModel):
    """User model for PostgreSQL storage."""

    id: str  # Primary key (UUID for new users, original sub for existing users - stable)
    sub: str  # OIDC subject identifier (current - can change with IDP)
    email: str
    first_name: str
    last_name: str
    company_name: str | None = None
    is_administrator: bool = False
    role: UserRole = UserRole.MEMBER
    status: UserStatus = UserStatus.ACTIVE
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class UserGroupMembership(BaseModel):
    """User's membership in a group."""

    group_id: int
    group_name: str
    group_role: Literal["read", "write", "manager"]


class UserWithGroups(User):
    """User with group memberships."""

    groups: list[UserGroupMembership] = Field(default_factory=list)


# Request/Response models for API


class PaginationMeta(BaseModel):
    """Pagination metadata for list responses."""

    page: int
    limit: int
    total: int


class UserListResponse(BaseModel):
    """Paginated user list response."""

    data: list[UserWithGroups]
    meta: PaginationMeta


class UserDetailResponse(BaseModel):
    """Single user detail response."""

    data: UserWithGroups


class UserStatusUpdate(BaseModel):
    """Request to update user status."""

    status: UserStatus


class UserGroupsUpdate(BaseModel):
    """Request to update user's group memberships."""

    group_ids: list[int]
    operation: Literal["set", "add", "remove"]
    role: Literal["read", "write", "manager"] = "read"


class UserRoleUpdate(BaseModel):
    """Request to update user's role."""

    role: UserRole


class UserGroupRoleUpdate(BaseModel):
    """Request to update user's role in a group."""

    role: Literal["read", "write", "manager"]


class BulkUserOperation(BaseModel):
    """Single operation in a bulk user update."""

    user_id: str
    action: Literal["suspend", "activate", "delete"]


class BulkUserOperationRequest(BaseModel):
    """Request to perform bulk user operations."""

    operations: list[BulkUserOperation]


class BulkOperationResult(BaseModel):
    """Result of a single bulk operation."""

    user_id: str
    success: bool
    error: str | None = None


class BulkUserOperationResponse(BaseModel):
    """Response for bulk user operations."""

    data: list[BulkOperationResult]


# User Settings models


class UserSettings(BaseModel):
    """User settings model for user-editable preferences."""

    user_id: str
    language: str = "en"
    timezone: str = "Europe/Zurich"
    custom_prompt: str | None = None
    mcp_tools: list[str] = Field(default_factory=list)
    preferred_model: str | None = None
    enable_thinking: bool | None = None
    thinking_level: OrchestratorThinkingLevel | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class UserSettingsUpdate(BaseModel):
    """Request to update user settings (partial update).

    Uses model_fields_set to distinguish:
    - Field not provided in request (not in model_fields_set, keeps current value)
    - Field explicitly set to None (in model_fields_set, clears the value)
    """

    language: str | None = None
    timezone: str | None = None
    custom_prompt: str | None = None
    mcp_tools: list[str] | None = None
    preferred_model: str | None = None
    enable_thinking: bool | None = None
    thinking_level: OrchestratorThinkingLevel | None = None


class UserSettingsResponse(BaseModel):
    """User settings response."""

    data: UserSettings


class UserAdminUpdate(BaseModel):
    """Request for admin to update user fields."""

    is_administrator: bool | None = None


class ImpersonateStartRequest(BaseModel):
    """Request model for starting user impersonation."""

    target_user_id: str
