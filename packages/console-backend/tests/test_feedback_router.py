"""Tests for feedback router (Phase 1)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_conversation(pg_session: AsyncSession, user_id: str, conversation_id: str = "conv-1") -> str:
    await pg_session.execute(
        text(
            "INSERT INTO conversations (conversation_id, user_id, started_at, last_message_at) "
            "VALUES (:cid, :uid, NOW(), NOW())"
        ),
        {"cid": conversation_id, "uid": user_id},
    )
    await pg_session.commit()
    return conversation_id


# ---------------------------------------------------------------------------
# Submit feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_positive_feedback(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    await _create_conversation(pg_session, test_user_model.id)

    response = await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "positive"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["conversation_id"] == "conv-1"
    assert data["message_id"] == "msg-1"
    assert data["rating"] == "positive"
    assert data["user_id"] == test_user_model.id


@pytest.mark.asyncio
async def test_submit_negative_feedback_with_comment(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    await _create_conversation(pg_session, test_user_model.id)

    response = await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "negative", "comment": "Not helpful"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["rating"] == "negative"
    assert data["comment"] == "Not helpful"


@pytest.mark.asyncio
async def test_upsert_feedback_changes_rating(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Submitting feedback again for the same message updates the rating (upsert)."""
    await _create_conversation(pg_session, test_user_model.id)

    # First submission
    resp1 = await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "positive"},
    )
    assert resp1.status_code == 201
    assert resp1.json()["rating"] == "positive"

    # Second submission — should update, not create a new one
    resp2 = await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "negative", "comment": "Changed my mind"},
    )
    assert resp2.status_code == 201
    assert resp2.json()["rating"] == "negative"
    assert resp2.json()["comment"] == "Changed my mind"

    # Verify only one row exists
    result = await pg_session.execute(
        text("SELECT COUNT(*) FROM message_feedback WHERE conversation_id = 'conv-1' AND message_id = 'msg-1'")
    )
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_submit_feedback_with_sub_agent_id(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    await _create_conversation(pg_session, test_user_model.id)

    response = await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "positive", "sub_agent_id": "search-agent"},
    )

    assert response.status_code == 201
    assert response.json()["sub_agent_id"] == "search-agent"


# ---------------------------------------------------------------------------
# Submit feedback for external conversations (Slack / Google Chat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_external_conversation(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Feedback can be submitted for conversations not in the database (Slack/Google Chat).

    The allow_external flag on the POST endpoint permits this.
    """
    # Do NOT create a conversation — simulate external client
    response = await client_with_db.post(
        "/api/v1/conversations/external-conv-123/messages/msg-1/feedback",
        json={"rating": "positive"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["conversation_id"] == "external-conv-123"


# ---------------------------------------------------------------------------
# Get feedback for conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_conversation_feedback(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    await _create_conversation(pg_session, test_user_model.id)

    # Submit feedback for two different messages
    await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "positive"},
    )
    await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-2/feedback",
        json={"rating": "negative"},
    )

    response = await client_with_db.get("/api/v1/conversations/conv-1/feedback")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    ratings = {fb["message_id"]: fb["rating"] for fb in data}
    assert ratings["msg-1"] == "positive"
    assert ratings["msg-2"] == "negative"


@pytest.mark.asyncio
async def test_get_feedback_wrong_conversation(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """GET feedback for a conversation the user doesn't own returns 404."""
    # Create conversation owned by a different user
    await pg_session.execute(
        text("INSERT INTO users (id, sub, email, first_name, last_name) VALUES (:id, :sub, :email, 'O', 'U')"),
        {"id": "other-id", "sub": "other-sub", "email": "other@test.com"},
    )
    await pg_session.execute(
        text(
            "INSERT INTO conversations (conversation_id, user_id, started_at, last_message_at) "
            "VALUES ('other-conv', 'other-id', NOW(), NOW())"
        ),
    )
    await pg_session.commit()

    response = await client_with_db.get("/api/v1/conversations/other-conv/feedback")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_feedback_nonexistent_conversation(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """GET feedback for a conversation that doesn't exist returns 404 (strict for GET)."""
    response = await client_with_db.get("/api/v1/conversations/does-not-exist/feedback")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Delete feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_feedback(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    await _create_conversation(pg_session, test_user_model.id)

    await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "positive"},
    )

    response = await client_with_db.delete("/api/v1/conversations/conv-1/messages/msg-1/feedback")
    assert response.status_code == 204

    # Verify deleted
    result = await pg_session.execute(
        text("SELECT COUNT(*) FROM message_feedback WHERE conversation_id = 'conv-1' AND message_id = 'msg-1'")
    )
    assert result.scalar() == 0


@pytest.mark.asyncio
async def test_delete_feedback_not_found(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    await _create_conversation(pg_session, test_user_model.id)

    response = await client_with_db.delete("/api/v1/conversations/conv-1/messages/msg-999/feedback")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_invalid_rating(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    await _create_conversation(pg_session, test_user_model.id)

    response = await client_with_db.post(
        "/api/v1/conversations/conv-1/messages/msg-1/feedback",
        json={"rating": "meh"},
    )
    assert response.status_code == 422
