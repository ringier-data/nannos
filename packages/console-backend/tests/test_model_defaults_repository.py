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
