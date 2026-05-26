"""Pydantic models for the Skills Registry API.

Handles discovery and import of skills from external sources.
Architecture: Git-first (any Git repo is a valid source), with optional
registry adapters (skills.sh, self-hosted) for search/discovery.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Registry visibility: who can discover and activate the skill.
RegistryVisibility = Literal["private", "public"]

# Registry scope: what the registry entry represents.
#   - standalone: agent-agnostic skill (can be activated on any agent)
#   - sub-agent: skill tied to a specific sub-agent's config
RegistryScope = Literal["standalone", "sub-agent"]


class SkillFile(BaseModel):
    """A single file within a skill (SKILL.md, examples, etc.)."""

    path: str = Field(description="Relative file path within the skill directory")
    content: str = Field(description="Full text content of the file")
    encoding: str | None = Field(
        default=None, description="Content encoding: None for UTF-8 text, 'base64' for binary files"
    )

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


class SkillSearchResult(BaseModel):
    """A skill found via search or browse."""

    id: str = Field(
        description="Stable unique identifier. Format: '{source}/{slug}' (registry) or '{repo}/{skill}' (Git)"
    )
    slug: str = Field(description="URL-safe skill slug (e.g. 'next-js-development')")
    name: str = Field(description="Human-readable name (e.g. 'Next.js Development')")
    description: str | None = Field(default=None, description="Short description of what the skill does")
    source: str = Field(description="Source repository (e.g. 'vercel-labs/agent-skills')")
    installs: int = Field(default=0, description="Total install count (registry only, 0 for Git sources)")
    source_type: str = Field(default="github", description="'github' or 'well-known'")
    author: str | None = Field(default=None, description="Display name of the skill author (registry only)")
    visibility: RegistryVisibility | None = Field(default=None, description="'private' or 'public' (registry only)")
    install_url: str | None = Field(default=None, description="Git clone URL or registry page URL")
    url: str | None = Field(default=None, description="Direct link to skill (GitHub tree URL or registry page)")


class SkillSearchResponse(BaseModel):
    """Response for skill search/browse."""

    data: list[SkillSearchResult] = Field(default_factory=list)
    query: str = Field(default="")
    search_type: str | None = Field(default=None, description="'fuzzy' or 'semantic' (registry search only)")
    count: int = Field(default=0, description="Number of results returned")
    total: int = Field(default=0, description="Total matching results (for pagination)")
    offset: int = Field(default=0, description="Current offset")
    has_more: bool = Field(default=False, description="Whether more results are available")


class SkillDetailResponse(BaseModel):
    """Skill detail from registry (metadata + files)."""

    id: str = Field(description="Stable unique identifier")
    source: str = Field(description="Source repository or provider")
    slug: str = Field(description="URL-safe skill slug")
    installs: int = Field(default=0, description="Total install count")
    hash: str | None = Field(default=None, description="Content hash for cache invalidation")
    files: list[SkillFile] | None = Field(default=None, description="All files in the skill folder")


class GitHubSkillDetail(BaseModel):
    """Skill detail fetched directly from a Git repository."""

    files: list[SkillFile] = Field(description="All files in the skill directory")
    tree_sha: str | None = Field(default=None, description="Git tree SHA for the skill directory")


class SkillAuditEntry(BaseModel):
    """A single security audit from a registry partner."""

    provider: str = Field(description="Partner display name (e.g. 'Gen Agent Trust Hub', 'Socket')")
    slug: str = Field(description="URL-safe partner slug")
    status: str = Field(description="Normalized verdict: 'pass', 'warn', or 'fail'")
    summary: str = Field(description="One-line human-readable summary")
    audited_at: str = Field(description="ISO 8601 timestamp of audit")
    risk_level: str | None = Field(default=None, description="'NONE', 'LOW', 'MEDIUM', 'HIGH', or 'CRITICAL'")
    categories: list[str] | None = Field(default=None, description="Detected categories")


class SkillAuditResponse(BaseModel):
    """Security audit response from registry."""

    id: str = Field(description="Stable unique identifier")
    source: str = Field(description="Source repository or provider")
    slug: str = Field(description="URL-safe skill slug")
    audits: list[SkillAuditEntry] = Field(default_factory=list, description="Security audit results")


# --- Activation scope ---

# Activation scope: who "owns" this skill activation.
#   personal  — activated by/for a specific user
#   group     — activated by/for a user group (shared)
#   sub-agent — part of the sub-agent's own config (visible to all users)
ActivationScope = Literal["personal", "group", "sub-agent"]


# --- Import models ---


class SkillImportRequest(BaseModel):
    """Request body for importing a skill.

    Two resolution paths (Git-first architecture):
    1. Direct Git: provide `repo` + `skill` (+ optional `ref`)
    2. Via registry: provide `registry_id` which resolves to repo + skill
    """

    repo: str | None = Field(
        default=None,
        description="Git repository (e.g. 'anthropics/skills', 'OthmanAdi/planning-with-files'). Primary source.",
    )
    skill: str | None = Field(
        default=None,
        description="Skill name/directory within the repo. If omitted with repo, assumes repo IS the skill.",
    )
    ref: str = Field(
        default="main",
        description="Git ref to fetch from (branch, tag, or commit SHA).",
    )
    registry_id: str | None = Field(
        default=None,
        description=(
            "Registry skill ID in format '{source}/{slug}' "
            "(e.g. 'vercel-labs/agent-skills/next-js-development'). "
            "Resolves to repo + skill via the configured registry. "
            "Alternative to providing repo + skill directly."
        ),
    )
    agent: str | None = Field(
        default=None, description="Target sub-agent name. If omitted, skill is added to registry only (no activation)."
    )
    scope: ActivationScope = Field(
        default="sub-agent", description="Activation scope: 'personal', 'group', or 'sub-agent' (baked into config)"
    )
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    overwrite: bool = Field(default=False, description="Overwrite existing skill with same name")
    force: bool = Field(
        default=False, description="Force import even if security assessment is 'unsafe' (requires approver role)"
    )


class SkillSourceInfo(BaseModel):
    """Provenance metadata stored with imported skills."""

    type: str = Field(description="Source type: 'git' or 'registry'")
    repo: str = Field(description="Git repository (owner/repo)")
    skill: str = Field(description="Skill name within the repo")
    ref: str = Field(default="main", description="Git ref that was fetched")
    hash: str | None = Field(default=None, description="Git tree SHA at time of import")
    registry_id: str | None = Field(default=None, description="Registry ID if discovered via registry")
    imported_at: str = Field(description="ISO 8601 timestamp of import")


# --- Security models ---


class SkillSecurityIndicator(BaseModel):
    """A single security risk indicator detected in skill files."""

    category: str = Field(description="Risk category identifier")
    risk_level: str = Field(description="'high' or 'medium'")
    evidence: list[str] = Field(default_factory=list, description="Snippets/paths that triggered detection")
    description: str = Field(description="Human-readable explanation")


class SkillSecurityVerdict(BaseModel):
    """Combined security assessment for a skill."""

    verdict: str = Field(description="'safe', 'caution', or 'unsafe'")
    indicators: list[SkillSecurityIndicator] = Field(default_factory=list)
    registry_audit: SkillAuditResponse | None = Field(default=None, description="Registry audit if available")
    reasoning: str = Field(description="Summary explanation of the verdict")
    assessed_at: str = Field(description="ISO 8601 timestamp")
    content_hash: str = Field(description="SHA-256 hash of skill contents (cache key)")


class SkillImportResponse(BaseModel):
    """Response after successful skill import."""

    skill_name: str = Field(description="Imported skill name")
    agent: str | None = Field(default=None, description="Target sub-agent name (None if registry-only)")
    scope: ActivationScope = Field(description="Activation scope")
    source: SkillSourceInfo = Field(description="Provenance metadata")
    files_count: int = Field(description="Total number of files imported (including SKILL.md)")
    overwritten: bool = Field(default=False, description="Whether an existing skill was overwritten")
    security: SkillSecurityVerdict = Field(description="Security assessment result")


# --- Activation models ---


class SkillActivation(BaseModel):
    """A skill activation record — links a registry entry to an agent at a specific scope."""

    id: int
    sub_agent_id: int
    registry_id: str = Field(description="UUID of the registry entry")
    scope: ActivationScope
    user_id: str | None = None
    group_id: int | None = None
    content_hash: str = Field(description="Pinned content hash at activation time")
    config_version_id: int | None = None
    activated_at: datetime
    activated_by: str

    @field_validator("registry_id", mode="before")
    @classmethod
    def _coerce_registry_id(cls, v: Any) -> str:
        return str(v) if v is not None else v


class SkillActivationWithStatus(BaseModel):
    """Activation record enriched with update-available status and skill metadata."""

    id: int
    sub_agent_id: int
    registry_id: str
    scope: ActivationScope
    user_id: str | None = None
    group_id: int | None = None
    group_name: str | None = None
    content_hash: str
    activated_at: datetime
    activated_by: str
    # Joined from skill_registry
    skill_slug: str = Field(description="Skill identifier (slug) for use in MCP tool calls")
    skill_name: str
    skill_description: str | None = None
    # Computed
    update_available: bool = Field(
        default=False, description="True when registry content_hash differs from activation content_hash"
    )
    latest_hash: str | None = Field(default=None, description="Current registry content_hash (if different)")


class SkillActivationRequest(BaseModel):
    """Request to activate a registry skill on an agent."""

    registry_id: str = Field(description="UUID of the skill in the registry")
    sub_agent_id: int = Field(description="Target sub-agent ID")
    scope: ActivationScope
    group_id: int | None = Field(default=None, description="Group ID (required when scope='group')")


class SkillActivationListResponse(BaseModel):
    """Response for listing activations for an agent."""

    items: list[SkillActivationWithStatus] = Field(default_factory=list)
    total: int = 0


class SkillRegistryCreateRequest(BaseModel):
    """Request to create a new skill in the registry."""

    name: str = Field(description="Skill name (will be slugified)")
    description: str = Field(default="", description="What the skill does")
    files: list[SkillFile] = Field(description="Skill files (must include SKILL.md content)")
    visibility: RegistryVisibility = Field(default="private", description="'private' or 'public'")


class SkillRegistryUpdateRequest(BaseModel):
    """Request to update a skill in the registry."""

    description: str | None = Field(default=None, description="Updated description")
    files: list[SkillFile] | None = Field(default=None, description="Updated files (replaces all)")


# --- Internal DB row models ---


class SkillRegistryEntry(BaseModel):
    """Skill registry entry as returned by service queries.

    Includes table columns plus joined/computed fields (e.g. author_name).
    """

    id: str
    name: str
    slug: str
    description: str | None = None
    source_type: str
    source_repo: str | None = None
    source_ref: str | None = None
    source_path: str | None = None
    files: list[SkillFile] = Field(default_factory=list)
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    security_verdict: str | None = None
    visibility: RegistryVisibility
    scope: RegistryScope | None = None
    sub_agent_id: int | None = None
    sandbox_required: bool = False
    owner_id: str | None = None
    created_by: str
    author_name: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return str(v) if v is not None else v

    @field_validator("files", mode="before")
    @classmethod
    def _coerce_files(cls, v: Any) -> list:
        return v if v is not None else []

    @field_validator("metadata", mode="before")
    @classmethod
    def _coerce_metadata(cls, v: Any) -> dict:
        return v if v is not None else {}


class RegistryRef(BaseModel):
    """Reference to a registry skill in a config version's skills list."""

    registry_id: str
    name: str


