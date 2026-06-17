"""Playbook reader service for loading AGENTS.md and skill folders.

Reads playbook files from a dedicated "agent-data" namespace in the store:
- Personal playbooks: namespace=(user_id, "agent-data"), key="/{agent_name}/AGENTS.md"
- Personal skills: namespace=(user_id, "agent-data"), key="/{agent_name}/skills/{skill_name}/SKILL.md"
- Personal skill files: namespace=(user_id, "agent-data"), key="/{agent_name}/skills/{skill_name}/scripts/check.py"
- Group playbooks: namespace=(group_id, "agent-data"), key="/{agent_name}/AGENTS.md"
- Group skills: namespace=(group_id, "agent-data"), key="/{agent_name}/skills/{skill_name}/SKILL.md"

Provides:
- read_agents_md() - loads AGENTS.md for system prompt injection
- list_skills() - lists available skills for the skill index
- read_skill() - loads a specific skill's SKILL.md on-demand
- read_skill_files() - loads all bundled files for a skill
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from langgraph.store.postgres.aio import AsyncPostgresStore

from agent_common.core.skill_frontmatter import parse_skill_frontmatter
from agent_common.models.skill import SkillFile

logger = logging.getLogger(__name__)

# Cache TTL in seconds
_CACHE_TTL = 60


@dataclass
class SkillIndexEntry:
    """Entry in the skill index shown in system prompt."""

    name: str
    description: str
    scope: str  # "personal" or "group"
    file_paths: list[str] = field(default_factory=list)  # relative paths of bundled files (excluding SKILL.md)


@dataclass
class _CacheEntry:
    """Internal cache entry with TTL."""

    value: Any
    expires_at: float


class PlaybookReaderService:
    """Service for reading playbook files from the persistent store.

    Reads AGENTS.md and SKILLS.md files from the user-scoped and group-scoped
    namespaces in AsyncPostgresStore. Includes a simple in-memory TTL cache
    to avoid repeated store reads within the same turn.
    """

    def __init__(self, store: AsyncPostgresStore):
        """Initialize with a reference to the document store.

        Args:
            store: AsyncPostgresStore instance for reading files
        """
        self._store = store
        self._cache: dict[str, _CacheEntry] = {}

    def _get_cached(self, key: str) -> Any | None:
        """Get a value from cache if not expired."""
        entry = self._cache.get(key)
        if entry and entry.expires_at > time.time():
            return entry.value
        if entry:
            del self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        """Set a value in cache with TTL."""
        self._cache[key] = _CacheEntry(value=value, expires_at=time.time() + _CACHE_TTL)

    async def _read_file_from_store(self, namespace: tuple[str, ...], file_path: str) -> str | None:
        """Read a file from the store by namespace and path.

        Args:
            namespace: Store namespace tuple (e.g., (user_id, "agent-data"))
            file_path: The file path key in the store

        Returns:
            File content as string, or None if not found
        """
        try:
            items = await self._store.aget(namespace=namespace, key=file_path)
            if items and hasattr(items, "value") and items.value:
                content = items.value.get("content")
                if content:
                    return content
            return None
        except Exception as e:
            logger.debug(f"Could not read {file_path} from namespace {namespace}: {e}")
            return None

    async def _read_raw_value(self, namespace: tuple[str, ...], file_path: str) -> dict | None:
        """Read the raw JSONB value dict from the store (content + encoding)."""
        try:
            items = await self._store.aget(namespace=namespace, key=file_path)
            if items and hasattr(items, "value") and items.value:
                return items.value
            return None
        except Exception as e:
            logger.debug(f"Could not read {file_path} from namespace {namespace}: {e}")
            return None

    async def _list_files_in_store(self, namespace: tuple[str, ...], prefix: str) -> list[str]:
        """List file keys in a store namespace matching a prefix.

        Uses asearch without a semantic query (query=None) to list items,
        then filters by key prefix client-side.

        Args:
            namespace: Store namespace tuple
            prefix: Key prefix to filter by

        Returns:
            List of matching file path keys
        """
        try:
            results = await self._store.asearch(namespace, limit=100)
            return [item.key for item in results if item.key.startswith(prefix)]
        except Exception as e:
            logger.debug(f"Could not list files with prefix {prefix} in namespace {namespace}: {e}")
            return []

    async def read_agents_md(
        self,
        user_id: str,
        agent_name: str,
        group_ids: list[str] | None = None,
    ) -> tuple[str | None, str | None]:
        """Read AGENTS.md for a given agent from both personal and group scopes.

        Searches all provided groups and returns the first group that has content.

        Args:
            user_id: User's stable database ID
            agent_name: Name of the sub-agent (e.g., "data-analyst")
            group_ids: Optional list of group IDs to search for group-scoped playbook

        Returns:
            Tuple of (group_content, personal_content) — either may be None
        """
        personal_path = f"/{agent_name}/AGENTS.md"
        personal_cache_key = f"agents_md:{user_id}:{agent_name}"

        # Check cache for personal
        personal_content = self._get_cached(personal_cache_key)
        if personal_content is None:
            personal_content = await self._read_file_from_store(
                namespace=(user_id, "agent-data"),
                file_path=personal_path,
            )
            # Cache even None results to avoid repeated lookups
            self._set_cached(personal_cache_key, personal_content or "")

        # Normalize empty string back to None
        if personal_content == "":
            personal_content = None

        # Read group playbook — use first group that has content
        group_content: str | None = None
        for group_id in group_ids or []:
            group_path = f"/{agent_name}/AGENTS.md"
            group_cache_key = f"agents_md:group:{group_id}:{agent_name}"

            content = self._get_cached(group_cache_key)
            if content is None:
                content = await self._read_file_from_store(
                    namespace=(str(group_id), "agent-data"),
                    file_path=group_path,
                )
                self._set_cached(group_cache_key, content or "")

            if content and content != "":
                group_content = content
                break

        return (group_content, personal_content)

    async def list_skills(
        self,
        user_id: str,
        agent_name: str,
        group_ids: list[str] | None = None,
    ) -> list[SkillIndexEntry]:
        """List available skills for a given agent from personal and all group scopes.

        Groups rows by skill folder name. Only SKILL.md rows provide metadata.
        Collects sibling file paths for each skill.

        Aggregates skills from all groups. Personal skills win on name collision,
        then the first group that defines a skill wins.

        Args:
            user_id: User's stable database ID
            agent_name: Name of the sub-agent
            group_ids: Optional list of group IDs for group-scoped skills

        Returns:
            List of SkillIndexEntry with name, description, scope, and file_paths
        """
        groups_key = ",".join(group_ids) if group_ids else ""
        cache_key = f"skills_index:{user_id}:{groups_key}:{agent_name}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        skills: list[SkillIndexEntry] = []
        seen_names: set[str] = set()

        # Personal skills
        personal_entries = await self._list_skill_folders(
            namespace=(user_id, "agent-data"),
            agent_name=agent_name,
            scope="personal",
        )
        for entry in personal_entries:
            skills.append(entry)
            seen_names.add(entry.name)

        # Group skills — aggregate from all groups
        for group_id in group_ids or []:
            group_entries = await self._list_skill_folders(
                namespace=(str(group_id), "agent-data"),
                agent_name=agent_name,
                scope="group",
            )
            for entry in group_entries:
                if entry.name not in seen_names:
                    skills.append(entry)
                    seen_names.add(entry.name)

        self._set_cached(cache_key, skills)
        return skills

    async def _list_skill_folders(
        self,
        namespace: tuple[str, ...],
        agent_name: str,
        scope: str,
    ) -> list[SkillIndexEntry]:
        """List skill folders in a namespace, grouped by folder name.

        Returns one SkillIndexEntry per skill folder that has a SKILL.md.
        """
        prefix = f"/{agent_name}/skills/"
        all_keys = await self._list_files_in_store(namespace=namespace, prefix=prefix)

        # Group keys by skill name (first path segment after skills/)
        folders: dict[str, list[str]] = {}
        for key in all_keys:
            rel = key.removeprefix(prefix)
            if not rel:
                continue
            parts = rel.split("/", 1)
            skill_name = parts[0]
            file_name = parts[1] if len(parts) > 1 else ""
            if skill_name not in folders:
                folders[skill_name] = []
            if file_name:
                folders[skill_name].append(file_name)

        entries: list[SkillIndexEntry] = []
        for skill_name, files in folders.items():
            if "SKILL.md" not in files:
                continue  # Skip folders without entry point

            # Read description from SKILL.md
            skill_md_path = f"/{agent_name}/skills/{skill_name}/SKILL.md"
            description = await self._extract_skill_description(
                namespace=namespace,
                file_path=skill_md_path,
            )

            # Collect non-SKILL.md file paths
            file_paths = sorted(f for f in files if f != "SKILL.md")

            entries.append(
                SkillIndexEntry(
                    name=skill_name,
                    description=description,
                    scope=scope,
                    file_paths=file_paths,
                )
            )

        return entries

    async def read_skill(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        group_ids: list[str] | None = None,
        scope: str = "auto",
    ) -> str | None:
        """Read a specific skill's SKILL.md content.

        Args:
            user_id: User's stable database ID
            agent_name: Name of the sub-agent
            skill_name: Name of the skill
            group_ids: Optional list of group IDs for group-scoped skills
            scope: "personal", "group", or "auto" (personal first, fallback to groups)

        Returns:
            SKILL.md content, or None if not found
        """
        skill_path = f"/{agent_name}/skills/{skill_name}/SKILL.md"

        if scope in ("personal", "auto"):
            content = await self._read_file_from_store(
                namespace=(user_id, "agent-data"),
                file_path=skill_path,
            )
            if content:
                return content
            if scope == "personal":
                return None

        if scope in ("group", "auto"):
            for group_id in group_ids or []:
                content = await self._read_file_from_store(
                    namespace=(str(group_id), "agent-data"),
                    file_path=skill_path,
                )
                if content:
                    return content

        return None

    async def read_skill_files(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        group_ids: list[str] | None = None,
        scope: str = "auto",
    ) -> list[SkillFile]:
        """Read all bundled files for a skill (excluding SKILL.md).

        Searches in the same scope resolution order as read_skill().

        Args:
            user_id: User's stable database ID
            agent_name: Name of the sub-agent
            skill_name: Name of the skill
            group_ids: Optional list of group IDs for group-scoped skills
            scope: "personal", "group", or "auto"

        Returns:
            List of SkillFile objects (path + content) for all non-SKILL.md files.
            Returns empty list if skill not found or has no files.
        """
        folder_prefix = f"/{agent_name}/skills/{skill_name}/"

        async def _read_files_from_namespace(namespace: tuple[str, ...]) -> list[SkillFile] | None:
            keys = await self._list_files_in_store(namespace=namespace, prefix=folder_prefix)
            if not keys:
                return None
            files: list[SkillFile] = []
            for key in keys:
                rel_path = key.removeprefix(folder_prefix)
                if rel_path == "SKILL.md" or not rel_path:
                    continue
                value = await self._read_raw_value(namespace=namespace, file_path=key)
                if value and value.get("content"):
                    files.append(
                        SkillFile(
                            path=rel_path,
                            content=value["content"],
                            encoding=value.get("encoding"),
                        )
                    )
            return files if files else []

        if scope in ("personal", "auto"):
            result = await _read_files_from_namespace((user_id, "agent-data"))
            if result is not None:
                return result
            if scope == "personal":
                return []

        if scope in ("group", "auto"):
            for group_id in group_ids or []:
                result = await _read_files_from_namespace((str(group_id), "agent-data"))
                if result is not None:
                    return result

        return []

    async def _extract_skill_description(self, namespace: tuple[str, ...], file_path: str) -> str:
        """Extract description from a skill file.

        Reads the ``description`` field from SKILL.md YAML frontmatter via
        parse_skill_frontmatter.

        Args:
            namespace: Store namespace
            file_path: Full file path key

        Returns:
            Short description string
        """
        content = await self._read_file_from_store(namespace=namespace, file_path=file_path)
        if not content:
            return ""

        parsed = parse_skill_frontmatter(content)
        if parsed and parsed.frontmatter.description:
            return parsed.frontmatter.description

        return ""
