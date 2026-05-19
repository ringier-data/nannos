"""Pydantic models for sub-agents."""

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ActivationSource(str, Enum):
    """Activation source enum matching database enum."""

    USER = "user"
    GROUP = "group"
    ADMIN = "admin"


class SubAgentType(str, Enum):
    """Sub-agent type enum matching database enum."""

    REMOTE = "remote"
    LOCAL = "local"
    FOUNDRY = "foundry"
    AUTOMATED = "automated"  # Scheduler-owned: auto-approved, constrained, not interactive


class SubAgentStatus(str, Enum):
    """Sub-agent status enum matching database enum."""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class OwnerStatus(str, Enum):
    """Owner status enum matching database enum."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class SubAgentOwner(BaseModel):
    """Owner information for sub-agent responses."""

    id: str
    name: str
    email: str


class RemoteAgentConfiguration(BaseModel):
    """Configuration for remote A2A agents."""

    agent_url: str


class LocalAgentConfiguration(BaseModel):
    """Configuration for local agents."""

    system_prompt: str
    mcp_url: str | None = None


class FoundryScope(str, Enum):
    """Foundry API scopes for OAuth2 authentication."""

    ONTOLOGIES_READ = "api:use-ontologies-read"
    ONTOLOGIES_WRITE = "api:use-ontologies-write"
    AIP_AGENTS_READ = "api:use-aip-agents-read"
    AIP_AGENTS_WRITE = "api:use-aip-agents-write"
    MEDIASETS_READ = "api:use-mediasets-read"
    MEDIASETS_WRITE = "api:use-mediasets-write"


class ThinkingLevel(str, Enum):
    """Thinking depth level for extended thinking mode."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FoundryAgentConfiguration(BaseModel):
    """Configuration for Foundry agents."""

    foundry_hostname: str = Field(
        default="https://blumen.palantirfoundry.de",
        description="Foundry instance hostname (e.g., 'https://blumen.palantirfoundry.de')",
    )
    foundry_client_id: str = Field(..., description="OAuth2 client ID for Foundry authentication")
    foundry_client_secret_ref: int = Field(..., description="Reference to secret ID in secrets table")
    foundry_ontology_rid: str = Field(..., description="Ontology RID (required)")
    foundry_query_api_name: str = Field(..., description="Query API name to execute (e.g., 'a2ATicketWriterAgent')")
    foundry_scopes: list[FoundryScope] = Field(..., description="OAuth2 scopes for Foundry API access")
    foundry_version: str | None = Field(None, description="Optional query API version")


_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class SkillFile(BaseModel):
    """A file bundled with a standard skill (e.g., script, reference doc)."""

    path: str = Field(..., description="Relative path inside the skill directory (e.g., 'scripts/check.py')")
    content: str = Field(..., description="File content")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        """Reject path traversal, absolute paths, and excessive depth."""
        if v.startswith("/") or v.startswith("~"):
            raise ValueError(f"Skill file path must be relative: {v}")
        segments = v.split("/")
        if ".." in segments:
            raise ValueError(f"Path traversal not allowed: {v}")
        if len(segments) > 6:
            raise ValueError(f"Skill file path exceeds max depth (6): {v}")
        if not v or not all(segments):
            raise ValueError(f"Invalid skill file path: {v}")
        return v


