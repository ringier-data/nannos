"""Activating another agent's published skill copies it (ADR 0006).

A sub-agent's config versions are immutable, and `resolve_imported_skills` resolves a
registry row whose own scope is 'sub-agent' as always-latest with `update_available`
forced off. Referencing the publisher's row would therefore let their edits change this
agent's skill underneath a pinned config version, with no update signal and nothing for
a revert to restore — which is why ADR 0006 says copy.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from console_backend.models.skills_registry import SkillFile
from console_backend.models.user import User, UserRole
from console_backend.services.skill_activation_service import SkillActivationService

PUBLISHER_ROW = "11111111-1111-1111-1111-111111111111"
OWN_COPY_ROW = "22222222-2222-2222-2222-222222222222"


def _actor() -> User:
    return User(
        id="user-id-1",
        sub="user-sub-1",
        email="t@example.com",
        first_name="T",
        last_name="U",
        role=UserRole.MEMBER,
    )


def _entry(scope: str, sub_agent_id: int | None):
    entry = MagicMock()
    entry.id = PUBLISHER_ROW
    entry.slug = "published"
    entry.description = "a published skill"
    entry.content_hash = "hash-source"
    entry.scope = scope
    entry.sub_agent_id = sub_agent_id
    entry.files = [SkillFile(path="SKILL.md", content="# published")]
    return entry


def _service(entry):
    svc = SkillActivationService()
    registry = MagicMock()
    registry.upsert_agent_skill = AsyncMock(return_value=(OWN_COPY_ROW, "hash-copy"))
    sub_agents = MagicMock()
    sub_agents.add_skill_to_config = AsyncMock()
    svc.set_skill_registry_service(registry)
    # set_sub_agent_service asserts the concrete type; assign directly for the double.
    svc._sub_agent_service = sub_agents
    svc._get_registry_entry = AsyncMock(return_value=entry)
    return svc, registry, sub_agents


def _db_no_existing_activation() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    result.scalar_one = MagicMock(return_value=99)
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_foreign_published_skill_is_copied_into_a_row_this_agent_owns():
    svc, registry, sub_agents = _service(_entry("sub-agent", sub_agent_id=1))

    await svc.activate(
        db=_db_no_existing_activation(),
        registry_id=PUBLISHER_ROW,
        sub_agent_id=2,
        agent_name="borrower",
        scope="sub-agent",
        user_id="user-id-1",
        actor=_actor(),
    )

    # A row owned by the activating agent was created from the publisher's files.
    registry.upsert_agent_skill.assert_awaited_once()
    kwargs = registry.upsert_agent_skill.await_args.kwargs
    assert kwargs["sub_agent_id"] == 2
    assert kwargs["files"][0].content == "# published"

    # ...and the config pins the COPY, not the publisher's row.
    cfg = sub_agents.add_skill_to_config.await_args.kwargs
    assert cfg["registry_id"] == OWN_COPY_ROW
    assert cfg["content_hash"] == "hash-copy"


@pytest.mark.asyncio
async def test_agents_own_skill_is_referenced_not_duplicated():
    """Re-activating a row this agent already owns must not spawn a second copy."""
    svc, registry, sub_agents = _service(_entry("sub-agent", sub_agent_id=2))

    await svc.activate(
        db=_db_no_existing_activation(),
        registry_id=PUBLISHER_ROW,
        sub_agent_id=2,
        agent_name="owner",
        scope="sub-agent",
        user_id="user-id-1",
        actor=_actor(),
    )

    registry.upsert_agent_skill.assert_not_awaited()
    assert sub_agents.add_skill_to_config.await_args.kwargs["registry_id"] == PUBLISHER_ROW


@pytest.mark.asyncio
async def test_standalone_entry_is_still_referenced_and_pinned():
    """Imported/standalone rows already pin by content_hash — leave them alone."""
    svc, registry, sub_agents = _service(_entry("standalone", sub_agent_id=None))

    await svc.activate(
        db=_db_no_existing_activation(),
        registry_id=PUBLISHER_ROW,
        sub_agent_id=2,
        agent_name="borrower",
        scope="sub-agent",
        user_id="user-id-1",
        actor=_actor(),
    )

    registry.upsert_agent_skill.assert_not_awaited()
    assert sub_agents.add_skill_to_config.await_args.kwargs["registry_id"] == PUBLISHER_ROW
