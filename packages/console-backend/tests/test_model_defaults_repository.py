"""DB tests for ModelDefaultsRepository tier memory (model_alias_tiers, migration 069).

Setting a model as a chat-tier default records which tier it served, so a retired
concrete-model sub-agent can later degrade to that tier's successor.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.user import User
from console_backend.repositories.model_defaults_repository import ModelDefaultsRepository
from console_backend.services.audit_service import AuditService


@pytest.fixture
def repo() -> ModelDefaultsRepository:
    r = ModelDefaultsRepository()
    r.set_audit_service(AuditService())
    return r


@pytest.mark.asyncio
async def test_chat_tier_default_is_remembered(repo, pg_session: AsyncSession, test_user_db: User):
    await repo.upsert_default(pg_session, actor=test_user_db, role="chat:premium", model_alias="opus-x")
    assert (await repo.get_alias_tiers(pg_session)).get("opus-x") == ["chat:premium"]


@pytest.mark.asyncio
async def test_reassigning_tier_keeps_old_alias_memory(repo, pg_session: AsyncSession, test_user_db: User):
    # opus-x was premium; gpt-4o replaces it. opus-x must KEEP its premium memory (so a
    # sub-agent pinned to the now-retired opus-x degrades to the premium successor gpt-4o).
    await repo.upsert_default(pg_session, actor=test_user_db, role="chat:premium", model_alias="opus-x")
    await repo.upsert_default(pg_session, actor=test_user_db, role="chat:premium", model_alias="gpt-4o")
    tiers = await repo.get_alias_tiers(pg_session)
    assert tiers.get("opus-x") == ["chat:premium"]
    assert tiers.get("gpt-4o") == ["chat:premium"]


@pytest.mark.asyncio
async def test_model_can_be_default_for_multiple_tiers(repo, pg_session: AsyncSession, test_user_db: User):
    # One model serving both low AND premium is remembered for BOTH tiers (multi-tier support).
    await repo.upsert_default(pg_session, actor=test_user_db, role="chat:low", model_alias="m-x")
    await repo.upsert_default(pg_session, actor=test_user_db, role="chat:premium", model_alias="m-x")
    assert sorted((await repo.get_alias_tiers(pg_session)).get("m-x")) == ["chat:low", "chat:premium"]


@pytest.mark.asyncio
async def test_non_chat_role_is_not_remembered(repo, pg_session: AsyncSession, test_user_db: User):
    await repo.upsert_default(pg_session, actor=test_user_db, role="embedding", model_alias="embed-x")
    assert "embed-x" not in await repo.get_alias_tiers(pg_session)


# --- failover chains (migration 095, nannos#204) ------------------------------------------
# Real-DB coverage per AGENTS.md: replace_fallbacks is a DELETE+INSERT under a DEFERRABLE
# unique (role, position) constraint plus a (role, alias) PK, and it writes an audit row.
# None of those failure modes are observable against a mocked repository.


@pytest.mark.asyncio
async def test_chain_round_trips_in_order(repo, pg_session: AsyncSession, test_user_db: User):
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["vertex", "azure"])
    assert await repo.get_fallbacks(pg_session, "chat") == ["vertex", "azure"]


@pytest.mark.asyncio
async def test_reordering_reuses_positions_within_one_statement(repo, pg_session: AsyncSession, test_user_db: User):
    """The reorder writes position 0 to an alias that currently holds position 1. It only
    commits because the unique constraint is DEFERRABLE; a non-deferred one would fire
    mid-statement even though the end state is valid."""
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["vertex", "azure"])
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["azure", "vertex"])
    assert await repo.get_fallbacks(pg_session, "chat") == ["azure", "vertex"]


@pytest.mark.asyncio
async def test_chains_are_scoped_per_tier(repo, pg_session: AsyncSession, test_user_db: User):
    """The same alias may appear in two tiers; the (role, alias) PK must not collide across them."""
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["vertex"])
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat:low", aliases=["vertex"])
    assert await repo.get_fallbacks(pg_session, "chat") == ["vertex"]
    assert await repo.get_fallbacks(pg_session, "chat:low") == ["vertex"]


@pytest.mark.asyncio
async def test_an_empty_chain_clears_the_tier(repo, pg_session: AsyncSession, test_user_db: User):
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["vertex"])
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=[])
    assert await repo.get_fallbacks(pg_session, "chat") == []


@pytest.mark.asyncio
async def test_get_all_fallbacks_returns_every_tier(repo, pg_session: AsyncSession, test_user_db: User):
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["vertex"])
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat:premium", aliases=["opus"])
    chains = await repo.get_all_fallbacks(pg_session)
    assert chains["chat"] == ["vertex"]
    assert chains["chat:premium"] == ["opus"]


@pytest.mark.asyncio
async def test_chain_change_is_audited_with_before_and_after(repo, pg_session: AsyncSession, test_user_db: User):
    """A fleet-wide routing change must leave a record of what it replaced — AGENTS.md requires
    the audit assertion, and 'what was the chain before' is the question an incident asks."""
    from sqlalchemy import text

    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["vertex"])
    await repo.replace_fallbacks(pg_session, actor=test_user_db, role="chat", aliases=["azure"])
    rows = (
        (
            await pg_session.execute(
                text(
                    "SELECT changes FROM audit_logs WHERE entity_id = 'chat' AND action = 'set_default' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows, "no audit row written for the chain change"
    changes = rows[0] if isinstance(rows[0], dict) else __import__("json").loads(rows[0])
    assert changes.get("before") == ["vertex"]
    assert changes.get("after") == ["azure"]