class SkillVersionSummary(BaseModel):
    """Summary of a skill version (for version history listing)."""

    content_hash: str
    description: str | None = None
    created_by: str
    created_at: datetime


class SkillVersionDetail(BaseModel):
    """Full detail of a skill version snapshot."""

    files: list[SkillFile] = Field(default_factory=list)
    content_hash: str
    description: str | None = None
    created_by: str
    created_at: datetime

    @field_validator("files", mode="before")
    @classmethod
    def _coerce_files(cls, v: Any) -> list:
        return v if v is not None else []


# --- Skill UI/MCP models ---


class SkillFileSummary(BaseModel):
    """Summary of a file within a skill folder."""

    path: str = Field(description="Relative path within the skill folder (e.g., 'scripts/check.py')")


class SkillSummary(BaseModel):
    """Summary of a skill file (for listing)."""

    name: str = Field(description="Skill identifier (lowercase, hyphens, per SKILL.md spec)")
    title: str = Field(description="Skill name from frontmatter (or first heading for legacy)")
    description: str = Field(
        default="", description="Description from frontmatter (what the skill does and when to use it)"
    )
    scope: ActivationScope = Field(description="'personal', 'group', or 'sub-agent'")
    file_count: int = Field(default=0, description="Number of bundled files (excluding SKILL.md)")
    group_id: str | None = Field(default=None, description="Group ID (present for group scope)")
    group_name: str | None = Field(default=None, description="Group display name (present for group scope)")


