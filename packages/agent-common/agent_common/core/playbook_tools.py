"""Playbook tools for reading and updating AGENTS.md and SKILLS.md files.

Provides four tools:
- fetch_skill: Read-only tool to load a skill file on-demand
- update_agents_md: HITL-guarded tool to update AGENTS.md
- create_skill_md: HITL-guarded tool to create a new skill file
- update_skill_md: HITL-guarded tool to update an existing skill file

The three write tools are designed to be registered in HITL_GUARDED_TOOLS
so the user is always prompted for approval before changes are persisted.
"""

import logging
from typing import Any, Literal, Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.store.postgres.aio import AsyncPostgresStore
from pydantic import BaseModel, Field

from agent_common.core.playbook_reader import PlaybookReaderService
from agent_common.core.skill_frontmatter import build_skill_content, validate_skill_name

logger = logging.getLogger(__name__)


# --- Input schemas ---


class ListSkillsInput(BaseModel):
    """Input schema for list_skills tool (no parameters)."""

    pass


class FetchSkillInput(BaseModel):
    """Input schema for fetch_skill tool."""

    skill_name: str = Field(
        ...,
        description="Name of the skill to fetch (without .md extension). See available_skills in system prompt or use list_skills.",
    )
    scope: Literal["personal", "group", "auto"] = Field(
        default="auto",
        description="Scope to search: 'personal' (user only), 'group' (group only), 'auto' (personal first, fallback to group)",
    )


class UpdateAgentsMdInput(BaseModel):
    """Input schema for update_agents_md tool."""

    agent_name: str = Field(
        ...,
        description="Name of the agent whose AGENTS.md to update (e.g., 'orchestrator', 'data-analyst')",
    )
    scope: Literal["personal", "group"] = Field(
        default="personal",
        description="Scope: 'personal' (user's private playbook) or 'group' (shared group playbook, requires manager role)",
    )
    section: str = Field(
        ...,
        description="Section heading to update or create (e.g., 'Learned Patterns', 'Workflow Preferences', 'Do / Don't')",
    )
    content: str = Field(
        ...,
        description="New content for the section. Will replace existing section content or create a new section.",
    )
    reason: str = Field(
        ...,
        description="Brief explanation of why this update is being proposed (shown to user in approval prompt)",
    )


class CreateSkillMdInput(BaseModel):
    """Input schema for create_skill_md tool."""

    agent_name: str = Field(
        ...,
        description="Name of the agent this skill belongs to (e.g., 'orchestrator', 'data-analyst')",
    )
    scope: Literal["personal", "group"] = Field(
        default="personal",
        description="Scope: 'personal' or 'group' (requires manager role)",
    )
    skill_name: str = Field(
        ...,
        description="Short identifier for the skill (lowercase, hyphens only). E.g., 'incident-triage', 'weekly-report'",
    )
    description: str = Field(
        ...,
        description="Description of what this skill does and when to use it (1-1024 chars, shown in skill index)",
    )
    body: str = Field(
        ...,
        description="Full skill instructions: workflow steps, examples, best practices. Use Markdown formatting.",
    )
    reason: str = Field(
        ...,
        description="Brief explanation of why this skill is being created (shown to user in approval prompt)",
    )


class UpdateSkillMdInput(BaseModel):
    """Input schema for update_skill_md tool."""

    agent_name: str = Field(
        ...,
        description="Name of the agent this skill belongs to",
    )
    scope: Literal["personal", "group"] = Field(
        default="personal",
        description="Scope: 'personal' or 'group' (requires manager role)",
    )
    skill_name: str = Field(
        ...,
        description="Identifier of the skill to update (the filename without .md)",
    )
    content: str = Field(
        ...,
        description="Complete new content for the skill file (replaces entire file)",
    )
    reason: str = Field(
        ...,
        description="Brief explanation of why this update is being proposed (shown to user in approval prompt)",
    )


# --- Tool implementations ---


def _get_config_metadata(runtime: Any) -> dict:
    """Extract config metadata from tool runtime, with fallback to LangChain context.

    When ToolRuntime injection works (tool has user-facing parameters),
    extracts metadata from runtime.config. When injection fails (e.g. for
    tools with no user-facing args like list_skills), falls back to
    reading the RunnableConfig from LangChain's context variable.

    Args:
        runtime: ToolRuntime (may be None if injection didn't work)

    Returns:
        Metadata dict from the config
    """
    if runtime and hasattr(runtime, "config"):
        return runtime.config.get("metadata", {})
    # Fallback: read from LangChain's context variable
    try:
        from langchain_core.runnables.config import ensure_config

        config = ensure_config()
        return config.get("metadata", {})
    except Exception:
        return {}


