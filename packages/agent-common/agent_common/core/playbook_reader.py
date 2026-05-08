"""Playbook reader service for loading AGENTS.md and SKILLS.md files.

Reads playbook files from a dedicated "agent-data" namespace in the store:
- Personal playbooks: namespace=(user_id, "agent-data"), key="/{agent_name}/AGENTS.md"
- Personal skills: namespace=(user_id, "agent-data"), key="/{agent_name}/skills/{skill_name}.md"
- Group playbooks: namespace=(group_id, "agent-data"), key="/{agent_name}/AGENTS.md"
- Group skills: namespace=(group_id, "agent-data"), key="/{agent_name}/skills/{skill_name}.md"

Provides:
- read_agents_md() - loads AGENTS.md for system prompt injection
- list_skills() - lists available skills for the skill index
- read_skill() - loads a specific skill file on-demand
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from langgraph.store.postgres.aio import AsyncPostgresStore

from agent_common.core.skill_frontmatter import parse_skill_frontmatter

logger = logging.getLogger(__name__)

# Cache TTL in seconds
_CACHE_TTL = 60

# Legacy namespace — try as fallback when "agent-data" yields nothing.
# Remove after one release cycle.
_LEGACY_NAMESPACE = "playbooks"
_CURRENT_NAMESPACE = "agent-data"


@dataclass
class SkillIndexEntry:
    """Entry in the skill index shown in system prompt."""

    name: str
    description: str
    scope: str  # "personal" or "group"


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

        Tries the provided namespace first; if the namespace ends with
        ``_CURRENT_NAMESPACE`` and no content is found, retries with the
        legacy ``_LEGACY_NAMESPACE`` for backward compatibility.

        Args:
            namespace: Store namespace tuple (e.g., (user_id, "agent-data"))
            file_path: The file path key in the store

        Returns:
            File content as string, or None if not found
        """
        content = await self._try_read(namespace, file_path)
        if content is None and len(namespace) >= 2 and namespace[-1] == _CURRENT_NAMESPACE:
            content = await self._try_read((*namespace[:-1], _LEGACY_NAMESPACE), file_path)
        return content

    async def _try_read(self, namespace: tuple[str, ...], file_path: str) -> str | None:
        """Attempt to read a single file from the store."""
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

    async def _list_files_in_store(self, namespace: tuple[str, ...], prefix: str) -> list[str]:
        """List file keys in a store namespace matching a prefix.

        Uses asearch without a semantic query (query=None) to list items,
        then filters by key prefix client-side. Falls back to the legacy
        namespace if the current one returns no results.

        Args:
            namespace: Store namespace tuple
            prefix: Key prefix to filter by

        Returns:
            List of matching file path keys
        """
        results = await self._try_list(namespace, prefix)
        if not results and len(namespace) >= 2 and namespace[-1] == _CURRENT_NAMESPACE:
            results = await self._try_list((*namespace[:-1], _LEGACY_NAMESPACE), prefix)
        return results

    async def _try_list(self, namespace: tuple[str, ...], prefix: str) -> list[str]:
        """Attempt to list files in a single namespace."""
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

        Aggregates skills from all groups. Personal skills win on name collision,
        then the first group that defines a skill wins.

        Args:
            user_id: User's stable database ID
            agent_name: Name of the sub-agent
            group_ids: Optional list of group IDs for group-scoped skills

        Returns:
            List of SkillIndexEntry with name, description, and scope
        """
        groups_key = ",".join(group_ids) if group_ids else ""
        cache_key = f"skills_index:{user_id}:{groups_key}:{agent_name}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        skills: list[SkillIndexEntry] = []
        seen_names: set[str] = set()

        # Personal skills
        personal_prefix = f"/{agent_name}/skills/"
        personal_files = await self._list_files_in_store(
            namespace=(user_id, "agent-data"),
            prefix=personal_prefix,
        )
        for file_path in personal_files:
            skill_name = file_path.rsplit("/", 1)[-1].removesuffix(".md")
            description = await self._extract_skill_description(
                namespace=(user_id, "agent-data"),
                file_path=file_path,
            )
            skills.append(SkillIndexEntry(name=skill_name, description=description, scope="personal"))
            seen_names.add(skill_name)

        # Group skills — aggregate from all groups
        for group_id in group_ids or []:
            group_prefix = f"/{agent_name}/skills/"
            group_files = await self._list_files_in_store(
                namespace=(str(group_id), "agent-data"),
                prefix=group_prefix,
            )
            for file_path in group_files:
                skill_name = file_path.rsplit("/", 1)[-1].removesuffix(".md")
                # Skip if already seen (personal wins, then first group wins)
                if skill_name in seen_names:
                    continue
                description = await self._extract_skill_description(
                    namespace=(str(group_id), "agent-data"),
                    file_path=file_path,
                )
                skills.append(SkillIndexEntry(name=skill_name, description=description, scope="group"))
                seen_names.add(skill_name)

        self._set_cached(cache_key, skills)
        return skills

    async def read_skill(
        self,
        user_id: str,
        agent_name: str,
        skill_name: str,
        group_ids: list[str] | None = None,
        scope: str = "auto",
    ) -> str | None:
        """Read a specific skill file.

        Args:
            user_id: User's stable database ID
            agent_name: Name of the sub-agent
            skill_name: Name of the skill (without .md extension)
            group_ids: Optional list of group IDs for group-scoped skills
            scope: "personal", "group", or "auto" (personal first, fallback to groups)

        Returns:
            Skill file content, or None if not found
        """
        personal_path = f"/{agent_name}/skills/{skill_name}.md"

        if scope in ("personal", "auto"):
            content = await self._read_file_from_store(
                namespace=(user_id, "agent-data"),
                file_path=personal_path,
            )
            if content:
                return content
            if scope == "personal":
                return None

        if scope in ("group", "auto"):
            for group_id in group_ids or []:
                group_path = f"/{agent_name}/skills/{skill_name}.md"
                content = await self._read_file_from_store(
                    namespace=(str(group_id), "agent-data"),
                    file_path=group_path,
                )
                if content:
                    return content

        return None

    async def _extract_skill_description(self, namespace: tuple[str, ...], file_path: str) -> str:
        """Extract description from a skill file.

        Supports both SKILL.md frontmatter format (preferred) and legacy
        markdown heading format (fallback). Uses parse_skill_frontmatter
        which handles both formats.

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

        return first_heading[:100] if first_heading else ""
