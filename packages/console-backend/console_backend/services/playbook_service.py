"""Playbook service for reading/writing AGENTS.md and skill files.

Queries the LangGraph store table in the docstore database directly using
SQLAlchemy, avoiding the need for a full langgraph dependency.

Store table schema:
  - prefix TEXT: namespace tuple joined by "." (e.g., "user123.agent-data")
  - key TEXT: file path (e.g., "/orchestrator/AGENTS.md")
  - value JSONB: {"content": "...markdown..."}

Skills are stored as folders:
  - SKILL.md: /{agent}/skills/{name}/SKILL.md  (entry point, mandatory)
  - Files:    /{agent}/skills/{name}/scripts/check.py  (bundled assets)

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
        """Build the store key for a skill's SKILL.md file."""
        return f"/{agent_name}/skills/{skill_name}/SKILL.md"

    def _skill_file_key(self, agent_name: str, skill_name: str, file_path: str) -> str:
        """Build the store key for a file within a skill folder."""
        return f"/{agent_name}/skills/{skill_name}/{file_path}"

    def _skill_folder_prefix(self, agent_name: str, skill_name: str) -> str:
        """Build the key prefix for all files within a single skill."""
        return f"/{agent_name}/skills/{skill_name}/"

    def _skills_prefix_key(self, agent_name: str, scope: str, group_id: str | None) -> str:
        """Build the key prefix for listing all skills."""
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
    ) -> list[dict[str, Any]]:
        """List all skills for an agent in the given scope.

        Groups rows by skill folder name. Only SKILL.md rows provide metadata.

        Returns list of dicts with 'name', 'title', 'description', 'scope', 'file_count'.
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

        # Group rows by skill name (second path segment under skills/)
        # Key format: /{agent}/skills/{skill_name}/SKILL.md or /{agent}/skills/{skill_name}/scripts/check.py
        skill_data: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = row[0]
            rel_path = key.removeprefix(key_prefix)
            if not rel_path:
                continue

            # Extract skill name (first segment)
            parts = rel_path.split("/", 1)
            skill_name = parts[0]
            file_name = parts[1] if len(parts) > 1 else ""

            if skill_name not in skill_data:
                skill_data[skill_name] = {"content": None, "file_count": 0}

            if file_name == "SKILL.md":
                content = self._extract_content(row[1])
                skill_data[skill_name]["content"] = content
            else:
                # Count non-SKILL.md files
                skill_data[skill_name]["file_count"] += 1

        skills = []
        for skill_name, data in skill_data.items():
            content = data["content"]
            if not content:
                continue  # Skip skills without SKILL.md

            title, description = self._extract_title_and_description(content)
            skills.append(
                {
                    "name": skill_name,
                    "title": title or skill_name,
                    "description": description,
                    "scope": scope,
                    "file_count": data["file_count"],
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
        """Delete a skill and all its files. Returns True if anything was deleted."""
        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        prefix = self._get_prefix(scope, user_id, group_id)
        folder_prefix = self._skill_folder_prefix(agent_name, skill_name)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("DELETE FROM store WHERE prefix = :prefix AND key LIKE :pattern"),
                {"prefix": prefix, "pattern": f"{folder_prefix}%"},
            )
            await db.commit()
            return result.rowcount > 0

    # ---- File-level operations within a skill folder ----

    async def list_skill_files(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        scope: str,
        group_id: str | None = None,
    ) -> list[str]:
        """List file paths within a skill folder (excluding SKILL.md).

        Returns list of relative paths (e.g., ['scripts/check.py', 'data/template.json']).
        """
        if not self._db_session_factory:
            return []

        prefix = self._get_prefix(scope, user_id, group_id)
        folder_prefix = self._skill_folder_prefix(agent_name, skill_name)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("SELECT key FROM store WHERE prefix = :prefix AND key LIKE :pattern"),
                {"prefix": prefix, "pattern": f"{folder_prefix}%"},
            )
            rows = result.all()

        files = []
        for row in rows:
            rel_path = row[0].removeprefix(folder_prefix)
            if rel_path and rel_path != "SKILL.md":
                files.append(rel_path)
        return sorted(files)

    async def get_skill_file(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        file_path: str,
        scope: str,
        group_id: str | None = None,
    ) -> str | None:
        """Read a single file from a skill folder. Returns None if not found."""
        if not self._db_session_factory:
            return None

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._skill_file_key(agent_name, skill_name, file_path)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("SELECT value FROM store WHERE prefix = :prefix AND key = :key"),
                {"prefix": prefix, "key": key},
            )
            row = result.first()
            if row and row[0]:
                return self._extract_content(row[0])
        return None

    async def put_skill_file(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        file_path: str,
        content: str,
        scope: str,
        group_id: str | None = None,
    ) -> None:
        """Write a single file to a skill folder.

        Validates that the skill exists (SKILL.md present), enforces size and
        count limits, and rejects SKILL.md as file_path.

        Raises:
            ValueError: If constraints are violated.
            RuntimeError: If service is not configured.
        """
        from console_backend.models.skills_registry import MAX_SKILL_FILE_SIZE_BYTES, MAX_SKILL_FILES

        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        if file_path == "SKILL.md":
            raise ValueError("Cannot write 'SKILL.md' directly — use put_skill() to update the skill entry point.")

        # Check file size
        content_size = len(content.encode("utf-8"))
        if content_size > MAX_SKILL_FILE_SIZE_BYTES:
            size_kb = content_size // 1024
            max_kb = MAX_SKILL_FILE_SIZE_BYTES // 1024
            raise ValueError(
                f"File exceeds maximum size of {max_kb}KB (got {size_kb}KB). "
                "Skill files are for scripts and configs — use /memories/ for large data."
            )

        prefix = self._get_prefix(scope, user_id, group_id)
        skill_md_key = self._skill_key(agent_name, scope, group_id, skill_name)
        file_key = self._skill_file_key(agent_name, skill_name, file_path)
        folder_prefix = self._skill_folder_prefix(agent_name, skill_name)

        async with self._db_session_factory() as db:
            # Verify skill exists
            exists = await db.execute(
                text("SELECT 1 FROM store WHERE prefix = :prefix AND key = :key"),
                {"prefix": prefix, "key": skill_md_key},
            )
            if not exists.first():
                raise ValueError(f"Skill '{skill_name}' does not exist. Create it first with console_create_skill.")

            # Check file count (excluding SKILL.md and the file being written if it already exists)
            count_result = await db.execute(
                text(
                    "SELECT COUNT(*) FROM store WHERE prefix = :prefix AND key LIKE :pattern "
                    "AND key != :skill_md_key AND key != :file_key"
                ),
                {
                    "prefix": prefix,
                    "pattern": f"{folder_prefix}%",
                    "skill_md_key": skill_md_key,
                    "file_key": file_key,
                },
            )
            current_count = count_result.scalar() or 0
            if current_count >= MAX_SKILL_FILES:
                raise ValueError(
                    f"Skill '{skill_name}' already has {current_count} files (maximum is {MAX_SKILL_FILES}). "
                    "Remove unused files before adding new ones."
                )

            await db.execute(
                text("""
                    INSERT INTO store (prefix, key, value, created_at, updated_at)
                    VALUES (:prefix, :key, :value, NOW(), NOW())
                    ON CONFLICT (prefix, key)
                    DO UPDATE SET value = :value, updated_at = NOW()
                """),
                {"prefix": prefix, "key": file_key, "value": _to_jsonb(content)},
            )
            await db.commit()

    async def delete_skill_file(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        file_path: str,
        scope: str,
        group_id: str | None = None,
    ) -> bool:
        """Delete a single file from a skill folder. Returns True if deleted.

        Raises ValueError if attempting to delete SKILL.md.
        """
        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        if file_path == "SKILL.md":
            raise ValueError("Cannot delete SKILL.md directly — use delete_skill() to remove the entire skill.")

        prefix = self._get_prefix(scope, user_id, group_id)
        key = self._skill_file_key(agent_name, skill_name, file_path)

        async with self._db_session_factory() as db:
            result = await db.execute(
                text("DELETE FROM store WHERE prefix = :prefix AND key = :key"),
                {"prefix": prefix, "key": key},
            )
            await db.commit()
            return result.rowcount > 0

    async def put_skill_with_files(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        scope: str,
        content: str,
        files: list[dict[str, str | None]] | None = None,
        group_id: str | None = None,
        replace_files: bool = False,
    ) -> None:
        """Write SKILL.md and optionally bundled files in one transaction.

        Args:
            user_id: User's stable database ID.
            agent_name: Sub-agent name.
            skill_name: Skill identifier.
            scope: 'personal' or 'group'.
            content: SKILL.md content.
            files: Optional list of dicts with 'path', 'content', and optional 'encoding' keys.
            group_id: Group ID (required for group scope).
            replace_files: If True and files is provided, delete all existing
                           non-SKILL.md files first (for bulk replacement).
        """
        from console_backend.models.skills_registry import MAX_SKILL_FILE_SIZE_BYTES, MAX_SKILL_FILES

        if not self._db_session_factory:
            raise RuntimeError("Playbook service not configured")

        if files and len(files) > MAX_SKILL_FILES and not replace_files:
            raise ValueError(f"Too many files ({len(files)}). Maximum is {MAX_SKILL_FILES} files per skill.")

        # Validate file sizes
        if files:
            for f in files:
                content_size = len(f["content"].encode("utf-8"))
                if content_size > MAX_SKILL_FILE_SIZE_BYTES:
                    size_kb = content_size // 1024
                    max_kb = MAX_SKILL_FILE_SIZE_BYTES // 1024
                    raise ValueError(
                        f"File '{f['path']}' exceeds maximum size of {max_kb}KB (got {size_kb}KB). "
                        "Skill files are for scripts and configs — use /memories/ for large data."
                    )

        prefix = self._get_prefix(scope, user_id, group_id)
        skill_md_key = self._skill_key(agent_name, scope, group_id, skill_name)
        folder_prefix = self._skill_folder_prefix(agent_name, skill_name)

        async with self._db_session_factory() as db:
            # Write SKILL.md
            await db.execute(
                text("""
                    INSERT INTO store (prefix, key, value, created_at, updated_at)
                    VALUES (:prefix, :key, :value, NOW(), NOW())
                    ON CONFLICT (prefix, key)
                    DO UPDATE SET value = :value, updated_at = NOW()
                """),
                {"prefix": prefix, "key": skill_md_key, "value": _to_jsonb(content)},
            )

            if files is not None:
                if replace_files:
                    # Delete all existing non-SKILL.md files
                    await db.execute(
                        text("DELETE FROM store WHERE prefix = :prefix AND key LIKE :pattern AND key != :skill_md_key"),
                        {"prefix": prefix, "pattern": f"{folder_prefix}%", "skill_md_key": skill_md_key},
                    )

                # Write each file
                for f in files:
                    file_key = self._skill_file_key(agent_name, skill_name, f["path"])
                    await db.execute(
                        text("""
                            INSERT INTO store (prefix, key, value, created_at, updated_at)
                            VALUES (:prefix, :key, :value, NOW(), NOW())
                            ON CONFLICT (prefix, key)
                            DO UPDATE SET value = :value, updated_at = NOW()
                        """),
                        {
                            "prefix": prefix,
                            "key": file_key,
                            "value": _to_jsonb(f["content"], encoding=f.get("encoding")),
                        },
                    )

            await db.commit()

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


def _to_jsonb(content: str, encoding: str | None = None) -> str:
    """Serialize content to JSON string for PostgreSQL JSONB column."""
    import json

    data: dict[str, str] = {"content": content}
    if encoding:
        data["encoding"] = encoding
    return json.dumps(data)
