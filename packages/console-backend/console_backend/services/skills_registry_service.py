"""Skills Orchestration Service.

Thin facade that composes:
- GitHubSource (file fetching and repo browsing)
- SkillsShClient (external registry search — no file serving)
- SkillRegistryService for internal CRUD over the registry table

This file exists for backward compatibility — the router calls methods here
which delegate to the appropriate lower-level service.
"""

import logging

from console_backend.config import config
from console_backend.models.skills_registry import (
    GitHubSkillDetail,
    SkillSearchResult,
)
from console_backend.services.skill_sources import ExternalRegistry, GitHubSource, SkillsShClient

logger = logging.getLogger(__name__)


class SkillsRegistryService:
    """Orchestration layer for skill discovery and fetching.

    - GitHubSource: the only true source (search + fetch files)
    - ExternalRegistry: search-only index (skills.sh or compatible)
    """

    def __init__(self) -> None:
        self._github_source = GitHubSource()
        self._external_registry: ExternalRegistry = SkillsShClient(
            base_url=config.skills_registry.registry_url,
            api_key=config.skills_registry.registry_api_key,
        )

    @property
    def github_source(self) -> GitHubSource:
        return self._github_source

    @property
    def external_registry(self) -> ExternalRegistry:
        return self._external_registry

    # ─── External registry search (metadata only, no files) ────────────────────

    async def search_external(self, query: str, limit: int = 50) -> tuple[list[SkillSearchResult], str | None]:
        """Search the external registry for community skills. Returns metadata, not files."""
        results = await self._external_registry.search(query=query, limit=limit)
        return results, None

    # ─── Git operations (primary source) ─────────────────────────────────────

    async def browse_repo(
        self, repo: str, ref: str = "main", limit: int = 50, offset: int = 0
    ) -> tuple[list[SkillSearchResult], int]:
        """Browse available skills in a Git repository via GitHub source.

        Returns (results, total) for pagination.
        """
        query = f"{repo}@{ref}" if ref != "main" else repo
        return await self._github_source.browse(query, limit=limit, offset=offset)

    async def fetch_skill_files_from_github(
        self, repo: str, skill_name: str, ref: str = "main"
    ) -> GitHubSkillDetail | None:
        """Fetch a skill's files from GitHub. Returns GitHubSkillDetail or None."""
        source_id = f"{repo}/{skill_name}@{ref}" if ref != "main" else f"{repo}/{skill_name}"
        detail = await self._github_source.fetch_skill(source_id)
        if detail is None:
            return None
        return GitHubSkillDetail(
            files=detail.files,
            tree_sha=detail.tree_sha,
        )

    def resolve_registry_id(self, registry_id: str) -> tuple[str, str]:
        """Resolve a skills.sh registry ID to Git coordinates (repo, skill_name).

        Format: 'owner/repo/slug' → ('owner/repo', 'slug')
        """
        parts = registry_id.strip("/").split("/")
        if len(parts) < 3:
            raise ValueError(f"Invalid registry_id format, expected 'owner/repo/slug': {registry_id}")
        repo = f"{parts[0]}/{parts[1]}"
        skill_name = parts[2]
        return repo, skill_name


# Singleton instance
skills_registry_service = SkillsRegistryService()
