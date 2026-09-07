"""Tests for `backend.routers.conversation_router`."""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console_backend.models.user import UserStatus

# Ensure code chooses auto credentials path during imports (avoid boto3 local credentials)
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

# Create test app and client
from console_backend.routers import conversation_router

app = FastAPI()
app.include_router(conversation_router.router)
# Ensure tests run with an authenticated user by default
app.dependency_overrides[conversation_router.require_auth_or_bearer_token] = lambda: MagicMock(
    id="test-user-id", email="test@example.com", is_administrator=False
)

# Set up a default mock conversation_service on app.state
_default_mock_conversation_service = MagicMock()
app.state.conversation_service = _default_mock_conversation_service

client = TestClient(app)


@pytest.fixture
def mock_conversations():
    """Return list of mock conversation objects."""
    return [
        MagicMock(
            conversation_id="conv1",
            session_ids=["session1"],
            user_id="0490f8d6-67ee-439b-8178-6ed66a72b0c9",
            started_at=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
            last_message_at=datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc),
            status=UserStatus.ACTIVE,
            message_count=5,
            metadata={"k": "v"},
            title="Test Conversation 1",
            agent_url="http://agent.example",
        ),
        MagicMock(
            conversation_id="conv2",
            session_ids=["session2", "session3"],
            user_id="0490f8d6-67ee-439b-8178-6ed66a72b0c9",
            started_at=datetime(2024, 12, 31, 12, 0, tzinfo=timezone.utc),
            last_message_at=datetime(2025, 1, 1, 13, 0, tzinfo=timezone.utc),
            status="completed",
            message_count=2,
            metadata={},
            title="Test Conversation 2",
            agent_url=None,
        ),
    ]


def test_get_conversations_success(mock_conversations):
    """Successful retrieval returns serialized conversations and correct count."""
    mock_service = MagicMock()
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=mock_conversations)
    app.state.conversation_service = mock_service

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9&limit=20")

    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "0490f8d6-67ee-439b-8178-6ed66a72b0c9"
    assert data["count"] == 2
    assert len(data["conversations"]) == 2
    # Check first conversation fields
    conv0 = data["conversations"][0]
    assert conv0["conversation_id"] == "conv1"
    assert conv0["status"] == "active"
    assert conv0["metadata"] == {"k": "v"}
    assert conv0["agent_url"] == "http://agent.example"


def test_get_conversations_limit_validation_low():
    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9&limit=0")
    assert resp.status_code == 400
    assert "Limit must be between 1 and 50" in resp.json()["detail"]


def test_get_conversations_limit_validation_high():
    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9&limit=51")
    assert resp.status_code == 400
    assert "Limit must be between 1 and 50" in resp.json()["detail"]


def test_get_conversations_empty():
    mock_service = MagicMock()
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=[])
    app.state.conversation_service = mock_service

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["conversations"] == []


def test_get_conversations_service_error():
    mock_service = MagicMock()
    mock_service.get_conversations_by_user_id = AsyncMock(side_effect=Exception("boom"))
    app.state.conversation_service = mock_service

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9")
    assert resp.status_code == 500
    assert "Failed to retrieve conversations" in resp.json()["detail"]


def test_datetime_serialization(mock_conversations):
    mock_service = MagicMock()
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=[mock_conversations[0]])
    app.state.conversation_service = mock_service

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9")
    assert resp.status_code == 200
    conv = resp.json()["conversations"][0]
    assert conv["started_at"].endswith("+00:00")
    assert conv["last_message_at"].endswith("+00:00")


@patch("console_backend.routers.conversation_router.config")
def test_get_conversations_permission(mock_config):
    """Test that non-local environment enforces user can only request their own conversations."""
    # Set up non-local environment
    mock_config.is_local.return_value = False
    mock_service = MagicMock()
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=[])
    app.state.conversation_service = mock_service

    # Override the dependency to inject a specific user
    test_user = MagicMock(id="user-1", email="user1@test.com")
    app.dependency_overrides[conversation_router.require_auth_or_bearer_token] = lambda: test_user

    try:
        # Requesting different user's conversations should be forbidden
        resp = client.get("/api/v1/conversations/?user_id=other-user&limit=20")
        assert resp.status_code == 403
        assert "Insufficient permissions" in resp.json()["detail"]

        # Requesting own conversations should succeed
        resp = client.get("/api/v1/conversations/?user_id=user-1&limit=20")
        assert resp.status_code == 200
    finally:
        # Restore default override
        app.dependency_overrides[conversation_router.require_auth_or_bearer_token] = lambda: MagicMock(
            id="test-user-id", email="test@example.com", is_administrator=False
        )