class SkillDetail(BaseModel):
    """Full skill file content."""

    name: str
    scope: ActivationScope
    content: str = Field(description="Full SKILL.md content (frontmatter + body)")
    files: list[SkillFileSummary] = Field(default_factory=list, description="Bundled file paths (excluding SKILL.md)")


class McpSkillFile(BaseModel):
    """A file to bundle with a skill."""

    path: str = Field(description="Relative path within the skill folder (e.g., 'scripts/check.py')")
    content: str = Field(description="File content (text)")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        """Reject path traversal, absolute paths, excessive depth, and SKILL.md."""
        if v.startswith("/") or v.startswith("~"):
            raise ValueError(f"Skill file path must be relative: {v}")
        segments = v.split("/")
        if ".." in segments:
            raise ValueError(f"Path traversal not allowed: {v}")
        if len(segments) > 6:
            raise ValueError(f"Skill file path exceeds max depth (6): {v}")
        if not v or not all(segments):
            raise ValueError(f"Invalid skill file path: {v}")
        if v == "SKILL.md":
            raise ValueError(
                "Cannot use 'SKILL.md' as a file path — it is the skill entry point managed via create/update skill."
            )
        return v


# Constraints for skill file operations
MAX_SKILL_FILES = 20
MAX_SKILL_FILE_SIZE_BYTES = 256 * 1024  # 256KB


