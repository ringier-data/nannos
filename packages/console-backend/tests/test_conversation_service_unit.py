from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from console_backend.exceptions import ConversationOwnershipError
from console_backend.services.conversation_service import ConversationService


@pytest.mark.asyncio
async def test_get_or_create_conversation_ownership_check(monkeypatch):
    cs = ConversationService.__new__(ConversationService)
    cs.conversation_ttl_seconds = 7776000

    # Mock get_conversation to return a conversation owned by other user
    existing = MagicMock()
    existing.conversation_id = "c1"
    existing.user_id = "owner-123"
    existing.started_at = datetime.now(timezone.utc)
    existing.last_message_at = existing.started_at

    cs.get_conversation = AsyncMock(return_value=existing)

    with pytest.raises(ConversationOwnershipError):
        await cs.get_or_create_conversation(conversation_id="c1", user_id="attacker", agent_url="", message=None)


def _service_with_captured_session(rowcount: int, rows=()):
    """A ConversationService whose only DB is a recorder. Returns the service
    and the list of (sql, params) pairs it executed."""
    executed: list[tuple[str, dict]] = []

    result = MagicMock()
    result.rowcount = rowcount
    result.mappings.return_value.all.return_value = list(rows)
    result.mappings.return_value.first.return_value = rows[0] if rows else None

    async def execute(statement, params=None):
        executed.append((str(statement), params or {}))
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    cs = ConversationService.__new__(ConversationService)
    cs._session_factory = MagicMock(return_value=db)
    return cs, executed


@pytest.mark.asyncio
async def test_archive_conversation_scopes_the_update_to_the_owner():
    """Soft delete: status flips, the row stays. Ownership lives in the WHERE
    clause, so another user's conversation simply matches nothing."""
    cs, executed = _service_with_captured_session(rowcount=1)

    assert await cs.archive_conversation("c1", "user-1") is True

    sql, params = executed[0]
    assert "SET status = 'archived'" in sql
    assert "user_id = :user_id" in sql
    assert "DELETE" not in sql.upper()
    assert params["conversation_id"] == "c1"
    assert params["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_archive_conversation_returns_false_when_nothing_matched():
    """Missing, someone else's, or already archived — all indistinguishable."""
    cs, _ = _service_with_captured_session(rowcount=0)
    assert await cs.archive_conversation("c1", "user-1") is False


@pytest.mark.asyncio
async def test_list_hides_archived_conversations():
    """Archiving is only a delete if the row stops coming back in the list."""
    cs, executed = _service_with_captured_session(rowcount=0)
    await cs.get_conversations_by_user_id("user-1")
    sql, _ = executed[0]
    assert "status <> 'archived'" in sql


@pytest.mark.asyncio
async def test_rename_conversation_merges_metadata_and_scopes_to_the_owner():
    """The name is written with a 'user' marker, and the rest of the metadata
    (page_context, embedded_sub_agent_id) survives — hence the `||` merge."""
    cs, executed = _service_with_captured_session(rowcount=1)

    assert await cs.rename_conversation("c1", "user-1", title="Q3 pacing") is True

    sql, params = executed[0]
    assert "SET title = :title" in sql
    assert "metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb)" in sql
    assert "user_id = :user_id" in sql
    # Renaming is not activity: the column the list orders by stays put.
    assert "last_message_at" not in sql
    assert params["title"] == "Q3 pacing"
    assert '"title_source": "user"' in params["patch"].replace("'", '"')


@pytest.mark.asyncio
async def test_rename_conversation_returns_false_when_nothing_matched():
    """Missing or someone else's — indistinguishable, and a 404 either way."""
    cs, _ = _service_with_captured_session(rowcount=0)
    assert await cs.rename_conversation("c1", "user-1", title="Q3 pacing") is False
