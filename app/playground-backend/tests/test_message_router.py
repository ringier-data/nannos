"""Tests for `playground_backend.routers.message_router`."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Prevent AWS/boto3 local credential path during imports
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from playground_backend.routers import message_router

app = FastAPI()
app.include_router(message_router.router)
client = TestClient(app)

# Test app should treat requests as authenticated by default
app.dependency_overrides[message_router.require_auth] = lambda: MagicMock(
    id="user-1", email="test@example.com", is_administrator=False
)


def _make_mock_message(**kwargs):
    default = dict(
        conversation_id="conv-123",
        sort_key=1,
        user_id="user-1",
        message_id="m1",
        role="user",
        parts=["hello"],
        task_id=None,
        created_at="2025-01-01T12:00:00+00:00",
        state="sent",
        metadata={"k": "v"},
    )
    default.update(kwargs)
    return MagicMock(**default)


@patch("playground_backend.routers.message_router.messages_service")
def test_get_messages_success(mock_service):
    msgs = [
        _make_mock_message(message_id="m1", sort_key=1),
        _make_mock_message(message_id="m2", sort_key=2, role="assistant", parts=["reply"]),
    ]
    mock_service.get_messages_by_conversation = AsyncMock(return_value=msgs)
    mock_service.hydrate_messages_files = AsyncMock(return_value=msgs)

    resp = client.get("/api/v1/messages/conv-123?limit=100")
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversation_id"] == "conv-123"
    assert data["count"] == 2
    assert data["messages"][0]["message_id"] == "m1"
    assert data["messages"][1]["role"] == "assistant"
    mock_service.get_messages_by_conversation.assert_awaited_once_with("conv-123", "user-1", limit=100)


def test_limit_validation_low():
    resp = client.get("/api/v1/messages/conv-123?limit=0")
    assert resp.status_code == 400
    assert "Limit must be between 1 and 100" in resp.json()["detail"]


def test_limit_validation_high():
    resp = client.get("/api/v1/messages/conv-123?limit=101")
    assert resp.status_code == 400


@patch("playground_backend.routers.message_router.messages_service")
def test_empty_result(mock_service):
    mock_service.get_messages_by_conversation = AsyncMock(return_value=[])
    mock_service.hydrate_messages_files = AsyncMock(return_value=[])
    resp = client.get("/api/v1/messages/conv-123")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["messages"] == []


@patch("playground_backend.routers.message_router.messages_service")
def test_service_error(mock_service):
    mock_service.get_messages_by_conversation = AsyncMock(side_effect=Exception("db"))
    mock_service.hydrate_messages_files = AsyncMock()
    resp = client.get("/api/v1/messages/conv-123")
    assert resp.status_code == 500
    assert "Failed to retrieve messages" in resp.json()["detail"]


@patch("playground_backend.routers.message_router.messages_service")
def test_created_at_serialization(mock_service):
    mock_msg = _make_mock_message(created_at="2025-11-19T12:00:00+00:00")
    mock_service.get_messages_by_conversation = AsyncMock(return_value=[mock_msg])
    mock_service.hydrate_messages_files = AsyncMock(return_value=[mock_msg])
    resp = client.get("/api/v1/messages/conv-123")
    assert resp.status_code == 200
    conv = resp.json()["messages"][0]
    assert conv["created_at"].endswith("+00:00")
