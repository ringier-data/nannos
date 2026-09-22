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
