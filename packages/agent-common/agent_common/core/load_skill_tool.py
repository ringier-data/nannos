"""``load_skill`` — put a whole SKILL.md into the model's context in ONE call.

Why a dedicated tool instead of ``read_file('/skills/<name>/SKILL.md')``:

* Under PTC the read-only filesystem tools are hidden from the model and only
  reachable inside ``eval``. Every ``eval`` result is capped at
  ``PTC_MAX_RESULT_CHARS`` and the interpreter prompt tells the model to return
  "a small slice". A knowledge-base skill of a few hundred lines does not fit,
  so the model pages through it with ``offset``/``limit`` — many round trips,
  and the instructions arrive fragmented and out of order.
* ``read_file`` also prefixes every line with a ``cat -n`` number, which is
  noise for instructions the model is supposed to follow, not edit.

This tool is bound natively (it is in ``_PTC_EXCLUDED_TOOL_NAMES`` so the PTC
middleware never hides it into ``eval``), returns the exact SKILL.md text with
no line numbers and no truncation of its own, and lists the skill's bundled
files so the model knows what else it can ``read_file``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent_common.core.skill_frontmatter import build_skill_content
from agent_common.models.skill import ResolvedSkill

logger = logging.getLogger(__name__)

LOAD_SKILL_TOOL_NAME = "load_skill"


class LoadSkillInput(BaseModel):
    name: str = Field(description="Skill name exactly as listed under 'Available skills'.")


def render_loaded_skill(skill: ResolvedSkill) -> str:
    """Full SKILL.md text plus a short index of the skill's bundled files."""
    content = build_skill_content(name=skill.name, description=skill.description, body=skill.body)
    if not skill.files:
        return content
    listing = "\n".join(f"- /skills/{skill.name}/{f.path}" for f in sorted(skill.files, key=lambda f: f.path))
    return f"{content.rstrip()}\n\n---\nBundled files (read with read_file when the instructions above refer to them):\n{listing}\n"


def render_skills_prompt(resolved_skills: Mapping[str, ResolvedSkill]) -> list[str]:
    """The system-prompt sections for an agent's resolved skills.

    Listed skills get the "## Skills System" block: name and description only, loaded on
    demand with ``load_skill``. An inlined skill (ADR-0012) is left out of that list and
    rendered in full under "## Inlined skills", as the exact text ``load_skill`` would
    return. ``load_skill`` still resolves it, so a model that calls it anyway gets the
    content, not an error.
    """
    skills = sorted(resolved_skills.values(), key=lambda s: s.name)
    listed = [s for s in skills if not s.inline]
    inlined = [s for s in skills if s.inline]
    sections: list[str] = []

    if listed:
        skill_lines = []
        for skill in listed:
            scope_label = skill.scope
            if skill.overrides:
                scope_label += f", overrides {skill.overrides}"
            skill_lines.append(f"- `{skill.name}` ({scope_label}): {skill.description}")
        sections.append(
            "## Skills System\n"
            "You have access to the following skills. Each skill is a directory under /skills/\n"
            "containing a SKILL.md file (and optionally scripts, references, assets).\n\n"
            "To use a skill:\n"
            "1. Match the user's request to a skill description below.\n"
            "2. Call load_skill(name='<name>'). It returns the COMPLETE SKILL.md in one call — "
            "do not read it through read_file, grep or eval, and never page it with offset/limit.\n"
            "3. Follow its instructions. If they refer to a bundled file, read that file with "
            "read_file('/skills/<name>/<file>').\n\n"
            "Available skills:\n" + "\n".join(skill_lines)
        )

    if inlined:
        blocks = [f'<skill name="{skill.name}">\n{render_loaded_skill(skill).rstrip()}\n</skill>' for skill in inlined]
        sections.append(
            "## Inlined skills\n"
            "These skills are already loaded in full below. Follow them directly; do not call "
            "load_skill for them. Read a bundled file they refer to with "
            "read_file('/skills/<name>/<file>').\n\n" + "\n\n".join(blocks)
        )

    return sections


def create_load_skill_tool(resolved_skills: Mapping[str, ResolvedSkill]) -> BaseTool:
    """Build the ``load_skill`` tool over this agent's resolved skills.

    ``resolved_skills`` is the same mapping mounted at ``/skills/`` for the
    filesystem tools, so what this tool returns and what ``read_file`` would
    page through are byte-for-byte the same content.
    """

    def _load(name: str) -> str:
        skill = resolved_skills.get(name.strip())
        if skill is None:
            available = ", ".join(sorted(resolved_skills)) or "(none)"
            return f"Unknown skill '{name}'. Available skills: {available}"
        logger.debug("load_skill: %s (%d chars, %d bundled files)", skill.name, len(skill.body), len(skill.files))
        return render_loaded_skill(skill)

    async def _aload(name: str) -> str:
        return _load(name)

    return StructuredTool.from_function(
        func=_load,
        coroutine=_aload,
        name=LOAD_SKILL_TOOL_NAME,
        description=(
            "Load a skill's complete SKILL.md into context in one call. Use this — not "
            "read_file, grep or eval — whenever a listed skill matches the request. It "
            "returns the whole file, so never page it with offset/limit. Bundled files "
            "the skill refers to are listed at the end; read those with read_file."
        ),
        args_schema=LoadSkillInput,
    )
