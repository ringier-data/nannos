"""User model."""

import json
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..utils.timezones import default_timezone_name, validate_timezone_name


class OrchestratorThinkingLevel(str, Enum):
    """Reasoning effort (LiteLLM convention). Per-model support comes from the gateway."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


#: Prefix of the subject a SCIM-provisioned user carries until their first OIDC login.
#: A real IdP subject never looks like this, which is the entire point: the placeholder
#: says what it is instead of having to be inferred from the shape of the row.
SCIM_PLACEHOLDER_SUB_PREFIX = "scim-pending:"


def placeholder_sub(user_id: str) -> str:
    """The subject to store for a SCIM-provisioned user who has no IdP account yet.

    Keyed on the row's own id so it stays unique under `users.sub`'s unique constraint.
    """
    return f"{SCIM_PLACEHOLDER_SUB_PREFIX}{user_id}"


def has_idp_identity(sub: str) -> bool:
    """True when `sub` is a real IdP subject rather than the SCIM placeholder.

    A user provisioned over SCIM has no account at the identity provider until they log in
    for the first time, so `ScimUserService.create_user` stores `placeholder_sub(id)` and the
    real subject only arrives with the first OIDC login. Anything that hands `users.sub` to
    the IdP — Keycloak group membership above all — must check this first: the placeholder is
    not a Keycloak user id, and using it as one gets `404 User not found`.

    This used to be inferred (`sub == id`, narrowed by `scim_user_name`) rather than written
    down. Inference could not be made sound: migration 001 keyed `users` by the OIDC sub
    itself, so a row from that era legitimately has `sub == id`, and `scim_user_name` can be
    written onto any row by a SCIM PUT/PATCH — including one of those, which silently stopped
    mirroring a user who did have a Keycloak account. Migration 101 rewrote the existing
    placeholders to this prefix so the question is answered by the value itself.
    """
    return not sub.startswith(SCIM_PLACEHOLDER_SUB_PREFIX)


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
    #: True for machine identities — the seeded `system` owner of auto-provisioned agents
    #: and the service accounts auto-onboarded from client-credentials tokens. Nobody reads
    #: their inbox, so audiences selected by standing filter them out.
    is_service_account: bool = False
    role: UserRole = UserRole.MEMBER
    status: UserStatus = UserStatus.ACTIVE
    phone_number_idp: str | None = None
    scim_attributes: dict[str, Any] | None = None
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


class OnboardingIssueKind(str, Enum):
    """What stands between a user and scheduled jobs that run and reach them (#311)."""

    #: Provisioned over SCIM, never signed in.
    NOT_SIGNED_IN = "not_signed_in"
    #: Had a vaulted offline token that Keycloak has since refused.
    SIGN_IN_EXPIRED = "sign_in_expired"
    #: Signed in, but never through the console or a brokered client, so no token is vaulted.
    SCHEDULER_NOT_READY = "scheduler_not_ready"
    #: Jobs deliver to a channel whose workspace the user has no sign-in in (per client + workspace).
    UNREACHABLE = "unreachable"
    #: A client reported it found no recipient for the user (per client + workspace).
    UNDELIVERED = "undelivered"
    #: The user lost access to the sub-agent a job runs (per agent).
    AGENT_INACCESSIBLE = "agent_inaccessible"
    #: The grant behind a job the user subscribed to was withdrawn.
    ACCESS_REVOKED = "access_revoked"
    #: Switched off with nothing left to release it but the member: a sign-in hold that
    #: outlived their sign-in, or a delivery hold whose channel was deleted. They switch it
    #: back on (or move it) from their Scheduler page; an administrator cannot.
    NEEDS_RESUME = "needs_resume"
    #: Jobs deliver to a channel Nannos cannot judge: no brokered sign-in with that client
    #: (an older local sign-in looks the same), or a channel with no known workspace.
    UNKNOWN_REACHABILITY = "unknown_reachability"


class IssueSeverity(str, Enum):
    """How much an issue costs the user right now."""

    #: A job that should run or deliver does not.
    BLOCKING = "blocking"
    #: Nothing is lost yet (no job waits on it), but the next job will.
    PENDING = "pending"
    #: Nannos cannot tell. Filterable, never counts towards a user's severity.
    INFO = "info"


class OnboardingIssueJob(BaseModel):
    """A job an issue stops, by its definition."""

    id: int
    name: str


class OnboardingIssue(BaseModel):
    """One thing a user is missing. Delivery issues are per client and workspace, so an
    administrator sees where the user has to sign in; an agent issue names the agent."""

    kind: OnboardingIssueKind
    severity: IssueSeverity
    jobs: list[OnboardingIssueJob] = Field(description="The jobs this stops, by name.")
    client_id: str | None = Field(default=None, description="The chat client, for delivery issues.")
    client_name: str | None = Field(default=None, description="Its display name (the broker client's).")
    workspace_id: str | None = Field(
        default=None, description="The client's workspace (a Slack team, a Google Chat project), when known."
    )
    channel_names: list[str] = Field(default_factory=list, description="The delivery channels involved.")
    agent_id: int | None = Field(default=None, description="The sub-agent, for agent_inaccessible.")
    agent_name: str | None = None


class UserOnboarding(BaseModel):
    """What stands between a user and scheduled jobs that run and reach them, for
    administrators. Derived on every read by `onboarding_sql`, never stored.

    Provisioned-but-never-signed-in is the normal state right after SCIM provisioning:
    it is only ``blocking`` once a job waits on it (ADR-0011).
    """

    severity: IssueSeverity | None = Field(
        description=(
            "The worst of the user's issues, ignoring info ones: blocking, pending, or null when "
            "nothing needs attention."
        )
    )
    issues: list[OnboardingIssue] = Field(
        description="Worst first, then by how many jobs each stops.",
    )

    @classmethod
    def of(cls, is_service_account: bool, onboarding: dict[str, Any] | str | None) -> "UserOnboarding | None":
        """*onboarding* is the `onboarding_sql` row as JSON (the driver may hand it over as
        a string). None for a machine identity: it never signs in interactively, so it has
        no onboarding."""
        if is_service_account:
            return None
        if isinstance(onboarding, str):
            onboarding = json.loads(onboarding)
        onboarding = onboarding or {}
        rank = onboarding.get("severity_rank") or 0
        return cls(
            severity=_SEVERITY_BY_RANK.get(rank),
            issues=[OnboardingIssue.model_validate(i) for i in onboarding.get("issues") or []],
        )


#: `onboarding_sql`'s ``severity_rank``: the worst non-info severity, 0 for none.
SEVERITY_RANK = {IssueSeverity.BLOCKING: 2, IssueSeverity.PENDING: 1}
_SEVERITY_BY_RANK = {rank: severity for severity, rank in SEVERITY_RANK.items()}


class UserSort(str, Enum):
    """Orders of the admin user list and the group member list."""

    #: Newest first (the user list's default).
    CREATED = "created"
    #: By name (the member list's default).
    NAME = "name"
    #: Worst onboarding severity first, then the most jobs stopped, then newest.
    SEVERITY = "severity"


class OnboardingSummaryEntry(BaseModel):
    """How many users have an issue of one kind (on one client, for delivery issues)."""

    kind: OnboardingIssueKind
    client_id: str | None = None
    client_name: str | None = None
    users: int


class OnboardingSummary(BaseModel):
    """Counts behind the Users page's onboarding filters, over the users the list's own
    search, group and status filters keep."""

    blocking: int = Field(description="Users whose worst issue is blocking.")
    pending: int = Field(description="Users whose worst issue is pending.")
    issues: list[OnboardingSummaryEntry] = Field(description="Most users first.")


class OnboardingSummaryResponse(BaseModel):
    data: OnboardingSummary


class UserWithGroups(User):
    """User with group memberships."""

    groups: list[UserGroupMembership] = Field(default_factory=list)
    #: None for a service account (see `UserOnboarding.of`).
    onboarding: UserOnboarding | None


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
    language: str = Field(default_factory=lambda: os.getenv("DEFAULT_LANGUAGE", "en"))
    timezone: str = Field(default_factory=default_timezone_name)
    custom_prompt: str | None = None
    mcp_tools: list[str] = Field(default_factory=list)
    preferred_model: str | None = None
    # Derived, not persisted — model lifecycle for preferred_model, computed on the read path
    # from the live gateway registry + chat default (see services/model_status.py). Mirrors
    # the sub-agent fields so the Settings UI can render "<preferred> (retired) -> <effective>".
    preferred_model_retired: bool = False
    effective_preferred_model: str | None = None
    enable_thinking: bool | None = None
    thinking_level: OrchestratorThinkingLevel | None = None
    phone_number_override: str | None = None
    tool_bypass_rules: dict[str, Any] = Field(default_factory=dict)
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
    phone_number_override: str | None = None
    tool_bypass_rules: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        # Scheduled jobs snapshot this value verbatim and evaluate cron
        # expressions in it, so an unresolvable name must be rejected here at
        # the source rather than surfacing later as a paused job.
        return validate_timezone_name(v)


class UserSettingsResponse(BaseModel):
    """User settings response."""

    model_config = {"from_attributes": True}

    data: UserSettings


class UserAdminUpdate(BaseModel):
    """Request for admin to update user fields."""

    is_administrator: bool | None = None


class PhoneVerificationRequest(BaseModel):
    """Request to send a phone verification code."""

    phone_number: str
    channel: str = "sms"  # "sms" or "call"


class PhoneVerificationCheckRequest(BaseModel):
    """Request to verify a phone verification code."""

    phone_number: str
    code: str


class ImpersonateStartRequest(BaseModel):
    """Request model for starting user impersonation."""

    target_user_id: str
