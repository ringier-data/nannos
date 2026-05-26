"""Pydantic models for the Playbook API."""

from typing import Literal

from pydantic import BaseModel, Field

# Playbook scope: playbooks only support personal/group (no sub-agent tier).
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


class PlaybookListResponse(BaseModel):
    """Response for listing playbooks."""

    personal: PlaybookContent | None = None
    groups: list[PlaybookContent] = Field(default_factory=list, description="Playbooks from all user groups")


class McpPlaybookUpdate(BaseModel):
    """Request body for the console_update_playbook MCP tool.

    Updates the AGENTS.md playbook for an agent. For section-based updates,
    provide section_name and content. For full replacement, provide content only.
    """

    agent_name: str = Field(
        default="self",
        description="Target sub-agent name. Defaults to 'self' (the calling agent).",
    )
    scope: PlaybookScope = Field(description="Scope: 'personal' or 'group'")
    content: str = Field(description="Full Markdown content to write")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
