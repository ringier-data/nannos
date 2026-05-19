"""Three-tier skill resolution: personal > group > default."""

from __future__ import annotations

import logging

from langgraph.store.postgres.aio import AsyncPostgresStore

from agent_common.core.playbook_reader import PlaybookReaderService
from agent_common.models.skill import ResolvedSkill, SkillDefinition

logger = logging.getLogger(__name__)


async def resolve_skills_for_agent(
    store: AsyncPostgresStore,
    user_id: str,
    agent_name: str,
    group_ids: list[str],
    default_skills: list[SkillDefinition],
) -> dict[str, ResolvedSkill]:
    """Resolve skills from all three tiers, applying override semantics.

    Resolution order: personal > group > default.
    A personal skill with the same name as a default skill overrides it.

    Args:
        store: Document store for reading personal/group skills
        user_id: User's stable database ID
        agent_name: Sub-agent name
        group_ids: Group IDs for group-scoped skills
        default_skills: Immutable skills from sub-agent config

    Returns:
        Dict mapping skill name -> ResolvedSkill (overrides applied)
    """
    resolved: dict[str, ResolvedSkill] = {}

    # 1. Start with default skills (lowest priority)
    default_names = set()
    for skill in default_skills:
        default_names.add(skill.name)
        resolved[skill.name] = ResolvedSkill(
            name=skill.name,
            description=skill.description,
            body=skill.body,
            scope="default",
            files=skill.files,
        )

    # 2. Read personal + group skills from docstore via PlaybookReaderService
    reader = PlaybookReaderService(store)
    docstore_skills = await reader.list_skills(
        user_id=user_id,
        agent_name=agent_name,
        group_ids=group_ids,
    )

    # 3. Apply overrides: group skills override default, personal overrides both
    for entry in docstore_skills:
        # Read full skill content
        content = await reader.read_skill(
            user_id=user_id,
            agent_name=agent_name,
            skill_name=entry.name,
            group_ids=group_ids,
            scope=entry.scope,
        )
        if not content:
            continue

        # Load bundled files for this skill
        skill_files = await reader.read_skill_files(
            user_id=user_id,
            agent_name=agent_name,
            skill_name=entry.name,
            group_ids=group_ids,
            scope=entry.scope,
        )

        overrides = "default" if entry.name in default_names else None
        resolved[entry.name] = ResolvedSkill(
            name=entry.name,
            description=entry.description,
            body=content,
            scope=entry.scope,
            files=skill_files,
            overrides=overrides,
        )

    logger.debug(
        "Resolved %d skills for agent %s (user=%s): %s",
        len(resolved),
        agent_name,
        user_id,
        list(resolved.keys()),
    )
    return resolved