def _get_user_id_and_group_id(runtime: Any) -> tuple[str | None, str | None]:
    """Extract user_id and group_id from tool runtime metadata.

    Args:
        runtime: ToolRuntime providing config metadata

    Returns:
        Tuple of (user_id, group_id)
    """
    metadata = _get_config_metadata(runtime)
    user_id = metadata.get("user_id")
    group_id = metadata.get("group_id")
    return user_id, group_id


def _get_group_ids(runtime: Any) -> list[str] | None:
    """Extract all group IDs from tool runtime metadata.

    Falls back to single group_id wrapped in a list for backward compatibility.

    Args:
        runtime: ToolRuntime providing config metadata

    Returns:
        List of group IDs, or None if no groups available
    """
    metadata = _get_config_metadata(runtime)
    group_ids = metadata.get("group_ids")
    if group_ids:
        return group_ids
    # Fallback to single group_id
    group_id = metadata.get("group_id")
    return [group_id] if group_id else None


def _build_file_path(agent_name: str, file_name: str) -> str:
    """Build the store key for a playbook file.

    Args:
        agent_name: Agent name
        file_name: Relative file name (e.g., "AGENTS.md" or "skills/incident_triage.md")

    Returns:
        Store key path
    """
    return f"/{agent_name}/{file_name}"


def _get_namespace(scope: str, user_id: str | None, group_id: str | None) -> tuple[str, str]:
    """Get the store namespace for the given scope.

    Args:
        scope: "personal" or "group"
        user_id: User ID (for personal scope)
        group_id: Group ID (for group scope)

    Returns:
        Namespace tuple
    """
    if scope == "group":
        if not group_id:
            raise ValueError("group_id is required for group-scoped operations")
        return (str(group_id), "agent-data")
    if not user_id:
        raise ValueError("user_id is required for personal-scoped operations")
    return (user_id, "agent-data")


def _update_section_in_markdown(existing_content: str | None, section: str, new_content: str) -> str:
    """Update or append a section in a Markdown document.

    If the section heading exists, replaces its content up to the next heading.
    If not, appends the section at the end.

    Args:
        existing_content: Current file content (or None for new file)
        section: Section heading (without ##)
        new_content: New content for the section

    Returns:
        Updated markdown content
    """
    section_heading = f"## {section}"

    if not existing_content:
        return f"# AGENTS.md\n\n{section_heading}\n\n{new_content}\n"

    lines = existing_content.split("\n")
    result_lines: list[str] = []
    in_target_section = False
    section_found = False

    for line in lines:
        if line.strip().startswith("## "):
            if in_target_section:
                # End of target section — insert new content and continue
                result_lines.append(section_heading)
                result_lines.append("")
                result_lines.append(new_content)
                result_lines.append("")
                in_target_section = False
            if line.strip() == section_heading:
                in_target_section = True
                section_found = True
                continue
            result_lines.append(line)
        elif not in_target_section:
            result_lines.append(line)
        # Skip lines within the target section (they'll be replaced)

    # If we were still in the target section at EOF
    if in_target_section:
        result_lines.append(section_heading)
        result_lines.append("")
        result_lines.append(new_content)
        result_lines.append("")

    # If section wasn't found, append it
    if not section_found:
        result_lines.append("")
        result_lines.append(section_heading)
        result_lines.append("")
        result_lines.append(new_content)
        result_lines.append("")

    return "\n".join(result_lines)


# --- Tool factories ---


