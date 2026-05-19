"""Abstract base classes for skill discovery.

Two interfaces:

- SkillSource: provides both search AND file fetching (e.g. GitHub).
- ExternalRegistry: provides search only — returns metadata/coordinates,
  never file contents (e.g. skills.sh).

Sources do NOT manage visibility, security verdicts, or persistence.
Those are Skill Registry concerns.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from console_backend.models.skills_registry import SkillFile, SkillSearchResult


@dataclass
class SkillSourceDetail:
    """Result of fetching a skill from a source — ready for import into registry."""

    name: str
    slug: str
    description: str
    files: list[SkillFile]
    source_repo: str | None = None
    source_ref: str | None = None
    source_path: str | None = None
    tree_sha: str | None = None


class SkillSource(ABC):
    """A source that can both search for skills AND fetch their file contents."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source name (e.g. 'github')."""
        ...

    @abstractmethod
    async def search(self, query: str, limit: int = 50) -> list[SkillSearchResult]:
        """Search for skills. Results are ephemeral — not persisted."""
        ...

    @abstractmethod
    async def fetch_skill(self, source_id: str) -> SkillSourceDetail | None:
        """Fetch a skill's files by source-specific ID.

        For GitHub: source_id = 'owner/repo/skill-name' or 'owner/repo' (repo IS skill)
        Optional '@ref' suffix for non-default branches.

        Returns SkillSourceDetail with files ready for import, or None if not found.
        """
        ...


class ExternalRegistry(ABC):
    """A search-only index that returns metadata but does NOT serve file contents.

    To get actual files from a result, resolve its coordinates and use a SkillSource.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable registry name (e.g. 'skills.sh')."""
        ...

    @abstractmethod
    async def search(self, query: str, limit: int = 50) -> list[SkillSearchResult]:
        """Search the registry. Returns metadata (repo coords, slug) — NOT files."""
        ...
