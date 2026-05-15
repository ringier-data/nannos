"""Pydantic models for the Skills Registry API.

Handles discovery and import of skills from external sources (skills.sh, GitHub).
"""

from pydantic import BaseModel, Field


class SkillFile(BaseModel):
    """A single file within a skill (SKILL.md, examples, etc.)."""

    path: str = Field(description="Relative file path within the skill directory")
    contents: str = Field(description="Full text content of the file")


class SkillSearchResult(BaseModel):
    """A skill found via search or browse.

    Matches the V1Skill shape from skills.sh API.
    """

    id: str = Field(description="Stable unique identifier. Format: '{source}/{slug}'")
    slug: str = Field(description="URL-safe skill slug (e.g. 'next-js-development')")
    name: str = Field(description="Human-readable name (e.g. 'Next.js Development')")
    source: str = Field(description="Source repository or provider (e.g. 'vercel-labs/agent-skills')")
    installs: int = Field(default=0, description="Total deduplicated install count")
    source_type: str = Field(default="github", description="'github' or 'well-known'")
    install_url: str | None = Field(default=None, description="GitHub URL or well-known base URL for installing")
    url: str | None = Field(default=None, description="Direct link to the skill's page on skills.sh")


class SkillSearchResponse(BaseModel):
    """Response for skill search/browse."""

    data: list[SkillSearchResult] = Field(default_factory=list)
    query: str = Field(default="")
    search_type: str | None = Field(default=None, description="'fuzzy' or 'semantic' (from skills.sh search)")
    count: int = Field(default=0, description="Number of results returned")


class SkillDetailResponse(BaseModel):
    """Response from skills.sh detail endpoint (GET /api/v1/skills/:source/:skill)."""

    id: str = Field(description="Stable unique identifier")
    source: str = Field(description="Source repository or provider")
    slug: str = Field(description="URL-safe skill slug")
    installs: int = Field(default=0, description="Total deduplicated install count")
    hash: str | None = Field(default=None, description="SHA-256 hash of file contents for cache invalidation")
    files: list[SkillFile] | None = Field(default=None, description="All files in the skill folder")


class GitHubSkillDetail(BaseModel):
    """Skill detail fetched from GitHub (via tree/contents API)."""

    files: list[SkillFile] = Field(description="All files in the skill directory")
    tree_sha: str | None = Field(default=None, description="Git tree SHA for the skill directory")


class SkillAuditEntry(BaseModel):
    """A single security audit from a partner."""

    provider: str = Field(description="Partner display name (e.g. 'Gen Agent Trust Hub', 'Socket')")
    slug: str = Field(description="URL-safe partner slug")
    status: str = Field(description="Normalized verdict: 'pass', 'warn', or 'fail'")
    summary: str = Field(description="One-line human-readable summary")
    audited_at: str = Field(description="ISO 8601 timestamp of audit")
    risk_level: str | None = Field(default=None, description="'NONE', 'LOW', 'MEDIUM', 'HIGH', or 'CRITICAL'")
    categories: list[str] | None = Field(default=None, description="Detected categories (Agent Trust Hub only)")


class SkillAuditResponse(BaseModel):
    """Response from skills.sh audit endpoint (GET /api/v1/skills/audit/:source/:skill)."""

    id: str = Field(description="Stable unique identifier")
    source: str = Field(description="Source repository or provider")
    slug: str = Field(description="URL-safe skill slug")
    audits: list[SkillAuditEntry] = Field(default_factory=list, description="Security audit results")


class SkillImportRequest(BaseModel):
    """Request body for importing a skill from an external source."""

    id: str | None = Field(
        default=None,
        description=(
            "skills.sh skill ID in format '{source}/{slug}' "
            "(e.g. 'vercel-labs/agent-skills/next-js-development'). "
            "Stable across requests — used to track installs and detect duplicates. "
            "The slug portion becomes the stored skill name unless 'skill' is also provided."
        ),
    )
    repo: str | None = Field(
        default=None,
        description="GitHub repo shorthand (e.g. 'OthmanAdi/planning-with-files'). Fallback source.",
    )
    skill: str | None = Field(
        default=None,
        description="Skill name within the repo (required when using 'repo' source).",
    )
    agent: str = Field(description="Target sub-agent name")
    scope: str = Field(default="personal", description="Target scope: 'personal', 'group', or 'default'")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    overwrite: bool = Field(default=False, description="Overwrite existing skill with same name")
    force: bool = Field(
        default=False, description="Force import even if security assessment fails (requires approver role)"
    )


class SkillSourceInfo(BaseModel):
    """Provenance metadata stored with imported skills."""

    id: str | None = Field(default=None, description="skills.sh skill ID")
    repo: str | None = Field(default=None, description="GitHub repo (owner/repo)")
    hash: str | None = Field(default=None, description="Content hash (skills.sh SHA-256 or git tree SHA)")
    imported_at: str = Field(description="ISO 8601 timestamp of import")


class SkillImportResponse(BaseModel):
    """Response after successful skill import."""

    name: str = Field(description="Imported skill name")
    agent: str = Field(description="Target sub-agent name")
    scope: str = Field(description="Scope where skill was stored")
    source: SkillSourceInfo = Field(description="Provenance metadata")


class SkillBrowseRequest(BaseModel):
    """Query parameters for browsing a specific GitHub repo."""

    repo: str = Field(description="GitHub repo shorthand (e.g. 'anthropics/skills')")
    ref: str = Field(default="main", description="Git ref (branch, tag, or commit SHA)")
