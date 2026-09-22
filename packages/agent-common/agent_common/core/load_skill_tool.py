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
import os
from collections.abc import Mapping

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent_common.core.skill_frontmatter import build_skill_content
from agent_common.models.skill import ResolvedSkill

logger = logging.getLogger(__name__)

LOAD_SKILL_TOOL_NAME = "load_skill"

# Upper bound on what a single ``load_skill`` call may put into context.
#
# The tool is deliberately exempt from the PTC ``eval`` result cap, because a SKILL.md
# arriving in fragments is the problem it exists to solve. That exemption removes the
# only ceiling the runtime would otherwise apply, so the tool carries its own: the
# registry accepts skills up to 256 KB, and one of those would land as a single
# ~64k-token ToolMessage. Past the limit the model is handed the frontmatter plus a
# pointer to ``read_file``, which pages — worse than one call, still far better than a
# turn whose context is gone.
LOAD_SKILL_MAX_CHARS = int(os.getenv("LOAD_SKILL_MAX_CHARS", "120000"))


class LoadSkillInput(BaseModel):
    name: str = Field(description="Skill name exactly as listed under 'Available skills'.")


def render_loaded_skill(skill: ResolvedSkill) -> str:
    """Full SKILL.md text plus a short index of the skill's bundled files."""
    content = build_skill_content(name=skill.name, description=skill.description, body=skill.body)
    if not skill.files:
        return content
    listing = "\n".join(f"- /skills/{skill.name}/{f.path}" for f in sorted(skill.files, key=lambda f: f.path))
    return f"{content.rstrip()}\n\n---\nBundled files (read with read_file when the instructions above refer to them):\n{listing}\n"


def _oversize_notice(skill: ResolvedSkill, size: int) -> str:
    """What the model gets instead of a SKILL.md too large to hand over whole."""
    head = build_skill_content(name=skill.name, description=skill.description, body="")
    return (
        f"{head.rstrip()}\n\n---\n"
        f"This skill's SKILL.md is {size} characters, over the {LOAD_SKILL_MAX_CHARS}-character "
        f"limit for a single load_skill call, so it was NOT loaded. Read it in pages instead:\n"
        f"read_file('/skills/{skill.name}/SKILL.md', offset=0, limit=500), then raise offset "
        f"until the file ends.\n"
    )


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
        rendered = render_loaded_skill(skill)
        if len(rendered) > LOAD_SKILL_MAX_CHARS:
            logger.warning(
                "load_skill: %s is %d chars, over the %d limit — returning a read_file pointer instead",
                skill.name,
                len(rendered),
                LOAD_SKILL_MAX_CHARS,
            )
            return _oversize_notice(skill, len(rendered))
        logger.debug("load_skill: %s (%d chars, %d bundled files)", skill.name, len(skill.body), len(skill.files))
        return rendered

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
