"""SKILL.md frontmatter utilities.

Parses and generates YAML frontmatter following the Agent Skills specification
(https://agentskills.io/specification).

A SKILL.md file has this format:

    ---
    name: my-skill
    description: What it does and when to use it.
    ---
    # Instructions

    Body markdown here...

Required frontmatter fields:
  - name: max 64 chars, lowercase alphanumeric + hyphens, no leading/trailing/consecutive hyphens
  - description: 1-1024 chars, describes what the skill does and when to use it

Optional frontmatter fields:
  - metadata: arbitrary key-value mapping
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

# Regex for valid skill names per spec:
# - lowercase alphanumeric + hyphens
# - no leading/trailing/consecutive hyphens
# - max 64 characters
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_MAX_NAME_LEN = 64
_MAX_DESCRIPTION_LEN = 1024


@dataclass
class SkillFrontmatter:
    """Parsed skill frontmatter."""

    name: str
    description: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedSkill:
    """A fully parsed SKILL.md file."""

    frontmatter: SkillFrontmatter
    body: str


def validate_skill_name(name: str) -> str | None:
    """Validate a skill name against the Agent Skills spec.

    Returns None if valid, or an error message string if invalid.
    """
    if not name:
        return "Skill name is required"
    if len(name) > _MAX_NAME_LEN:
        return f"Skill name must be at most {_MAX_NAME_LEN} characters (got {len(name)})"
    if "--" in name:
        return "Skill name must not contain consecutive hyphens (--)"
    if not _SKILL_NAME_RE.match(name):
        return (
            "Skill name must contain only lowercase letters, numbers, and hyphens, "
            "and must not start or end with a hyphen"
        )
    return None


def parse_skill_frontmatter(content: str) -> ParsedSkill | None:
    """Parse a SKILL.md file into frontmatter and body.

    Expects YAML frontmatter between ``---`` delimiters. Returns None if the
    content is empty, has no frontmatter block, or the frontmatter is unparseable.
    """
    if not content or not content.strip():
        return None

    stripped = content.strip()
    if not stripped.startswith("---"):
        return None

    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return None

    yaml_str = parts[1].strip()
    body = parts[2].strip()
    try:
        fm_data = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        return None
    if not isinstance(fm_data, dict):
        return None

    name = str(fm_data.get("name", ""))
    description = str(fm_data.get("description", ""))
    raw_meta = fm_data.get("metadata", {})
    metadata = {str(k): str(v) for k, v in raw_meta.items()} if isinstance(raw_meta, dict) else {}
    return ParsedSkill(
        frontmatter=SkillFrontmatter(
            name=name,
            description=description,
            metadata=metadata,
        ),
        body=body,
    )


def build_skill_content(
    name: str,
    description: str,
    body: str,
    metadata: dict[str, str] | None = None,
) -> str:
    """Build a SKILL.md file from components.

    Generates proper YAML frontmatter followed by the body content.

    Args:
        name: Skill identifier (must pass validate_skill_name)
        description: What the skill does and when to use it
        body: Markdown instructions body
        metadata: Optional key-value metadata

    Returns:
        Complete SKILL.md content string
    """
    lines = ["---", f"name: {name}", f"description: {description}"]

    if metadata:
        lines.append("metadata:")
        for k, v in metadata.items():
            lines.append(f'  {k}: "{v}"')

    lines.append("---")
    lines.append("")

    if body:
        lines.append(body)

    content = "\n".join(lines)
    # Ensure trailing newline
    if not content.endswith("\n"):
        content += "\n"
    return content