class SkillCreate(BaseModel):
    """Request body for creating a new skill."""

    name: str = Field(description="Skill identifier (lowercase letters, numbers, hyphens only, per SKILL.md spec)")
    description: str = Field(default="", description="What the skill does and when to use it (shown in skill index)")
    content: str = Field(description="Skill instructions body (Markdown). Frontmatter is generated automatically.")
    files: list[McpSkillFile] | None = Field(default=None, description="Optional files to bundle with the skill")


class SkillUpdate(BaseModel):
    """Request body for updating a skill."""

    content: str = Field(description="Full Markdown content to write")
    files: list[McpSkillFile] | None = Field(
        default=None,
        description="If provided, replaces ALL bundled files. If omitted, existing files are untouched.",
    )


class SkillListResponse(BaseModel):
    """Response for listing skills."""

    items: list[SkillSummary] = Field(default_factory=list)


# --- MCP Tool Request/Response Models ---


class McpSkillCreate(BaseModel):
    """Request body for the console_create_skill MCP tool.

    Creates a skill in the registry and auto-activates it on the calling agent.
    The skill is immediately usable after creation.
    """

    agent_name: str = Field(
        default="self",
        description="Target sub-agent name. Defaults to 'self' (the calling agent).",
    )
    scope: ActivationScope = Field(
        description="Activation scope: 'personal' (user-only), 'group' (shared with group), or 'sub-agent' (baked into sub-agent config for all users)",
    )
    skill_name: str = Field(description="Skill identifier (lowercase letters, numbers, hyphens only)")
    description: str = Field(default="", description="What the skill does and when to use it")
    body: str = Field(description="Skill instructions body (Markdown)")
    files: list[McpSkillFile] | None = Field(default=None, description="Optional files to bundle with the skill")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    visibility: RegistryVisibility = Field(
        default="private",
        description="Registry visibility: 'private' (only you) or 'public' (everyone)",
    )