def create_playbook_tools(store: AsyncPostgresStore) -> list[BaseTool]:
    """Create all playbook tools.

    Args:
        store: AsyncPostgresStore for reading/writing playbook files

    Returns:
        List of playbook tools [fetch_skill, update_agents_md, create_skill_md, update_skill_md]
    """
    reader = PlaybookReaderService(store)

    async def fetch_skill_handler(
        skill_name: str,
        scope: str = "auto",
        runtime: Optional[ToolRuntime] = None,
    ) -> str:
        """Fetch a skill file for detailed workflow instructions.

        Use this when you need guidance for a complex multi-step workflow
        listed in <available_skills>. Returns the full skill content.

        Args:
            skill_name: Name of the skill (from available_skills index)
            scope: "personal", "group", or "auto" (tries personal first)
            runtime: Tool runtime (injected)

        Returns:
            Skill content or error message
        """
        user_id, group_id = _get_user_id_and_group_id(runtime)
        if not user_id:
            return "Error: Could not determine user identity. Cannot fetch skill."

        # Determine agent_name from runtime context
        # Skills are loaded relative to the current agent
        agent_name = runtime.config.get("metadata", {}).get("agent_name", "orchestrator") if runtime else "orchestrator"

        group_ids = _get_group_ids(runtime)
        content = await reader.read_skill(
            user_id=user_id,
            agent_name=agent_name,
            skill_name=skill_name,
            group_ids=group_ids,
            scope=scope,
        )

        if content:
            return content
        return f"Skill '{skill_name}' not found in {scope} scope. Check available_skills for valid names."

    async def update_agents_md_handler(
        agent_name: str,
        scope: str = "personal",
        section: str = "",
        content: str = "",
        reason: str = "",
        runtime: Optional[ToolRuntime] = None,
    ) -> str:
        """Update the AGENTS.md playbook for an agent.

        Proposes changes to the agent's behavioral playbook. The change requires
        user approval via human-in-the-loop before being saved.

        Updates or creates a section in the AGENTS.md file. The playbook will
        take effect from your next message.

        Args:
            agent_name: Agent to update (e.g., 'orchestrator', 'data-analyst')
            scope: 'personal' or 'group'
            section: Section heading to update/create
            content: New section content
            reason: Why this update is proposed
            runtime: Tool runtime (injected)

        Returns:
            Success/error message
        """
        user_id, group_id = _get_user_id_and_group_id(runtime)
        if not user_id:
            return "Error: Could not determine user identity."

        if scope == "group" and not group_id:
            return "Error: No group context available. Cannot update group playbook."

        try:
            namespace = _get_namespace(scope, user_id, group_id)
            file_path = _build_file_path(agent_name, "AGENTS.md")

            # Read existing content
            existing = await reader._read_file_from_store(namespace=namespace, file_path=file_path)

            # Build updated content
            updated = _update_section_in_markdown(existing, section, content)

            # Write to store
            await store.aput(
                namespace=namespace,
                key=file_path,
                value={"content": updated},
            )

            logger.info(f"Updated AGENTS.md for agent '{agent_name}' (scope={scope}, section='{section}')")
            return (
                f"Successfully updated section '{section}' in {scope} AGENTS.md for {agent_name}. "
                f"Reason: {reason}. The updated playbook will take effect from your next message."
            )
        except Exception as e:
            logger.error(f"Failed to update AGENTS.md: {e}", exc_info=True)
            return f"Error updating AGENTS.md: {str(e)}"

    async def create_skill_md_handler(
        agent_name: str,
        scope: str = "personal",
        skill_name: str = "",
        description: str = "",
        body: str = "",
        reason: str = "",
        runtime: Optional[ToolRuntime] = None,
    ) -> str:
        """Create a new skill file for an agent following the SKILL.md spec.

        Creates a new skill with YAML frontmatter (name + description) and
        a Markdown body with workflow instructions. The skill will appear in
        available_skills and can be fetched on-demand.
        Requires user approval via human-in-the-loop.

        Args:
            agent_name: Agent this skill belongs to
            scope: 'personal' or 'group'
            skill_name: Short identifier (lowercase, hyphens only)
            description: What the skill does and when to use it
            body: Full workflow instructions
            reason: Why this skill is being created
            runtime: Tool runtime (injected)

        Returns:
            Success/error message
        """
        user_id, group_id = _get_user_id_and_group_id(runtime)
        if not user_id:
            return "Error: Could not determine user identity."

        if scope == "group" and not group_id:
            return "Error: No group context available. Cannot create group skill."

        # Validate skill name per SKILL.md spec
        name_error = validate_skill_name(skill_name)
        if name_error:
            return f"Error: {name_error}"

        try:
            namespace = _get_namespace(scope, user_id, group_id)
            file_path = _build_file_path(agent_name, f"skills/{skill_name}.md")

            # Check if skill already exists
            existing = await reader._read_file_from_store(namespace=namespace, file_path=file_path)
            if existing:
                return f"Error: Skill '{skill_name}' already exists. Use update_skill_md to modify it."

            # Build skill content with YAML frontmatter
            skill_content = build_skill_content(
                name=skill_name,
                description=description,
                body=body,
            )

            # Write to store
            await store.aput(
                namespace=namespace,
                key=file_path,
                value={"content": skill_content},
            )

            logger.info(f"Created skill '{skill_name}' for agent '{agent_name}' (scope={scope})")
            return (
                f"Successfully created skill '{skill_name}' for {agent_name} ({scope} scope). "
                f"Reason: {reason}. The skill will be available to fetch from your next message."
            )
        except Exception as e:
            logger.error(f"Failed to create skill: {e}", exc_info=True)
            return f"Error creating skill: {str(e)}"

    async def update_skill_md_handler(
        agent_name: str,
        scope: str = "personal",
        skill_name: str = "",
        content: str = "",
        reason: str = "",
        runtime: Optional[ToolRuntime] = None,
    ) -> str:
        """Update an existing skill file.

        Replaces the entire content of a skill file. Requires user approval.

        Args:
            agent_name: Agent this skill belongs to
            scope: 'personal' or 'group'
            skill_name: Skill to update
            content: Complete new content (replaces entire file)
            reason: Why this update is proposed
            runtime: Tool runtime (injected)

        Returns:
            Success/error message
        """
        user_id, group_id = _get_user_id_and_group_id(runtime)
        if not user_id:
            return "Error: Could not determine user identity."

        if scope == "group" and not group_id:
            return "Error: No group context available. Cannot update group skill."

        try:
            namespace = _get_namespace(scope, user_id, group_id)
            file_path = _build_file_path(agent_name, f"skills/{skill_name}.md")

            # Check skill exists
            existing = await reader._read_file_from_store(namespace=namespace, file_path=file_path)
            if not existing:
                return f"Error: Skill '{skill_name}' not found. Use create_skill_md to create it."

            # Write updated content
            await store.aput(
                namespace=namespace,
                key=file_path,
                value={"content": content},
            )

            logger.info(f"Updated skill '{skill_name}' for agent '{agent_name}' (scope={scope})")
            return (
                f"Successfully updated skill '{skill_name}' for {agent_name} ({scope} scope). "
                f"Reason: {reason}. Changes will take effect from your next message."
            )
        except Exception as e:
            logger.error(f"Failed to update skill: {e}", exc_info=True)
            return f"Error updating skill: {str(e)}"

    async def list_skills_handler(
        runtime: Optional[ToolRuntime] = None,
    ) -> str:
        """List all available skills for the current agent.

        Returns a formatted list of skills with their names, scopes, and descriptions.

        Args:
            runtime: Tool runtime (injected)

        Returns:
            Formatted skill list or message if none found
        """
        user_id, _ = _get_user_id_and_group_id(runtime)
        if not user_id:
            return "Error: Could not determine user identity."

        agent_name = runtime.config.get("metadata", {}).get("agent_name", "orchestrator") if runtime else "orchestrator"
        group_ids = _get_group_ids(runtime)

        try:
            skills = await reader.list_skills(
                user_id=user_id,
                agent_name=agent_name,
                group_ids=group_ids,
            )
        except Exception as e:
            logger.error(f"Failed to list skills: {e}", exc_info=True)
            return f"Error listing skills: {str(e)}"

        if not skills:
            return "No skills are currently defined. You can create one with the create_skill_md tool."

        lines = [f"- `{s.name}` ({s.scope}): {s.description}" for s in skills]
        return "Available skills:\n" + "\n".join(lines)

    # Build tool objects
    list_skills_tool = StructuredTool.from_function(
        coroutine=list_skills_handler,
        name="list_skills",
        description=(
            "List all available skills (playbook workflows) for the current agent. "
            "Returns skill names, descriptions, and scopes (personal/group). "
            "Use this to discover what skills exist before fetching one with fetch_skill."
        ),
        args_schema=ListSkillsInput,
    )

    fetch_skill_tool = StructuredTool.from_function(
        coroutine=fetch_skill_handler,
        name="fetch_skill",
        description=(
            "Fetch a skill's full workflow instructions. Use when you need detailed guidance "
            "for a complex multi-step workflow listed in <available_skills>. "
            "Returns the complete skill content with steps, examples, and best practices."
        ),
        args_schema=FetchSkillInput,
    )

    update_agents_md_tool = StructuredTool.from_function(
        coroutine=update_agents_md_handler,
        name="update_agents_md",
        description=(
            "Update the agent's behavioral playbook (AGENTS.md). Use this to save learned "
            "preferences, patterns, or instructions that should persist across conversations. "
            "Requires user approval before saving. Changes take effect from the next message."
        ),
        args_schema=UpdateAgentsMdInput,
    )

    create_skill_md_tool = StructuredTool.from_function(
        coroutine=create_skill_md_handler,
        name="create_skill_md",
        description=(
            "Create a new skill file documenting a complex multi-step workflow. "
            "Use this when you've learned a repeatable process that should be captured "
            "for future reference. Requires user approval. Available via fetch_skill after creation."
        ),
        args_schema=CreateSkillMdInput,
    )

    update_skill_md_tool = StructuredTool.from_function(
        coroutine=update_skill_md_handler,
        name="update_skill_md",
        description=(
            "Update an existing skill file with improved workflow instructions. "
            "Use when a skill's steps, examples, or best practices need refinement. "
            "Requires user approval. Changes take effect from the next message."
        ),
        args_schema=UpdateSkillMdInput,
    )

    return [list_skills_tool, fetch_skill_tool, update_agents_md_tool, create_skill_md_tool, update_skill_md_tool]
