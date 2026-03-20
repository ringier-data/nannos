"""Pydantic models for sub-agents."""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


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


class SubAgentConfigVersion(BaseModel):
    """Version history entry for sub-agent configurations."""

    id: int | None = None  # Primary key from sub_agent_config_versions table
    sub_agent_id: int | None = None  # Foreign key to sub_agents
    version: int
    version_hash: str | None = None  # Content hash (12 chars) - identifies drafts/pending
    release_number: int | None = None  # Sequential release number - only for approved versions
    description: str  # Agent skill set description - crucial for orchestrator routing

    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url, Foundry agents use foundry_* fields
    model: str | None = (
        None  # LLM model: 'gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6', 'claude-haiku-4-5'
    )
    system_prompt: str | None = None  # For local sub-agents: the system prompt
    agent_url: str | None = None  # For remote sub-agents: the URL of the agent
    mcp_tools: list[str] = Field(default_factory=list)  # MCP tool names enabled for this version

    # Foundry agent configuration
    foundry_hostname: str | None = None
    foundry_client_id: str | None = None
    foundry_client_secret_ref: int | None = None  # Reference to secrets table
    foundry_client_secret_ssmkey: str | None = Field(
        default=None,
        description=(
            "SSM Parameter Store name for Foundry client secret. "
            "Is retrieved conditionally just when needed by the orchestrator."
        ),
    )
    foundry_ontology_rid: str | None = None
    foundry_query_api_name: str | None = None
    foundry_scopes: list[str] | None = None  # Stored as TEXT[] in database
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
    thinking_level: ThinkingLevel | None = ThinkingLevel.LOW

    change_summary: str | None = None
    status: SubAgentStatus = SubAgentStatus.DRAFT
    submitted_by_user_id: str | None = None  # User who submitted for approval
    approved_by_user_id: str | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    deleted_at: datetime | None = None  # Soft delete timestamp
    created_at: datetime


class SubAgent(BaseModel):
    """Sub-agent model.

    Metadata (name, owner, type) lives on sub_agents table.
    Configuration data (description, model, config, status) lives on sub_agent_config_versions.
    The config_version field holds the joined version data (default or specific version).
    """

    id: int
    name: str
    owner_user_id: str
    owner: SubAgentOwner | None = None
    owner_status: OwnerStatus = OwnerStatus.ACTIVE
    type: SubAgentType
    current_version: int = 1
    default_version: int | None = None  # NULL means no approved version yet
    config_version: SubAgentConfigVersion | None = None  # Joined version data
    is_public: bool | None = None  # If true, accessible to all users without group permissions
    is_activated: bool | None = None  # If true, user has activated this sub-agent
    activated_by: ActivationSource | None = None  # Activation source: 'user', 'group', or 'admin'
    activated_by_groups: list[int] | None = None  # List of group IDs that activated this agent
    deleted_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        from_attributes = True


ModelName = Literal["gpt-4o", "gpt-4o-mini", "claude-sonnet-4.5", "claude-sonnet-4.6", "claude-haiku-4-5"]


class SubAgentCreate(BaseModel):
    """Request model for creating a sub-agent."""

    name: str
    description: str
    type: SubAgentType
    is_public: bool = False  # If true, accessible to all users without group permissions

    # Configuration data: Local sub-agents use system_prompt, Remote sub-agents use agent_url, Foundry agents use foundry_* fields
    model: ModelName | None = (
        None  # LLM model: 'gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6', 'claude-haiku-4-5'
    )
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


class SubAgentUpdate(BaseModel):
    """Request model for updating a sub-agent."""

    name: str | None = None
    description: str
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


class SubAgentListResponse(BaseModel):
    """Response model for listing sub-agents."""

    items: list[SubAgent]
    total: int
