"""Skill Sources — external discovery and file fetching.

Two interfaces:
- SkillSource: search + fetch files (GitHub)
- ExternalRegistry: search only, no files (skills.sh)
"""

from console_backend.services.skill_sources.base import ExternalRegistry, SkillSource, SkillSourceDetail
from console_backend.services.skill_sources.github import GitHubSource
from console_backend.services.skill_sources.skills_sh import SkillsShClient

__all__ = ["ExternalRegistry", "SkillSource", "SkillSourceDetail", "GitHubSource", "SkillsShClient"]