class McpSkillUpdate(BaseModel):
    """Request body for the console_update_skill MCP tool.

    Updates a skill in the registry and auto-refreshes the calling agent's
    own activation. Other consumers' activations remain pinned.
    """

    agent_name: str = Field(
        default="self",
        description="Target sub-agent name. Defaults to 'self' (the calling agent).",
    )
    scope: ActivationScope = Field(
        description="Scope of the skill to update: 'personal', 'group', or 'sub-agent'",
    )
    skill_name: str = Field(description="Skill identifier to update")
    description: str | None = Field(default=None, description="Updated description")
    body: str | None = Field(default=None, description="Updated skill instructions body (Markdown)")
    content: str | None = Field(
        default=None,
        description="Full SKILL.md content including frontmatter. Mutually exclusive with body.",
    )
    files: list[McpSkillFile] | None = Field(
        default=None,
        description="If provided, replaces ALL bundled files. If omitted, existing files are untouched.",
    )
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    registry_id: str | None = Field(default=None, description="Registry entry UUID (alternative to skill_name lookup)")


class McpSkillRemove(BaseModel):
    """Request body for the console_remove_skill MCP tool.

    Deactivates a skill from the calling agent. The registry entry is preserved
    for other consumers.
    """

    agent_name: str = Field(
        default="self",
        description="Target sub-agent name. Defaults to 'self' (the calling agent).",
    )
    scope: ActivationScope = Field(description="Scope of the skill to remove: 'personal', 'group', or 'sub-agent'")
    skill_name: str = Field(description="Skill identifier to deactivate")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    registry_id: str | None = Field(default=None, description="Registry entry UUID (alternative to skill_name lookup)")


class McpSkillWriteFile(BaseModel):
    """Request body for the console_write_skill_file MCP tool."""

    agent_name: str = Field(
        default="self",
        description="Target sub-agent name. Defaults to 'self' (the calling agent).",
    )
    scope: ActivationScope = Field(description="Scope of the skill: 'personal', 'group', or 'sub-agent'")
    skill_name: str = Field(description="Skill identifier that the file belongs to")
    file_path: str = Field(description="Relative path within the skill folder (e.g., 'scripts/check.py')")
    content: str = Field(description="File content (text)")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    registry_id: str | None = Field(default=None, description="Registry entry UUID (alternative to skill_name lookup)")


class McpSkillDeleteFile(BaseModel):
    """Request body for the console_delete_skill_file MCP tool."""

    agent_name: str = Field(
        default="self",
        description="Target sub-agent name. Defaults to 'self' (the calling agent).",
    )
    scope: ActivationScope = Field(description="Scope of the skill: 'personal', 'group', or 'sub-agent'")
    skill_name: str = Field(description="Skill identifier that the file belongs to")
    file_path: str = Field(description="Relative path of the file to delete (e.g., 'scripts/check.py')")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    registry_id: str | None = Field(default=None, description="Registry entry UUID (alternative to skill_name lookup)")


class McpSkillResponse(BaseModel):
    """Response from MCP skill operations."""

    skill_name: str
    scope: ActivationScope
    agent_name: str
    message: str
    registry_id: str | None = Field(default=None, description="Registry entry UUID (for create/update operations)")


class McpSkillActivate(BaseModel):
    """Request body for the console_activate_skill MCP tool.

    Activates an existing registry skill on the calling agent.
    Use this to adopt a skill created by someone else.
    """

    agent_name: str = Field(
        default="self",
        description="Target sub-agent name. Defaults to 'self' (the calling agent).",
    )
    registry_id: str | None = Field(default=None, description="Registry entry UUID to activate")
    skill_name: str | None = Field(
        default=None, description="Skill name to search in registry (alternative to registry_id)"
    )
    scope: ActivationScope = Field(
        default="personal", description="Activation scope: 'personal', 'group', or 'sub-agent'"
    )
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
