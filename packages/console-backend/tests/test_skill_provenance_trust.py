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
        AsyncMock(), _actor(), 42, [skill], trust_provenance=True, prune_mirrored=True
    )

    registry.prune_mirrored_skills.assert_awaited_once()
    kwargs = registry.prune_mirrored_skills.await_args.kwargs
    assert kwargs["sub_agent_id"] == 42
    assert kwargs["keep_ids"] == ["kept-id"]


@pytest.mark.asyncio
async def test_sync_that_withdraws_its_last_skill_still_prunes():
    """The case the prune exists for: nothing left to derive a source type from.

    Deriving the scope from the incoming payload made an empty sync a no-op, which is
    precisely the stale-public-row situation being fixed.
    """
    registry = _registry()

    await _service(registry)._persist_and_strip_skills(
        AsyncMock(), _actor(), 42, [], trust_provenance=True, prune_mirrored=True
    )

    registry.prune_mirrored_skills.assert_awaited_once()
    assert registry.prune_mirrored_skills.await_args.kwargs["keep_ids"] == []


@pytest.mark.asyncio
async def test_user_edit_never_prunes():
    """An ordinary config save is not a statement about the mirrored set."""
    registry = _registry()
    skill = SkillDefinition(name="mine", description="d", body="b", scope="sub-agent")

    await _service(registry)._persist_and_strip_skills(AsyncMock(), _actor(), 1, [skill])

    registry.prune_mirrored_skills.assert_not_awaited()


@pytest.mark.asyncio
async def test_revert_trusts_provenance_but_never_prunes():
    """A revert replays one stored version; it does not own the upstream set."""
    registry = _registry()
    skill = SkillDefinition(name="mirrored", description="d", body="b", scope="sub-agent", provenance=_WELL_KNOWN)

    await _service(registry)._persist_and_strip_skills(
        AsyncMock(), _actor(), 1, [skill], trust_provenance=True
    )

    assert registry.upsert_agent_skill.await_args.kwargs["provenance"] == _WELL_KNOWN
    registry.prune_mirrored_skills.assert_not_awaited()


# --- Borrowed skills are read-only references ---

_AGENT1_ROW = "11111111-1111-1111-1111-111111111111"
_AGENT2_ROW = "22222222-2222-2222-2222-222222222222"
_MISSING_ROW = "33333333-3333-3333-3333-333333333333"


def _db_with_registry_rows(rows: list[dict]) -> AsyncMock:
    """A db whose single SELECT returns these skill_registry (id, sub_agent_id) rows."""
    db = AsyncMock()
    result = MagicMock()
    result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_skill_borrowed_from_another_agent_is_never_written_back():
    """A public skill activated from agent 1 onto agent 2 stays agent 1's to edit.

    resolve_imported_skills copies the publisher's scope ('sub-agent') onto the
    borrowing agent's config entry, so the scope test alone would send the next save
    into the upsert branch and rewrite the publisher's row by id.
    """
    registry = _registry()
    borrowed = SkillDefinition(
        name="published",
        description="d",
        body="b",
        scope="sub-agent",
        registry_id=_AGENT1_ROW,
        content_hash="hash-1",
    )
    db = _db_with_registry_rows([{"id": _AGENT1_ROW, "sub_agent_id": 1}])

    refs = await _service(registry)._persist_and_strip_skills(db, _actor(), 2, [borrowed])

    registry.upsert_agent_skill.assert_not_awaited()
    assert refs[0].registry_id == _AGENT1_ROW
    assert refs[0].content_hash == "hash-1"


@pytest.mark.asyncio
async def test_agents_own_skill_is_still_written_back():
    """The ownership test must not break the ordinary edit path."""
    registry = _registry(skill_id=_AGENT2_ROW)
    own = SkillDefinition(
        name="mine",
        description="d",
        body="b",
        scope="sub-agent",
        registry_id=_AGENT2_ROW,
        content_hash="hash-1",
    )
    db = _db_with_registry_rows([{"id": _AGENT2_ROW, "sub_agent_id": 2}])

    await _service(registry)._persist_and_strip_skills(db, _actor(), 2, [own])

    registry.upsert_agent_skill.assert_awaited_once()


@pytest.mark.asyncio
async def test_dangling_registry_reference_is_not_written_through():
    """An id with no row is treated as foreign rather than recreated under this agent."""
    registry = _registry()
    ghost = SkillDefinition(
        name="ghost", description="d", body="b", scope="sub-agent", registry_id=_MISSING_ROW, content_hash="h"
    )
    db = _db_with_registry_rows([])

    await _service(registry)._persist_and_strip_skills(db, _actor(), 2, [ghost])

    registry.upsert_agent_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_row_cannot_be_written_through_by_claiming_sub_agent_scope():
    """The claimed scope is not evidence of anything — that is why ownership is looked up.

    A config save naming a standalone row's UUID while claiming scope='sub-agent'
    would otherwise reach upsert_agent_skill and rewrite that row.
    """
    registry = _registry()
    crafted = SkillDefinition(
        name="theirs",
        description="d",
        body="rewritten",
        scope="sub-agent",
        registry_id=_AGENT1_ROW,
        content_hash="hash-1",
    )
    db = _db_with_registry_rows([{"id": _AGENT1_ROW, "sub_agent_id": None}])

    await _service(registry)._persist_and_strip_skills(db, _actor(), 2, [crafted])

    registry.upsert_agent_skill.assert_not_awaited()
