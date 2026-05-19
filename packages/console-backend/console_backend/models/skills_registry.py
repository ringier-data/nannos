"""Pydantic models for the Skills Registry API.

Handles discovery and import of skills from external sources.
Architecture: Git-first (any Git repo is a valid source), with optional
registry adapters (skills.sh, self-hosted) for search/discovery.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class SkillFile(BaseModel):
    """A single file within a skill (SKILL.md, examples, etc.)."""

    path: str = Field(description="Relative file path within the skill directory")
    contents: str = Field(description="Full text content of the file")
    encoding: str | None = Field(
        default=None, description="Content encoding: None for UTF-8 text, 'base64' for binary files"
    )


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
    visibility: str | None = Field(default=None, description="'private' or 'public' (registry only)")
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
    scope: str = Field(
        default="default", description="Visibility/activation scope: 'personal', 'group', or 'default' (global)"
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
    scope: str = Field(description="Visibility/activation scope")
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
    scope: str = Field(description="'personal' or 'group'")
    user_id: str | None = None
    group_id: int | None = None
    content_hash: str = Field(description="Pinned content hash at activation time")
    locked: bool = Field(default=False, description="True if managed by config version")
    config_version_id: int | None = None
    activated_at: datetime
    activated_by: str


class SkillActivationWithStatus(BaseModel):
    """Activation record enriched with update-available status and skill metadata."""

    id: int
    sub_agent_id: int
    registry_id: str
    scope: str
    user_id: str | None = None
    group_id: int | None = None
    group_name: str | None = None
    content_hash: str
    locked: bool = False
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
    scope: str = Field(description="'personal' or 'group'")
    group_id: int | None = Field(default=None, description="Group ID (required when scope='group')")


class SkillActivationListResponse(BaseModel):
    """Response for listing activations for an agent."""

    items: list[SkillActivationWithStatus] = Field(default_factory=list)
    total: int = 0


class SkillRegistryEntry(BaseModel):
    """A full registry entry for authoring/editing views."""

    id: str = Field(description="UUID")
    name: str
    slug: str
    description: str | None = None
    source_type: str
    source_repo: str | None = None
    source_ref: str | None = None
    source_path: str | None = None
    files: list[SkillFile] = Field(default_factory=list)
    content_hash: str
    visibility: str = Field(description="'private', 'group', or 'public'")
    owner_id: str | None = None
    group_ids: list[int] = Field(default_factory=list)
    security_verdict: str | None = None
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SkillRegistryCreateRequest(BaseModel):
    """Request to create a new skill in the registry."""

    name: str = Field(description="Skill name (will be slugified)")
    description: str = Field(default="", description="What the skill does")
    files: list[SkillFile] = Field(description="Skill files (must include SKILL.md content)")
    visibility: str = Field(default="private", description="'private', 'group', or 'public'")
    group_ids: list[int] = Field(
        default_factory=list, description="Groups that can see this skill (when visibility='group')"
    )


class SkillRegistryUpdateRequest(BaseModel):
    """Request to update a skill in the registry."""

    description: str | None = Field(default=None, description="Updated description")
    files: list[SkillFile] | None = Field(default=None, description="Updated files (replaces all)")