class SkillDefinition(BaseModel):
    """A standard (immutable) skill bundled with a sub-agent config version.

    Two modes:
    - Custom skills (source=None): full content stored inline (body + files)
    - Imported skills (source set): only reference stored (name, description, source, source_hash).
      Body and files are empty/absent — resolve from skill registry at runtime.
    """

    name: str = Field(..., description="Skill identifier (lowercase, alphanumeric + hyphens)")
    description: str = Field(..., max_length=1024, description="What the skill does")
    body: str = Field(
        default="", description="SKILL.md body content (markdown). Empty for imported skills (resolve from registry)."
    )
    files: list[SkillFile] = Field(
        default_factory=list, description="Optional scripts/references/assets. Empty for imported skills."
    )
    source: str | None = Field(
        default=None,
        description="Registry skill ID if imported (e.g., 'vercel-labs/agent-skills/next-js-dev'). Null for custom skills.",
    )
    source_hash: str | None = Field(
        default=None,
        description="Content hash of the registry skill at import time. Used to detect available updates.",
    )
    update_available: bool = Field(
        default=False,
        description="True when the registry has a newer version than the pinned source_hash.",
    )
    latest_hash: str | None = Field(
        default=None,
        description="Current content_hash in the registry. Present when update_available=True.",
    )
    sandbox_required: bool = Field(
        default=False,
        description="Whether the skill contains executable files (.py, .sh, etc.) that require sandbox.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or len(v) > 64 or not _SKILL_NAME_RE.match(v):
            raise ValueError(
                "Skill name must be 1-64 chars, lowercase alphanumeric + hyphens, "
                "no leading/trailing/consecutive hyphens"
            )
        return v


class SkillSummary(BaseModel):
    """Lightweight skill metadata for list responses (no body/files content)."""

    name: str
    description: str = ""
    source: str | None = None
    source_hash: str | None = None
    update_available: bool = False
    latest_hash: str | None = None
    sandbox_required: bool = False


class SubAgentConfigVersionBase(BaseModel):
    """Base fields shared between full and summary config versions."""

    id: int | None = None
    sub_agent_id: int | None = None
    version: int
    version_hash: str | None = None
    release_number: int | None = None
    description: str  # Agent skill set description - crucial for orchestrator routing
    model: str | None = None
    system_prompt: str | None = None
    agent_url: str | None = None
    mcp_tools: list[str] = Field(default_factory=list)
    foundry_hostname: str | None = None
    foundry_client_id: str | None = None
    foundry_client_secret_ref: int | None = None
    foundry_client_secret_ssmkey: str | None = Field(
        default=None,
        description=(
            "SSM Parameter Store name for Foundry client secret. "
            "Is retrieved conditionally just when needed by the orchestrator."
        ),
    )
    foundry_ontology_rid: str | None = None
    foundry_query_api_name: str | None = None
    foundry_scopes: list[str] | None = None
    foundry_version: str | None = None
    pricing_config: dict | None = Field(
        default=None,
        description=(
            "Agent-specific rate card configuration. Only applicable for remote and foundry agents. "
            "Format: {'rate_card_entries': [{'billing_unit': 'token_name', 'price_per_million': 1.5}]} "
            "or {'price_per_million_requests': 0.05}"
        ),
    )
    enable_thinking: bool | None = None
    thinking_level: ThinkingLevel | None = ThinkingLevel.LOW
    sandbox_enabled: bool = False
    change_summary: str | None = None
    status: SubAgentStatus = SubAgentStatus.DRAFT
    submitted_by_user_id: str | None = None
    approved_by_user_id: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    deleted_at: datetime | None = None
    created_at: datetime


class SubAgentConfigVersion(SubAgentConfigVersionBase):
    """Full version with complete skill content (body + files)."""

    skills: list[SkillDefinition] = Field(default_factory=list)


class SubAgentBase(BaseModel):
    """Base fields shared between SubAgent (detail) and SubAgentListItem (list)."""

    id: int
    name: str
    owner_user_id: str
    owner: SubAgentOwner | None = None
    owner_status: OwnerStatus = OwnerStatus.ACTIVE
    type: SubAgentType
    system_role: str | None = None
    current_version: int = 1
    default_version: int | None = None
    is_public: bool | None = None
    is_activated: bool | None = None
    activated_by: ActivationSource | None = None
    activated_by_groups: list[int] | None = None
    effective_permission: Literal["owner", "write", "read"] | None = None
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


class SubAgent(SubAgentBase):
    """Sub-agent model with full config version (including skill body/files).

    Metadata (name, owner, type) lives on sub_agents table.
    Configuration data (description, model, config, status) lives on sub_agent_config_versions.
    The config_version field holds the joined version data (default or specific version).
    """

    config_version: SubAgentConfigVersion | None = None  # Joined version data


ModelName = Literal[
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet-4.5",
    "claude-sonnet-4.6",
    "claude-haiku-4-5",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
]


class SubAgentCreate(BaseModel):
    """Request model for creating a sub-agent."""

    name: str
    description: str
    type: SubAgentType
    is_public: bool = False  # If true, accessible to all users without group permissions

    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url, Foundry agents use foundry_* fields
    model: ModelName | None = None
    system_prompt: str | None = None  # For local sub-agents: the system prompt
    agent_url: str | None = None  # For remote sub-agents: the URL of the agent
    mcp_tools: list[str] | None = None  # MCP tool names enabled for this version

    # Foundry agent configuration
    foundry_hostname: str | None = None
    foundry_client_id: str | None = None
    foundry_client_secret_ref: int | None = None  # Reference to existing secret in secrets table
    foundry_ontology_rid: str | None = None
    foundry_query_api_name: str | None = None
    foundry_scopes: list[FoundryScope] | None = None
    foundry_version: str | None = None

    # Agent-specific pricing configuration (remote and foundry agents only)
    pricing_config: dict | None = Field(
        default=None,
        description=(
            "Agent-specific rate card configuration. Only applicable for remote and foundry agents. "
            "Format: {'rate_card_entries': [{'billing_unit': 'token_name', 'price_per_million': 1.5}]} "
            "or {'price_per_million_requests': 0.05}"
        ),
    )

    # Extended thinking configuration (only supported for Claude Sonnet and Gemini models)
    enable_thinking: bool | None = None
    thinking_level: ThinkingLevel | None = None

    # Standard skills and sandbox execution
    skills: list[SkillDefinition] = Field(default_factory=list)
    sandbox_enabled: bool = False

    @model_validator(mode="after")
    def _validate_sandbox_local_only(self) -> "SubAgentCreate":
        if self.sandbox_enabled and self.type != SubAgentType.LOCAL:
            raise ValueError("sandbox_enabled is only supported for local agents")
        return self


class SubAgentUpdate(BaseModel):
    """Request model for updating a sub-agent."""

    name: str | None = None
    description: str | None = None
    is_public: bool | None = None  # If true, accessible to all users without group permissions

    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url, Foundry agents use foundry_* fields
    model: ModelName | None = None  # LLM model to use
    system_prompt: str | None = None  # For local sub-agents: the system prompt
    agent_url: str | None = None  # For remote sub-agents: the URL of the agent
    mcp_tools: list[str] | None = None  # MCP tool names enabled for this version

    # Foundry agent configuration
    foundry_hostname: str | None = None
    foundry_client_id: str | None = None
    foundry_client_secret_ref: int | None = None  # Reference to existing secret in secrets table
    foundry_ontology_rid: str | None = None
    foundry_query_api_name: str | None = None
    foundry_scopes: list[FoundryScope] | None = None
    foundry_version: str | None = None

    # Agent-specific pricing configuration (remote and foundry agents only)
    pricing_config: dict | None = Field(
        default=None,
        description=(
            "Agent-specific rate card configuration. Only applicable for remote and foundry agents. "
            "Format: {'rate_card_entries': [{'billing_unit': 'token_name', 'price_per_million': 1.5}]} "
            "or {'price_per_million_requests': 0.05}"
        ),
    )

    # Extended thinking configuration (only supported for Claude Sonnet and Gemini models)
    enable_thinking: bool | None = None
    thinking_level: ThinkingLevel | None = None

    # Standard skills and sandbox execution
    skills: list[SkillDefinition] | None = None
    sandbox_enabled: bool | None = None

    change_summary: str | None = None  # For version history


class SubAgentApproval(BaseModel):
    """Request model for approving/rejecting a sub-agent."""

    action: str = Field(..., pattern="^(approve|reject)$")
    rejection_reason: str | None = None


class SubAgentVersionApproval(BaseModel):
    """Request model for approving/rejecting a specific version."""

    action: str = Field(..., pattern="^(approve|reject)$")
    rejection_reason: str | None = None


class SubAgentSubmitRequest(BaseModel):
    """Request model for submitting a version for approval."""

    change_summary: str = Field(..., min_length=1, description="Required description of changes in this version")


class SubAgentSetDefaultVersion(BaseModel):
    """Request model for setting the default version."""

    version: int


class SubAgentGroupPermission(BaseModel):
    """Permission assignment for a group on a sub-agent."""

    user_group_id: int
    permissions: list[Literal["read", "write"]]


class SubAgentPermissionsUpdate(BaseModel):
    """Request model for updating sub-agent permissions with read/write granularity."""

    group_permissions: list[SubAgentGroupPermission]


class SubAgentGroupPermissionResponse(BaseModel):
    """Response model for group permissions on a sub-agent."""

    user_group_id: int
    user_group_name: str
    permissions: list[Literal["read", "write"]]


class SubAgentConfigVersionSummary(SubAgentConfigVersionBase):
    """Lightweight config version for list responses (skills without body/files)."""

    skills: list[SkillSummary] = Field(default_factory=list)

    @classmethod
    def from_full(cls, cv: SubAgentConfigVersion) -> "SubAgentConfigVersionSummary":
        """Convert a full config version to a summary (strips skill body/files)."""
        data = cv.model_dump(exclude={"skills"})
        data["skills"] = [
            SkillSummary(
                name=s.name,
                description=s.description,
                source=s.source,
                source_hash=s.source_hash,
                update_available=s.update_available,
                latest_hash=s.latest_hash,
                sandbox_required=s.sandbox_required,
            )
            for s in cv.skills
        ]
        return cls(**data)


class SubAgentListItem(SubAgentBase):
    """Lightweight sub-agent for list responses (skills without body/files)."""

    config_version: SubAgentConfigVersionSummary | None = None

    @classmethod
    def from_sub_agent(cls, sa: "SubAgent") -> "SubAgentListItem":
        """Convert a full SubAgent to a list item (strips skill body/files)."""
        data = sa.model_dump(exclude={"config_version"})
        if sa.config_version:
            data["config_version"] = SubAgentConfigVersionSummary.from_full(sa.config_version)
        return cls(**data)


class SubAgentListResponse(BaseModel):
    """Response model for listing sub-agents."""

    items: list[SubAgentListItem]
    total: int


class SubAgentListFullResponse(BaseModel):
    """Response model for listing sub-agents with full details."""

    items: list[SubAgent]
    total: int
