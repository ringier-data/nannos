"""Pydantic models for the Playbook API."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Activation scope: where the skill is delivered at runtime.
#   - personal: only the activating user sees it (highest priority)
#   - group: shared with all group members (middle priority)
#   - default: baked into sub-agent config, visible to all users (lowest priority)
ActivationScope = Literal["personal", "group", "default"]

# Playbook scope: playbooks only support personal/group (no default tier).
PlaybookScope = Literal["personal", "group"]


class PlaybookContent(BaseModel):
    """AGENTS.md content for an agent."""

    agent_name: str
    scope: PlaybookScope = Field(description="'personal' or 'group'")
    content: str | None = Field(default=None, description="Markdown content of the AGENTS.md file")
    group_id: str | None = Field(default=None, description="Group ID (present for group scope)")
    group_name: str | None = Field(default=None, description="Group display name (present for group scope)")


class PlaybookUpdate(BaseModel):
    """Request body for updating AGENTS.md."""

    content: str = Field(description="Full Markdown content to write")


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
    scope: ActivationScope = Field(description="'personal', 'group', or 'default'")
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


class PlaybookListResponse(BaseModel):
    """Response for listing playbooks."""

    personal: PlaybookContent | None = None
    groups: list[PlaybookContent] = Field(default_factory=list, description="Playbooks from all user groups")


class SkillListResponse(BaseModel):
    """Response for listing skills."""

    items: list[SkillSummary] = Field(default_factory=list)


# --- MCP Tool Request/Response Models ---


class McpSkillCreate(BaseModel):
    """Request body for the console_create_skill MCP tool.

    Creates a skill in the registry and auto-activates it on the calling agent.
    The skill is immediately usable after creation.
    """

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    scope: ActivationScope = Field(
        description="Activation scope: 'personal' (user-only), 'group' (shared with group), or 'default' (baked into sub-agent config for all users)",
    )
    skill_name: str = Field(description="Skill identifier (lowercase letters, numbers, hyphens only)")
    description: str = Field(default="", description="What the skill does and when to use it")
    body: str = Field(description="Skill instructions body (Markdown)")
    files: list[McpSkillFile] | None = Field(default=None, description="Optional files to bundle with the skill")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    sub_agent_id: int | None = Field(
        default=None, description="Sub-agent ID (resolved from agent_name if not provided)"
    )
    visibility: str = Field(
        default="private",
        description="Registry visibility: 'private' (only you), 'group' (your group), or 'public' (everyone)",
    )


class McpSkillUpdate(BaseModel):
    """Request body for the console_update_skill MCP tool.

    Updates a skill in the registry and auto-refreshes the calling agent's
    own activation. Other consumers' activations remain pinned.
    """

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    scope: ActivationScope = Field(
        description="Scope of the skill to update: 'personal', 'group', or 'default'",
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
    sub_agent_id: int | None = Field(
        default=None, description="Sub-agent ID (resolved from agent_name if not provided)"
    )
    registry_id: str | None = Field(default=None, description="Registry entry UUID (alternative to skill_name lookup)")


class McpSkillRemove(BaseModel):
    """Request body for the console_remove_skill MCP tool.

    Deactivates a skill from the calling agent. The registry entry is preserved
    for other consumers.
    """

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    scope: ActivationScope = Field(description="Scope of the skill to remove: 'personal', 'group', or 'default'")
    skill_name: str = Field(description="Skill identifier to deactivate")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    sub_agent_id: int | None = Field(
        default=None, description="Sub-agent ID (resolved from agent_name if not provided)"
    )


class McpPlaybookUpdate(BaseModel):
    """Request body for the console_update_playbook MCP tool.

    Updates the AGENTS.md playbook for an agent. For section-based updates,
    provide section_name and content. For full replacement, provide content only.
    """

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    scope: PlaybookScope = Field(description="Scope: 'personal' or 'group'")
    content: str = Field(description="Full Markdown content to write")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")


class McpSkillWriteFile(BaseModel):
    """Request body for the console_write_skill_file MCP tool."""

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    scope: ActivationScope = Field(description="Scope of the skill: 'personal', 'group', or 'default'")
    skill_name: str = Field(description="Skill identifier that the file belongs to")
    file_path: str = Field(description="Relative path within the skill folder (e.g., 'scripts/check.py')")
    content: str = Field(description="File content (text)")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    sub_agent_id: int | None = Field(
        default=None, description="Sub-agent ID (resolved from agent_name if not provided)"
    )


class McpSkillDeleteFile(BaseModel):
    """Request body for the console_delete_skill_file MCP tool."""

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    scope: ActivationScope = Field(description="Scope of the skill: 'personal', 'group', or 'default'")
    skill_name: str = Field(description="Skill identifier that the file belongs to")
    file_path: str = Field(description="Relative path of the file to delete (e.g., 'scripts/check.py')")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    sub_agent_id: int | None = Field(
        default=None, description="Sub-agent ID (resolved from agent_name if not provided)"
    )


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

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    registry_id: str | None = Field(default=None, description="Registry entry UUID to activate")
    skill_name: str | None = Field(
        default=None, description="Skill name to search in registry (alternative to registry_id)"
    )
    scope: ActivationScope = Field(
        default="personal", description="Activation scope: 'personal', 'group', or 'default'"
    )
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    sub_agent_id: int | None = Field(
        default=None, description="Sub-agent ID (resolved from agent_name if not provided)"
    )
