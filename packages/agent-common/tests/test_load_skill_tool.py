"""Tests for the native ``load_skill`` tool — a whole SKILL.md in one call."""

from __future__ import annotations

import asyncio

import pytest

from agent_common.core.graph_utils import _PTC_EXCLUDED_TOOL_NAMES
from agent_common.core.load_skill_tool import (
    LOAD_SKILL_TOOL_NAME,
    create_load_skill_tool,
    render_loaded_skill,
)
from agent_common.middleware.tool_status import _build_status
from agent_common.models.skill import ResolvedSkill, SkillFile


def _skills() -> dict[str, ResolvedSkill]:
    body = "\n".join(f"line {i}: consent details for the Alloy platform" for i in range(1, 601))  # 600 lines, > 20k chars
    return {
        "alloy-kb": ResolvedSkill(
            name="alloy-kb",
            description="Alloy knowledge base",
            body=body,
            scope="default",
            files=[
                SkillFile(path="references/consent.md", content="# consent"),
                SkillFile(path="assets/a.txt", content="x"),
            ],
        ),
        "tiny": ResolvedSkill(name="tiny", description="Tiny skill", body="Do the thing.", scope="personal"),
    }


class TestLoadSkillTool:
    def test_returns_whole_skill_md_without_line_numbers_or_truncation(self):
        tool = create_load_skill_tool(_skills())
        out = tool.invoke({"name": "alloy-kb"})
        assert out.startswith("---\nname: alloy-kb\ndescription: Alloy knowledge base\n")
        assert "line 1: consent details" in out
        assert "line 600: consent details" in out
        assert len(out) > 20_000  # would not fit an eval result
        assert "\t" not in out  # no cat -n prefixes

    def test_lists_bundled_files_with_virtual_paths(self):
        out = create_load_skill_tool(_skills()).invoke({"name": "alloy-kb"})
        assert "Bundled files" in out
        assert "- /skills/alloy-kb/references/consent.md" in out
        assert "- /skills/alloy-kb/assets/a.txt" in out

    def test_no_files_section_when_skill_has_no_files(self):
        out = create_load_skill_tool(_skills()).invoke({"name": "tiny"})
        assert out.rstrip().endswith("Do the thing.")
        assert "Bundled files" not in out

    def test_unknown_skill_names_the_available_ones(self):
        out = create_load_skill_tool(_skills()).invoke({"name": "nope"})
        assert out.startswith("Unknown skill 'nope'")
        assert "alloy-kb" in out and "tiny" in out

    def test_name_is_stripped(self):
        out = create_load_skill_tool(_skills()).invoke({"name": "  tiny "})
        assert "name: tiny" in out

    def test_async_path_matches_sync(self):
        tool = create_load_skill_tool(_skills())
        assert asyncio.run(tool.ainvoke({"name": "tiny"})) == tool.invoke({"name": "tiny"})

    def test_render_matches_skills_store_content(self):
        """What load_skill returns is the same SKILL.md the /skills/ backend serves."""
        from agent_common.backends.skills_store import SkillsStoreBackend

        skills = _skills()
        served = asyncio.run(SkillsStoreBackend(skills).aread("/skills/tiny/SKILL.md", offset=0, limit=10_000))
        assert render_loaded_skill(skills["tiny"]) == served.file_data["content"]


class TestWiring:
    def test_tool_name(self):
        assert create_load_skill_tool(_skills()).name == LOAD_SKILL_TOOL_NAME == "load_skill"

    def test_stays_native_under_ptc(self):
        """Never hidden into eval: the eval result cap would defeat the tool."""
        assert LOAD_SKILL_TOOL_NAME in _PTC_EXCLUDED_TOOL_NAMES

    def test_status_label(self):
        assert _build_status(LOAD_SKILL_TOOL_NAME, {"name": "alloy-kb"}) == "Loading skill alloy-kb…"
        assert _build_status(LOAD_SKILL_TOOL_NAME, {}) == "Loading skill…"

    @pytest.mark.asyncio
    async def test_risk_scorer_never_gates_it(self):
        from unittest.mock import MagicMock

        from agent_common.core.tool_risk_scorer import score_tool_risk

        score, entry = await score_tool_risk(
            LOAD_SKILL_TOOL_NAME, {"name": "alloy-kb"}, tool=None, cache=MagicMock(), server_slug="_self"
        )
        assert score == 0.0
        assert entry is not None and entry.base_score == 0.0
