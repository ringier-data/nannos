"""Pydantic models for user groups."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .sub_agent import SubAgentStatus
from .user import PaginationMeta


class UserGroup(BaseModel):
    """User group model."""

    id: int
    name: str
    description: str | None = None
    keycloak_group_id: str | None = None  # Keycloak group ID for one-way sync
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class UserGroupMember(BaseModel):
    """User group membership model."""

    id: int
    user_id: str
    user_group_id: int
    group_role: Literal["read", "write", "manager"] = "read"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class MemberInfo(BaseModel):
    """Member info for group detail response."""

    user_id: str
    email: str
    first_name: str
    last_name: str
    group_role: Literal["read", "write", "manager"]


class UserGroupWithMembers(UserGroup):
    """User group with member list."""

    member_count: int = 0
    members: list[MemberInfo] = Field(default_factory=list)


# Request models


class UserGroupCreate(BaseModel):
    """Request to create a user group."""

    name: str
    description: str | None = None


class UserGroupUpdate(BaseModel):
    """Request to update a user group."""

    name: str | None = None
    description: str | None = None


class GroupMemberAdd(BaseModel):
    """Request to add members to a group."""

    user_ids: list[str]
    role: Literal["read", "write", "manager"] = "read"


class GroupMemberUpdate(BaseModel):
    """Request to update a member's role."""

    role: Literal["read", "write", "manager"]


class GroupMemberRemove(BaseModel):
    """Request to remove members from a group (bulk operation)."""

    user_ids: list[str]


class BulkGroupDelete(BaseModel):
    """Request to delete multiple groups."""

    group_ids: list[int]
    force: bool = False


# Response models


class UserGroupListResponse(BaseModel):
    """Paginated user group list response."""

    data: list[UserGroupWithMembers]
    meta: PaginationMeta


class UserGroupDetailResponse(BaseModel):
    """Single user group detail response."""

    data: UserGroupWithMembers


class GroupMemberListResponse(BaseModel):
    """Paginated group member list response."""

    data: list[MemberInfo]
    meta: PaginationMeta


class BulkDeleteResult(BaseModel):
    """Result of a single group deletion."""

    group_id: int
    success: bool
    error: str | None = None


class BulkGroupDeleteResponse(BaseModel):
    """Response for bulk group deletion."""

    data: list[BulkDeleteResult]


class SubAgentRef(BaseModel):
    """Basic reference to a sub-agent."""

    id: int
    name: str


class SubAgentRefWithStatus(SubAgentRef):
    """Sub-agent reference with status indicators for UI."""

    approval_status: SubAgentStatus  # draft, pending_approval, approved, rejected
    is_activated: bool  # Whether currently activated for the user
    activated_by_groups: list[int] | None = None  # Which groups activated it
    is_default: bool = False  # Whether this agent is a default for the group


class SubAgentAdd(BaseModel):
    """Request to add default sub-agents to a group."""

    sub_agent_ids: list[int]
