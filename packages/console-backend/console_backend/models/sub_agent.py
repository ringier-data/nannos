"""Pydantic models for sub-agents."""

import re
import uuid as _uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .skills_registry import RegistryScope, SkillFile


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
    """Reasoning effort (LiteLLM convention). Per-model support comes from the gateway."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ModelTier(str, Enum):
    """Capability tier a sub-agent binds to instead of a concrete model alias. Resolves to
    the chat:<tier> model_defaults slot at read time ('standard' → the plain 'chat' default),
    so retiring/upgrading a model is one slot repoint. Mutually exclusive with an
    explicit model."""

    LOW = "low"
    STANDARD = "standard"
    PREMIUM = "premium"


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


class SkillDefinition(BaseModel):
    """A skill bundled with a sub-agent config version.

    On read from DB, only registry_id and content_hash are present (from SkillRef JSONB).
    All other fields are populated by resolve_imported_skills() from the registry.
    """

    name: str = Field(default="", description="Skill identifier (lowercase, alphanumeric + hyphens)")
    description: str = Field(default="", max_length=1024, description="What the skill does")
    body: str = Field(default="", description="SKILL.md body content (markdown). Empty for registry-backed skills.")
    files: list[SkillFile] = Field(
        default_factory=list, description="Optional scripts/references/assets. Empty for registry-backed skills."
    )
    registry_id: str | None = Field(
        default=None,
        description="UUID of the skill in skill_registry table.",
    )
    source: str | None = Field(
        default=None,
        description="External provenance path where the skill was imported from (e.g., 'vercel-labs/agent-skills/xlsx'). Informational only.",
    )
    content_hash: str | None = Field(
        default=None,
        description="Content hash pinning this skill to a specific version in the registry.",
    )
    update_available: bool = Field(
        default=False,
        description="True when the registry has a newer version than the pinned content_hash.",
    )
    latest_hash: str | None = Field(
        default=None,
        description="Current content_hash in the registry. Present when update_available=True.",
    )
    sandbox_required: bool = Field(
        default=False,
        description="Whether the skill contains executable files (.py, .sh, etc.) that require sandbox.",
    )
    scope: RegistryScope | None = Field(
        default=None,
        description="Registry scope: 'sub-agent' for inline-editable skills, 'standalone' for imported read-only. Set on read.",
    )

    @model_validator(mode="before")
    @classmethod
    def _compat_source_hash(cls, data: dict) -> dict:
        """Backward compat: map old 'source_hash' key to 'content_hash'."""
        if isinstance(data, dict) and "source_hash" in data and "content_hash" not in data:
            data["content_hash"] = data.pop("source_hash")
        return data

    @field_validator("registry_id")
    @classmethod
    def _validate_registry_id(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                _uuid.UUID(v)
            except (ValueError, AttributeError):
                raise ValueError(f"registry_id must be a valid UUID, got: {v!r}")
        return v

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        # Allow empty — populated after resolve_imported_skills
        if not v:
            return v
        if len(v) > 64 or not _SKILL_NAME_RE.match(v):
            raise ValueError(
                "Skill name must be 1-64 chars, lowercase alphanumeric + hyphens, "
                "no leading/trailing/consecutive hyphens"
            )
        return v


class SkillRef(BaseModel):
    """Minimal reference to a versioned skill in the registry.

    This is what gets persisted in config version JSONB.
    registry_id identifies the skill, content_hash pins the version.
    """

    registry_id: str = Field(..., description="UUID of the skill in skill_registry")
    content_hash: str = Field(..., description="SHA-256 content hash pinning to a specific version")


class SkillSummary(BaseModel):
    """Lightweight skill metadata for list responses (no body/files content)."""

    name: str
    description: str = ""
    source: str | None = None
    content_hash: str | None = None
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
    # Capability tier the agent binds to instead of `model` (mutually exclusive). Resolved to
    # the chat:<tier> default at read time → effective_model (see services/model_status.py).
    model_tier: ModelTier | None = None
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


class SubAgentConfigVersionRaw(SubAgentConfigVersionBase):
    """Version as stored in the database — skills are unresolved references.

    Use this when reading config version rows before batch-resolving skills.
    Convert to SubAgentConfigVersion after resolution.
    """

    skill_refs: list[SkillRef] = Field(default_factory=list)

    def to_resolved(self, skills: list[SkillDefinition]) -> "SubAgentConfigVersion":
        """Produce a fully resolved config version with complete skill content."""
        data = self.model_dump(exclude={"skill_refs"})
        data["skills"] = skills
        return SubAgentConfigVersion(**data)


class SubAgentConfigVersion(SubAgentConfigVersionBase):
    """Full version with complete skill content (body + files)."""

    skills: list[SkillDefinition] = Field(default_factory=list)

    # Derived, not persisted — computed on the read path from the live gateway registry +
    # the chat default (see services/model_status.py). This is the single source of truth
    # for model-retirement handling: clients render "<model> (retired) -> <effective_model>"
    # and the orchestrator runs `effective_model` directly, neither re-deriving the rule.
    model_retired: bool = False
    # The alias the agent actually runs on: `model` when still registered, the chat default
    # when `model` has been retired, or None when no model was set (inherit the orchestrator).
    effective_model: str | None = None


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


# Any alias registered on the Model Gateway (the gateway is the source of truth
# for which models exist). Deliberately NOT a hardcoded Literal — that goes stale the moment
# a model is added/retired and rejects valid, freshly-registered aliases on the write path.
# Selection is constrained to live models by the console UI dropdown (GET /api/v1/models);
# a genuinely unknown alias is caught downstream (the gateway 400s) and surfaced as a retired
# model on the read path (see services/model_status.py).
ModelName = str


class SubAgentCreate(BaseModel):
    """Request model for creating a sub-agent."""

    name: str
    description: str
    type: SubAgentType
    is_public: bool = False  # If true, accessible to all users without group permissions

    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url, Foundry agents use foundry_* fields
    model: ModelName | None = None
    # Bind to a capability tier instead of a concrete model (mutually exclusive with `model`).
    model_tier: ModelTier | None = None
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

    @model_validator(mode="after")
    def _validate_model_xor_tier(self) -> "SubAgentCreate":
        if self.model is not None and self.model_tier is not None:
            raise ValueError("set either model or model_tier, not both")
        return self


class SubAgentUpdate(BaseModel):
    """Request model for updating a sub-agent."""

    name: str | None = None
    description: str | None = None
    is_public: bool | None = None  # If true, accessible to all users without group permissions

    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url, Foundry agents use foundry_* fields
    model: ModelName | None = None  # LLM model to use
    # Bind to a capability tier instead of a concrete model (mutually exclusive with `model`).
    model_tier: ModelTier | None = None
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

    @model_validator(mode="after")
    def _validate_model_xor_tier(self) -> "SubAgentUpdate":
        if self.model is not None and self.model_tier is not None:
            raise ValueError("set either model or model_tier, not both")
        return self


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
                content_hash=s.content_hash,
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