def _conv(conversation_id: str, metadata: dict) -> MagicMock:
    return MagicMock(
        conversation_id=conversation_id,
        user_id="0490f8d6-67ee-439b-8178-6ed66a72b0c9",
        started_at=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        last_message_at=datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc),
        status="active",
        metadata=metadata,
        title="t",
        agent_url="",
        sub_agent_config_hash=None,
    )


def test_get_conversations_embedded_scope_filter():
    """embedded_sub_agent_id scopes the list to ONE application's conversations —
    a host page must never receive console or other-app conversation titles."""
    mock_service = MagicMock()
    mock_service.get_conversations_by_user_id = AsyncMock(
        return_value=[
            _conv("app42", {"embedded_sub_agent_id": "42"}),
            _conv("console-conv", {}),
            _conv("app7", {"embedded_sub_agent_id": "7"}),
        ]
    )
    app.state.conversation_service = mock_service

    resp = client.get(
        "/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9&embedded_sub_agent_id=42"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert [c["conversation_id"] for c in data["conversations"]] == ["app42"]
    assert data["count"] == 1


def test_get_conversations_unfiltered_includes_embedded():
    """Without the param (console main list) embedded conversations stay visible
    (they render with a badge and read-only input client-side)."""
    mock_service = MagicMock()
    mock_service.get_conversations_by_user_id = AsyncMock(
        return_value=[
            _conv("app42", {"embedded_sub_agent_id": "42"}),
            _conv("console-conv", {}),
        ]
    )
    app.state.conversation_service = mock_service

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9")

    assert resp.status_code == 200
    assert {c["conversation_id"] for c in resp.json()["conversations"]} == {"app42", "console-conv"}


def test_delete_conversation_soft_deletes_and_returns_204():
    """DELETE archives the conversation — the row and its messages survive, it
    just stops being listed."""
    mock_service = MagicMock()
    mock_service.archive_conversation = AsyncMock(return_value=True)
    app.state.conversation_service = mock_service

    resp = client.delete("/api/v1/conversations/conv1")

    assert resp.status_code == 204
    mock_service.archive_conversation.assert_awaited_once_with(
        conversation_id="conv1", user_id="test-user-id"
    )


def test_delete_conversation_missing_or_not_yours_is_404():
    """Ownership is enforced inside the UPDATE, so someone else's conversation
    is indistinguishable from a missing one — nothing leaks about whose it is."""
    mock_service = MagicMock()
    mock_service.archive_conversation = AsyncMock(return_value=False)
    app.state.conversation_service = mock_service

    resp = client.delete("/api/v1/conversations/someone-elses")

    assert resp.status_code == 404


def test_rename_conversation_returns_204():
    """PATCH stores the new name; the marker that protects it lives in the
    service, not here."""
    mock_service = MagicMock()
    mock_service.rename_conversation = AsyncMock(return_value=True)
    app.state.conversation_service = mock_service

    resp = client.patch("/api/v1/conversations/conv1", json={"title": "  Q3   pacing  "})

    assert resp.status_code == 204
    # Whitespace is collapsed before it reaches the database.
    mock_service.rename_conversation.assert_awaited_once_with(
        conversation_id="conv1", user_id="test-user-id", title="Q3 pacing"
    )


def test_rename_conversation_missing_or_not_yours_is_404():
    """Same as delete: ownership is enforced in the UPDATE, so nothing leaks."""
    mock_service = MagicMock()
    mock_service.rename_conversation = AsyncMock(return_value=False)
    app.state.conversation_service = mock_service

    resp = client.patch("/api/v1/conversations/someone-elses", json={"title": "Mine now"})

    assert resp.status_code == 404


@pytest.mark.parametrize("title", ["", "   ", "x" * 61])
def test_rename_conversation_rejects_unusable_names(title):
    """A blank name has no way back to the generated one, and an essay does not
    fit a list row — both are refused before the database sees them."""
    mock_service = MagicMock()
    mock_service.rename_conversation = AsyncMock(return_value=True)
    app.state.conversation_service = mock_service

    resp = client.patch("/api/v1/conversations/conv1", json={"title": title})

    assert resp.status_code == 422
    mock_service.rename_conversation.assert_not_awaited()
