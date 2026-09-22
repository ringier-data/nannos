"""Provenance is server-set, and a sync's skill set is authoritative.

``SkillDefinition`` is both the internal host-sync type and the public
``SubAgentCreate``/``SubAgentUpdate`` payload. Two consequences the review of #255
caught, covered here:

1. A client could stamp ``provenance.source_type='well-known'`` on its own skill.
   The registry then treats the row as imported and ``update_skill`` 403s on every
   later edit — a self-inflicted, unrecoverable lock-out.
2. Nothing removed the rows a re-sync no longer writes, so a renamed or withdrawn
   public skill kept a world-readable registry row with no binding behind it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from console_backend.models.skills_registry import SkillProvenance
from console_backend.models.sub_agent import SkillDefinition
from console_backend.models.user import User, UserRole
from console_backend.services.sub_agent_service import SubAgentService


def _actor() -> User:
    return User(
        id="user-id-1",
        sub="user-sub-1",
        email="test@example.com",
        first_name="Test",
        last_name="User",
        role=UserRole.MEMBER,
    )


def _service(registry) -> SubAgentService:
    service = SubAgentService()
    service.set_skill_registry_service(registry)
    return service


def _registry(skill_id: str = "reg-1") -> MagicMock:
    registry = MagicMock()
    registry.upsert_agent_skill = AsyncMock(return_value=(skill_id, "hash-1"))
    registry.prune_mirrored_skills = AsyncMock(return_value=[])
    return registry


_WELL_KNOWN = SkillProvenance(
    source_type="well-known",
    source_repo="https://example.test",
    source_ref="rev-1",
    source_path="https://example.test/.well-known/agent-skills/a/SKILL.md",
)


@pytest.mark.asyncio
async def test_client_supplied_provenance_is_dropped():
    registry = _registry()
    skill = SkillDefinition(name="mine", description="d", body="b", scope="sub-agent", provenance=_WELL_KNOWN)

    await _service(registry)._persist_and_strip_skills(AsyncMock(), _actor(), 1, [skill])

    assert registry.upsert_agent_skill.await_args.kwargs["provenance"] is None


@pytest.mark.asyncio
async def test_sync_provenance_is_kept():
    registry = _registry()
    skill = SkillDefinition(name="mirrored", description="d", body="b", scope="sub-agent", provenance=_WELL_KNOWN)

    await _service(registry)._persist_and_strip_skills(
        AsyncMock(), _actor(), 1, [skill], trust_provenance=True
    )

    assert registry.upsert_agent_skill.await_args.kwargs["provenance"] == _WELL_KNOWN


@pytest.mark.asyncio
async def test_sync_prunes_rows_it_did_not_write():
    registry = _registry(skill_id="kept-id")
    skill = SkillDefinition(name="mirrored", description="d", body="b", scope="sub-agent", provenance=_WELL_KNOWN)

    await _service(registry)._persist_and_strip_skills(
        AsyncMock(), _actor(), 42, [skill], trust_provenance=True
    )

    registry.prune_mirrored_skills.assert_awaited_once()
    kwargs = registry.prune_mirrored_skills.await_args.kwargs
    assert kwargs["sub_agent_id"] == 42
    assert kwargs["source_type"] == "well-known"
    assert kwargs["keep_ids"] == ["kept-id"]


@pytest.mark.asyncio
async def test_user_edit_never_prunes():
    """An ordinary config save is not a statement about the mirrored set."""
    registry = _registry()
    skill = SkillDefinition(name="mine", description="d", body="b", scope="sub-agent")

    await _service(registry)._persist_and_strip_skills(AsyncMock(), _actor(), 1, [skill])

    registry.prune_mirrored_skills.assert_not_awaited()
