"""Tests for skills_resolver.resolve_skills_for_agent."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_common.core.skills_resolver import resolve_skills_for_agent
from agent_common.models.skill import SkillDefinition, SkillFile


def _std_skills():
    return [
        SkillDefinition(
            name="incident-triage",
            description="Handle production incidents.",
            body="# Steps\n1. Check\n2. Fix",
            files=[SkillFile(path="scripts/check.py", content="print('ok')")],
        ),
        SkillDefinition(
            name="weekly-report",
            description="Generate weekly report.",
            body="Summarize the week.",
        ),
    ]


@pytest.mark.asyncio
async def test_standard_skills_passthrough():
    """Default skills returned when no personal/group overrides exist."""
    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(return_value=[])

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=[],
            default_skills=_std_skills(),
        )

    assert len(result) == 2
    assert "incident-triage" in result
    assert "weekly-report" in result
    assert result["incident-triage"].scope == "default"
    assert result["incident-triage"].files[0].path == "scripts/check.py"
    assert result["weekly-report"].scope == "default"
    assert result["weekly-report"].overrides is None


@pytest.mark.asyncio
async def test_personal_overrides_standard():
    """Personal skill with same name overrides standard."""
    from agent_common.core.playbook_reader import SkillIndexEntry

    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(
            return_value=[
                SkillIndexEntry(name="incident-triage", description="My custom triage", scope="personal"),
            ]
        )
        reader.read_skill = AsyncMock(return_value="Custom steps for triage.")
        reader.read_skill_files = AsyncMock(return_value=[])

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=[],
            default_skills=_std_skills(),
        )

    assert result["incident-triage"].scope == "personal"
    assert result["incident-triage"].body == "Custom steps for triage."
    assert result["incident-triage"].overrides == "default"
    # Standard skill that wasn't overridden stays
    assert result["weekly-report"].scope == "default"


@pytest.mark.asyncio
async def test_group_overrides_standard():
    """Group skill with same name overrides standard."""
    from agent_common.core.playbook_reader import SkillIndexEntry

    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(
            return_value=[
                SkillIndexEntry(name="weekly-report", description="Team report format", scope="group"),
            ]
        )
        reader.read_skill = AsyncMock(return_value="Team-specific weekly report format.")
        reader.read_skill_files = AsyncMock(return_value=[])

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=["group-1"],
            default_skills=_std_skills(),
        )

    assert result["weekly-report"].scope == "group"
    assert result["weekly-report"].body == "Team-specific weekly report format."
    assert result["weekly-report"].overrides == "default"


@pytest.mark.asyncio
async def test_personal_skill_no_override():
    """Personal skill with a new name doesn't set overrides."""
    from agent_common.core.playbook_reader import SkillIndexEntry

    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(
            return_value=[
                SkillIndexEntry(name="my-custom-workflow", description="A new skill", scope="personal"),
            ]
        )
        reader.read_skill = AsyncMock(return_value="Do custom things.")
        reader.read_skill_files = AsyncMock(return_value=[])

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=[],
            default_skills=_std_skills(),
        )

    assert "my-custom-workflow" in result
    assert result["my-custom-workflow"].scope == "personal"
    assert result["my-custom-workflow"].overrides is None
    # Defaults still present
    assert len(result) == 3


@pytest.mark.asyncio
async def test_empty_standard_skills():
    """Works with no default skills."""
    from agent_common.core.playbook_reader import SkillIndexEntry

    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(
            return_value=[
                SkillIndexEntry(name="custom", description="A skill", scope="personal"),
            ]
        )
        reader.read_skill = AsyncMock(return_value="Body text.")
        reader.read_skill_files = AsyncMock(return_value=[])

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=[],
            default_skills=[],
        )

    assert len(result) == 1
    assert result["custom"].scope == "personal"
    assert result["custom"].overrides is None


@pytest.mark.asyncio
async def test_no_skills_at_all():
    """Returns empty dict when no skills from any source."""
    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(return_value=[])

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=[],
            default_skills=[],
        )

    assert result == {}


@pytest.mark.asyncio
async def test_unreadable_skill_skipped():
    """If read_skill returns None, that entry is skipped."""
    from agent_common.core.playbook_reader import SkillIndexEntry

    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(
            return_value=[
                SkillIndexEntry(name="broken", description="Cannot read", scope="personal"),
            ]
        )
        reader.read_skill = AsyncMock(return_value=None)

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=[],
            default_skills=_std_skills(),
        )

    # Only default skills remain; broken one was skipped
    assert len(result) == 2
    assert "broken" not in result


@pytest.mark.asyncio
async def test_no_group_ids():
    """Passing empty group_ids doesn't crash."""
    with patch("agent_common.core.skills_resolver.PlaybookReaderService") as MockReader:
        reader = MockReader.return_value
        reader.list_skills = AsyncMock(return_value=[])

        result = await resolve_skills_for_agent(
            store=AsyncMock(),
            user_id="user-1",
            agent_name="my-agent",
            group_ids=[],
            default_skills=_std_skills(),
        )

    assert len(result) == 2
