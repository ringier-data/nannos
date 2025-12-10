from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from aiodynamo.client import Client
from aiodynamo.credentials import Key, StaticCredentials
from aiodynamo.http.httpx import HTTPX

from playground_backend.services.conversation_service import ConversationService


@pytest_asyncio.fixture
async def conversation_service(dynamodb_tables, mock_config):
    """Create ConversationService instance with real DynamoDB table."""
    http_client = httpx.AsyncClient()
    client = Client(
        HTTPX(http_client),
        StaticCredentials(Key("test", "test")),
        mock_config.dynamodb.region,
        endpoint=dynamodb_tables,
    )

    service = ConversationService()
    service.client = client
    service.table = client.table(mock_config.dynamodb.conversations_table)

    yield service
    await http_client.aclose()


@pytest.mark.asyncio
async def test_get_or_create_conversation_creates_with_title_from_history():
    cs = ConversationService.__new__(ConversationService)
    # mock table and methods
    cs.table = MagicMock()
    cs.get_conversation = AsyncMock(return_value=None)
    cs.insert_conversation = AsyncMock()

    message = "Hello world from user"

    # Call get_or_create_conversation with message
    await cs.get_or_create_conversation(
        conversation_id="conv-123", user_id="user-1", agent_url="http://agent", message=message
    )

    # insert_conversation should be called and title should contain 'Hello world'
    assert cs.insert_conversation.await_count == 1
    called_kwargs = cs.insert_conversation.call_args.kwargs
    assert "title" in called_kwargs
    assert "Hello world" in called_kwargs["title"]


@pytest.mark.asyncio
async def test_get_conversations_by_user_id_sort_and_filter(conversation_service):
    """Test that get_conversations_by_user_id filters by user and sorts correctly."""
    now = datetime.now(tz=timezone.utc)
    table = conversation_service.table

    # Create conversations for different users
    # Use UUIDv7-style IDs that are lexicographically time-ordered (newer = larger)
    # In real usage, uuid.uuid7() generates these automatically
    await table.put_item(
        item={
            "userId": "user1",
            "conversationId": "01900000-0000-7000-8000-000000000001",  # older UUIDv7-style
            "startedAt": (now - timedelta(days=2)).isoformat(),
            "lastMessageAt": (now - timedelta(days=1)).isoformat(),
            "ttl": int(now.timestamp() + conversation_service.conversation_ttl_seconds),
            "agentUrl": "http://a",
        }
    )

    await table.put_item(
        item={
            "userId": "user1",
            "conversationId": "01900000-0000-7000-8000-000000000002",  # newer UUIDv7-style
            "startedAt": (now - timedelta(days=1)).isoformat(),
            "lastMessageAt": now.isoformat(),
            "ttl": int(now.timestamp() + conversation_service.conversation_ttl_seconds),
            "agentUrl": "http://b",
        }
    )

    await table.put_item(
        item={
            "userId": "user2",
            "conversationId": "01900000-0000-7000-8000-000000000003",
            "startedAt": now.isoformat(),
            "lastMessageAt": now.isoformat(),
            "ttl": int(now.timestamp() + conversation_service.conversation_ttl_seconds),
            "agentUrl": "http://a",
        }
    )

    # Test query for user1
    res = await conversation_service.get_conversations_by_user_id("user1", limit=10)
    assert isinstance(res, list)
    assert len(res) == 2
    # With scan_forward=False and UUIDv7 sort key, newest (larger ID) comes first
    assert res[0].conversation_id == "01900000-0000-7000-8000-000000000002"
    assert res[1].conversation_id == "01900000-0000-7000-8000-000000000001"

    # Test again to verify consistency
    res2 = await conversation_service.get_conversations_by_user_id("user1", limit=10)
    assert len(res2) == 2
    assert res2[0].conversation_id == "01900000-0000-7000-8000-000000000002"


@pytest.mark.asyncio
async def test_get_conversation_with_composite_key(conversation_service):
    """Test that get_conversation uses userId + conversationId composite key."""
    now = datetime.now(tz=timezone.utc)
    table = conversation_service.table

    # Create a conversation
    await table.put_item(
        item={
            "userId": "user1",
            "conversationId": "01900000-0000-7000-8000-000000000001",
            "startedAt": now.isoformat(),
            "lastMessageAt": now.isoformat(),
            "ttl": int(now.timestamp() + conversation_service.conversation_ttl_seconds),
            "agentUrl": "http://agent",
            "title": "Test Conversation",
            "status": "active",
        }
    )

    # Test retrieving the conversation with correct user
    result = await conversation_service.get_conversation("01900000-0000-7000-8000-000000000001", "user1")
    assert result is not None
    assert result.conversation_id == "01900000-0000-7000-8000-000000000001"
    assert result.user_id == "user1"
    assert result.title == "Test Conversation"

    # Test that wrong user cannot retrieve the conversation
    result_wrong_user = await conversation_service.get_conversation("01900000-0000-7000-8000-000000000001", "user2")
    assert result_wrong_user is None

    # Test that non-existent conversation returns None
    result_not_found = await conversation_service.get_conversation("01900000-0000-7000-8000-000000000999", "user1")
    assert result_not_found is None
