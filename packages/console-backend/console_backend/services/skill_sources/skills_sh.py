"""skills.sh — ExternalRegistry implementation.

skills.sh is an external registry/index (90k+ skills). It provides search
and metadata but does NOT serve file contents. File fetching always goes
through a SkillSource (GitHub) using the repo coordinates returned here.

API base: configurable via SKILL_REGISTRY_URL (defaults to https://skills.sh).
Auth: optional Bearer token via SKILL_REGISTRY_API_KEY.
"""

import logging

import httpx
from pydantic import SecretStr

from console_backend.models.skills_registry import SkillSearchResult
from console_backend.services.skill_sources.base import ExternalRegistry

logger = logging.getLogger(__name__)


class SkillsShClient(ExternalRegistry):
    """skills.sh external registry (search-only, no file serving)."""

    def __init__(self, base_url: str, api_key: SecretStr | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def name(self) -> str:
        return "skills.sh"

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        return headers

    async def search(self, query: str, limit: int = 50) -> list[SkillSearchResult]:
        """Search skills.sh index via /api/v1/skills/search.

        Returns metadata (repo coordinates, slug, description) — NOT files.
        To get files, use GitHubSource.fetch_skill() with the returned coordinates.
        """
        if not query or len(query) < 2:
            return []

        url = f"{self._base_url}/api/v1/skills/search"
        params = {"q": query, "limit": min(limit, 200)}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params, headers=self._headers())
            if resp.status_code != 200:
                logger.warning("skills.sh search failed: %s %s", resp.status_code, resp.text[:200])
                return []

            data = resp.json()
            results = []
            for item in data.get("data", []):
                if item.get("isDuplicate"):
                    continue
                results.append(
                    SkillSearchResult(
                        id=item["id"],
                        slug=item["slug"],
                        name=item.get("name", item["slug"]),
                        source=item["source"],
                        installs=item.get("installs", 0),
                        source_type=item.get("sourceType", "github"),
                        install_url=item.get("installUrl"),
                        url=item.get("url"),
                    )
                )
            return results
