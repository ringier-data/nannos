"""Skill definition models for agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillFile:
    """A file bundled with a default skill."""

    path: str
    content: str
    encoding: str | None = None  # None for UTF-8 text, "base64" for binary


@dataclass
class SkillDefinition:
    """A default (immutable) skill bundled with a sub-agent config version."""

    name: str
    description: str
    body: str
    files: list[SkillFile] = field(default_factory=list)


@dataclass
class ResolvedSkill:
    """A skill after three-tier resolution (personal > group > default)."""

    name: str
    description: str
    body: str
    scope: str  # "personal", "group", or "default"
    files: list[SkillFile] = field(default_factory=list)
    overrides: str | None = None  # scope that this skill overrides (e.g., "default")
