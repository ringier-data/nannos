"""Tests for `backend.routers.conversation_router`."""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from playground_backend.models.user import UserStatus

# Ensure code chooses auto credentials path during imports (avoid boto3 local credentials)
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

# Create test app and client
from playground_backend.routers import conversation_router

app = FastAPI()
app.include_router(conversation_router.router)
# Ensure tests run with an authenticated user by default
app.dependency_overrides[conversation_router.require_auth] = lambda: MagicMock(
    id="test-user-id", email="test@example.com", is_administrator=False
)
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


@patch("playground_backend.routers.conversation_router.conversation_service")
def test_get_conversations_success(mock_service, mock_conversations):
    """Successful retrieval returns serialized conversations and correct count."""
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=mock_conversations)

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


@patch("playground_backend.routers.conversation_router.conversation_service")
def test_get_conversations_empty(mock_service):
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=[])

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["conversations"] == []


@patch("playground_backend.routers.conversation_router.conversation_service")
def test_get_conversations_service_error(mock_service):
    mock_service.get_conversations_by_user_id = AsyncMock(side_effect=Exception("boom"))

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9")
    assert resp.status_code == 500
    assert "Failed to retrieve conversations" in resp.json()["detail"]


@patch("playground_backend.routers.conversation_router.conversation_service")
def test_datetime_serialization(mock_service, mock_conversations):
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=[mock_conversations[0]])

    resp = client.get("/api/v1/conversations/?user_id=0490f8d6-67ee-439b-8178-6ed66a72b0c9")
    assert resp.status_code == 200
    conv = resp.json()["conversations"][0]
    assert conv["started_at"].endswith("+00:00")
    assert conv["last_message_at"].endswith("+00:00")


@patch("playground_backend.routers.conversation_router.conversation_service")
@patch("playground_backend.routers.conversation_router.config")
def test_get_conversations_permission(mock_config, mock_service):
    """Test that non-local environment enforces user can only request their own conversations."""
    # Set up non-local environment
    mock_config.is_local.return_value = False
    mock_service.get_conversations_by_user_id = AsyncMock(return_value=[])

    # Override the dependency to inject a specific user
    test_user = MagicMock(id="user-1", email="user1@test.com")
    app.dependency_overrides[conversation_router.require_auth] = lambda: test_user

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
        app.dependency_overrides[conversation_router.require_auth] = lambda: MagicMock(
            id="test-user-id", email="test@example.com", is_administrator=False
        )
