"""Playbook service for reading/writing AGENTS.md and skill files.

Queries the LangGraph store table in the docstore database directly using
SQLAlchemy, avoiding the need for a full langgraph dependency.

Store table schema:
  - prefix TEXT: namespace tuple joined by "." (e.g., "user123.playbooks")
  - key TEXT: file path (e.g., "/orchestrator/AGENTS.md")
  - value JSONB: {"content": "...markdown..."}

Skill files follow the SKILL.md spec (https://agentskills.io/specification):
  - YAML frontmatter with required `name` and `description` fields
  - Markdown body with instructions
"""

import logging
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class PlaybookService:
    """Service for managing playbook files in the LangGraph store."""

    def __init__(self) -> None:
        self._db_session_factory: async_sessionmaker[AsyncSession] | None = None

    def set_db_session_factory(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Set the docstore database session factory."""
        self._db_session_factory = factory

    @property
    def is_available(self) -> bool:
        """Check if the playbook service is configured and available."""
        return self._db_session_factory is not None

    def _get_prefix(self, scope: str, user_id: str, group_id: str | None) -> str:
        """Build the store prefix from scope and IDs.

        LangGraph stores namespace tuples as dot-joined strings.
        e.g., ("user123", "agent-data") → "user123.agent-data"
        """
        if scope == "group":
            if not group_id:
                raise ValueError("group_id required for group scope")
            return f"{group_id}.agent-data"
        return f"{user_id}.agent-data"

    def _agents_md_key(self, agent_name: str, scope: str, group_id: str | None) -> str:
        """Build the store key for an AGENTS.md file."""
        return f"/{agent_name}/AGENTS.md"

    def _skill_key(self, agent_name: str, scope: str, group_id: str | None, skill_name: str) -> str:
        """Build the store key for a skill file."""
        return f"/{agent_name}/skills/{skill_name}.md"

    def _skills_prefix_key(self, agent_name: str, scope: str, group_id: str | None) -> str:
        """Build the key prefix for listing skills."""
        return f"/{agent_name}/skills/"

    async def get_agents_md(self, user_id: str, agent_name: str, scope: str, group_id: str | None = None) -> str | None:
        """Read AGENTS.md content for a given agent and scope.

        Returns None if the file doesn't exist.
        """
        if not self._db_session_factory:
            return None

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._agents_md_key(agent_name, scope, group_id)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("SELECT value FROM store WHERE prefix = :prefix AND key = :key"),
                {"prefix": prefix, "key": key},
            )
            row = result.first()
            if row and row[0]:
                return self._extract_content(row[0])
        return None

    async def put_agents_md(
        self, user_id: str, agent_name: str, scope: str, content: str, group_id: str | None = None
    ) -> None:
        """Write AGENTS.md content for a given agent and scope."""
        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._agents_md_key(agent_name, scope, group_id)

        async with self._db_session_factory() as db:
            await db.execute(
                text("""
                    INSERT INTO store (prefix, key, value, created_at, updated_at)
                    VALUES (:prefix, :key, :value, NOW(), NOW())
                    ON CONFLICT (prefix, key)
                    DO UPDATE SET value = :value, updated_at = NOW()
                """),
                {"prefix": prefix, "key": key, "value": _to_jsonb(content)},
            )
            await db.commit()

    async def delete_agents_md(self, user_id: str, agent_name: str, scope: str, group_id: str | None = None) -> bool:
        """Delete AGENTS.md for a given agent and scope. Returns True if deleted."""
        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._agents_md_key(agent_name, scope, group_id)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("DELETE FROM store WHERE prefix = :prefix AND key = :key"),
                {"prefix": prefix, "key": key},
            )
            await db.commit()
            return result.rowcount > 0

    async def list_skills(
        self, user_id: str, agent_name: str, scope: str, group_id: str | None = None
    ) -> list[dict[str, str]]:
        """List all skill files for an agent in the given scope.

        Returns list of dicts with 'name', 'title', 'description', 'scope'.
        """
        if not self._db_session_factory:
            return []

        prefix = self._get_prefix(scope, user_id, group_id)
        key_prefix = self._skills_prefix_key(agent_name, scope, group_id)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("SELECT key, value FROM store WHERE prefix = :prefix AND key LIKE :pattern"),
                {"prefix": prefix, "pattern": f"{key_prefix}%"},
            )
            rows = result.all()

        skills = []
        for row in rows:
            key = row[0]
            content = self._extract_content(row[1])
            if not content:
                continue

            # Extract skill name from key path
            skill_name = key.removeprefix(key_prefix).removesuffix(".md")
            if not skill_name or "/" in skill_name:
                continue

            title, description = self._extract_title_and_description(content)
            skills.append(
                {
                    "name": skill_name,
                    "title": title or skill_name,
                    "description": description,
                    "scope": scope,
                }
            )

        return skills

    async def get_skill(
        self, user_id: str, agent_name: str, skill_name: str, scope: str, group_id: str | None = None
    ) -> str | None:
        """Read a skill file's content. Returns None if not found."""
        if not self._db_session_factory:
            return None

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._skill_key(agent_name, scope, group_id, skill_name)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("SELECT value FROM store WHERE prefix = :prefix AND key = :key"),
                {"prefix": prefix, "key": key},
            )
            row = result.first()
            if row and row[0]:
                return self._extract_content(row[0])
        return None

    async def put_skill(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        scope: str,
        content: str,
        group_id: str | None = None,
    ) -> None:
        """Write a skill file."""
        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._skill_key(agent_name, scope, group_id, skill_name)

        async with self._db_session_factory() as db:
            await db.execute(
                text("""
                    INSERT INTO store (prefix, key, value, created_at, updated_at)
                    VALUES (:prefix, :key, :value, NOW(), NOW())
                    ON CONFLICT (prefix, key)
                    DO UPDATE SET value = :value, updated_at = NOW()
                """),
                {"prefix": prefix, "key": key, "value": _to_jsonb(content)},
            )
            await db.commit()

    async def delete_skill(
        self, user_id: str, agent_name: str, skill_name: str, scope: str, group_id: str | None = None
    ) -> bool:
        """Delete a skill file. Returns True if deleted."""
        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._skill_key(agent_name, scope, group_id, skill_name)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("DELETE FROM store WHERE prefix = :prefix AND key = :key"),
                {"prefix": prefix, "key": key},
            )
            await db.commit()
            return result.rowcount > 0

    def _extract_content(self, value: Any) -> str | None:
        """Extract text content from LangGraph store value JSONB."""
        if isinstance(value, dict):
            return value.get("content")
        return None

    def _extract_title_and_description(self, content: str) -> tuple[str, str]:
        """Extract title (name) and description from a skill file.

        Supports both SKILL.md frontmatter format (preferred) and legacy
        markdown heading format (fallback for old skills).

        Args:
            content: Full skill file content

        Returns:
            Tuple of (title/name, description)
        """
        # Try YAML frontmatter first
        stripped = content.strip()
        if stripped.startswith("---"):
            parts = stripped.split("---", 2)
            if len(parts) >= 3:
                try:
                    fm_data = yaml.safe_load(parts[1].strip())
                    if isinstance(fm_data, dict):
                        name = str(fm_data.get("name", ""))
                        description = str(fm_data.get("description", ""))
                        return name, description
                except yaml.YAMLError:
                    pass

        # Legacy: extract from markdown headings
        title = ""
        description = ""
        lines = content.split("\n")

        for line in lines:
            stripped_line = line.strip()
            if not title and stripped_line.startswith("# "):
                title = stripped_line[2:].strip()
            elif title and not description and stripped_line and not stripped_line.startswith("#"):
                description = stripped_line
                break

        return title, description


def _to_jsonb(content: str) -> str:
    """Serialize content to JSON string for PostgreSQL JSONB column."""
    import json

    return json.dumps({"content": content})
