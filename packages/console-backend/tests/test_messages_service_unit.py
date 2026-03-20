"""Unit tests for playground_backend.services.messages_service."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from a2a.types import FilePart, FileWithUri, Part, TaskState, TextPart

from playground_backend.models.message import Message
from playground_backend.services.messages_service import (
    MessagesService,
    _parse_agent_response,
    _parse_status_update,
    _parse_task,
)


@pytest.mark.asyncio
async def test_save_agent_response_nested_and_flat_formats():
    ms = MessagesService()

    # Patch insert_message to capture args and return a Message
    called = {}

    async def fake_insert_message(**kwargs):
        called.update(kwargs)
        # return a Message instance similar to real
        return Message(
            conversation_id=kwargs.get("conversation_id", ""),
            sort_key=f"MSG#0#{kwargs.get('message_id', '')}",
            user_id=kwargs.get("user_id", ""),
            message_id=kwargs.get("message_id", ""),
            role=kwargs.get("role", ""),
            parts=kwargs.get("parts", []),
            created_at="2025-01-01T00:00:00+00:00",
            state=kwargs.get("state", "completed"),
            raw_payload=kwargs.get("raw_payload", ""),
            metadata=kwargs.get("metadata", {}),
            ttl=0,
            final=kwargs.get("final", False),
            kind=kwargs.get("kind", ""),
        )

    ms.insert_message = AsyncMock(side_effect=fake_insert_message)

    # Nested format with status-update kind
    nested = {
        "id": "agent-1",
        "final": True,
        "kind": "status-update",
        "status": {
            "state": "completed",
            "message": {
                "messageId": "msg-nested-1",
                "role": "assistant",
                "parts": [{"kind": "text", "text": "nested reply"}],
                "metadata": {"k": "v"},
            },
        },
    }

    res = await ms.save_agent_response(nested, conversation_id="conv-1", user_id="user-1")
    assert res is not None
    # insert_message should have been called with extracted nested values
    assert called["conversation_id"] == "conv-1"
    assert called["user_id"] == "user-1"
    assert called["message_id"] == "msg-nested-1"
    assert called["role"] == "assistant"
    assert isinstance(called["parts"], list) and called["parts"][0]["text"] == "nested reply"


def test_parse_status_update_with_nested_message():
    """Test parsing status-update with nested status.message."""
    response = {
        "contextId": "dde49b7a-b4b8-48ba-9276-11a42d820f22",
        "final": False,
        "kind": "status-update",
        "status": {
            "message": {
                "contextId": "dde49b7a-b4b8-48ba-9276-11a42d820f22",
                "kind": "message",
                "messageId": "f47bc0fc-12b4-42f0-a3d2-0e5786afda8f",
                "parts": [{"kind": "text", "text": "📋 **Plan created** (5 tasks)"}],
                "role": "agent",
                "taskId": "98ab980a-2209-42a5-aaae-55e93e3108e0",
            },
            "state": "working",
            "timestamp": "2025-11-21T09:23:15.724055+00:00",
        },
        "taskId": "98ab980a-2209-42a5-aaae-55e93e3108e0",
        "id": "09e43b4e-fe71-4057-bc00-db9e9b74ab80",
    }

    parsed = _parse_status_update(response)

    assert parsed["kind"] == "status-update"
    assert parsed["message_id"] == "f47bc0fc-12b4-42f0-a3d2-0e5786afda8f"
    assert parsed["role"] == "assistant"  # 'agent' normalized to 'assistant'

    assert parsed["state"] == TaskState.working
    assert parsed["final"] is False
    assert parsed["task_id"] == "98ab980a-2209-42a5-aaae-55e93e3108e0"
    assert len(parsed["parts"]) == 1
    assert parsed["parts"][0]["text"] == "📋 **Plan created** (5 tasks)"


def test_parse_status_update_without_message():
    """Test parsing status-update with only state (no nested message)."""
    response = {
        "contextId": "dde49b7a-b4b8-48ba-9276-11a42d820f22",
        "final": True,
        "kind": "status-update",
        "status": {"state": "completed", "timestamp": "2025-11-21T09:24:00.493443+00:00"},
        "taskId": "98ab980a-2209-42a5-aaae-55e93e3108e0",
        "id": "09e43b4e-fe71-4057-bc00-db9e9b74ab80",
    }

    parsed = _parse_status_update(response)

    assert parsed["kind"] == "status-update"

    assert parsed["state"] == TaskState.completed
    assert parsed["final"] is True
    assert parsed["task_id"] == "98ab980a-2209-42a5-aaae-55e93e3108e0"
    # Should create synthetic status part
    assert len(parsed["parts"]) == 1
    assert "Status: completed" in parsed["parts"][0]["text"]
    assert "2025-11-21T09:24:00.493443+00:00" in parsed["parts"][0]["text"]


def test_parse_status_update_with_artifact():
    """Test parsing artifact-update kind."""
    response = {
        "artifact": {
            "artifactId": "96b01ff4-f2fb-456d-9db6-6b0d036e4771",
            "name": "orchestrator_result",
            "parts": [{"kind": "text", "text": "Your trip to Paris is fully planned! Here's a summary..."}],
        },
        "contextId": "dde49b7a-b4b8-48ba-9276-11a42d820f22",
        "kind": "artifact-update",
        "taskId": "98ab980a-2209-42a5-aaae-55e93e3108e0",
        "id": "09e43b4e-fe71-4057-bc00-db9e9b74ab80",
    }

    parsed = _parse_status_update(response)

    assert parsed["kind"] == "artifact-update"
    assert parsed["message_id"] == "96b01ff4-f2fb-456d-9db6-6b0d036e4771"
    assert parsed["task_id"] == "98ab980a-2209-42a5-aaae-55e93e3108e0"
    assert len(parsed["parts"]) == 1
    assert "Your trip to Paris" in parsed["parts"][0]["text"]


def test_parse_task_with_history():
    """Test parsing task kind with history."""
    response = {
        "contextId": "dde49b7a-b4b8-48ba-9276-11a42d820f22",
        "history": [
            {
                "contextId": "dde49b7a-b4b8-48ba-9276-11a42d820f22",
                "kind": "message",
                "messageId": "09e43b4e-fe71-4057-bc00-db9e9b74ab80",
                "metadata": {"user_id": "0490f8d6-67ee-439b-8178-6ed66a72b0c9"},
                "parts": [{"kind": "text", "text": "Help me plan a trip to Paris"}],
                "role": "user",
                "taskId": "98ab980a-2209-42a5-aaae-55e93e3108e0",
            }
        ],
        "id": "98ab980a-2209-42a5-aaae-55e93e3108e0",
        "kind": "task",
        "status": {"state": "submitted"},
    }

    parsed = _parse_task(response)

    assert parsed["kind"] == "task"

    assert parsed["state"] == TaskState.submitted
    assert parsed["task_id"] == "98ab980a-2209-42a5-aaae-55e93e3108e0"
    assert parsed["message_id"] == "98ab980a-2209-42a5-aaae-55e93e3108e0"
    assert "history" in parsed
    assert len(parsed["history"]) == 1
    assert parsed["history"][0]["messageId"] == "09e43b4e-fe71-4057-bc00-db9e9b74ab80"
    # Should create task event part
    assert len(parsed["parts"]) == 1
    assert "Task submitted" in parsed["parts"][0]["text"]


def test_parse_agent_response_dispatches_correctly():
    """Test that _parse_agent_response dispatches to correct parser."""
    # status-update should use _parse_status_update
    status_response = {"kind": "status-update", "status": {"state": "working"}, "id": "test-1"}
    parsed = _parse_agent_response(status_response)
    assert parsed["kind"] == "status-update"

    assert parsed["state"] == TaskState.working

    # task should use _parse_task
    task_response = {"kind": "task", "id": "task-1", "status": {"state": "submitted"}}
    parsed = _parse_agent_response(task_response)
    assert parsed["kind"] == "task"

    assert parsed["state"] == TaskState.submitted

    # unknown kind should raise ValueError
    unknown_response = {"kind": "custom-type", "messageId": "msg-1", "parts": [{"text": "hello"}]}
    with pytest.raises(ValueError, match="Unsupported agent response kind: 'custom-type'"):
        _parse_agent_response(unknown_response)


@pytest.mark.asyncio
async def test_save_agent_response_with_history():
    """Test that save_agent_response calls save_history_messages when history present."""
    ms = MessagesService()

    insert_calls = []
    history_calls = []

    async def fake_insert(**kwargs):
        insert_calls.append(kwargs)
        return Message(
            conversation_id=kwargs["conversation_id"],
            sort_key=f"MSG#0#{kwargs['message_id']}",
            user_id=kwargs["user_id"],
            message_id=kwargs["message_id"],
            role=kwargs["role"],
            parts=kwargs["parts"],
            created_at="2025-01-01T00:00:00+00:00",
            state=kwargs["state"],
            raw_payload=kwargs["raw_payload"],
            metadata=kwargs["metadata"],
            ttl=0,
            final=kwargs["final"],
            kind=kwargs["kind"],
        )

    async def fake_save_history(history, conv_id, user_id):
        history_calls.append({"history": history, "conv_id": conv_id, "user_id": user_id})
        return len(history)

    ms.insert_message = AsyncMock(side_effect=fake_insert)

    task_response = {
        "kind": "task",
        "id": "task-123",
        "status": {"state": "submitted"},
        "history": [{"messageId": "hist-1", "role": "user", "parts": [{"text": "question"}]}],
    }

    result = await ms.save_agent_response(task_response, "conv-1", "user-1")

    assert result is not None
    assert len(insert_calls) == 1
    assert insert_calls[0]["message_id"] == "task-123"

    # History is no longer saved by the service; do not expect save_history_messages to be called
    assert not hasattr(ms, "save_history_messages")


# ============================================================================
# Tests for message file hydration
# ============================================================================


@pytest.mark.asyncio
async def test_hydrate_with_expired_urls():
    """Test that expired presigned URLs are regenerated."""

    ms = MessagesService()

    # Mock FileStorageService
    mock_storage = AsyncMock()
    mock_storage.generate_presigned_get_url = AsyncMock(
        return_value="https://bucket.s3.amazonaws.com/file.txt?fresh=true"
    )

    with patch("playground_backend.services.messages_service.FileStorageService") as mock_fs_class:
        mock_fs_class.return_value = mock_storage

        # Create expired URL
        expired_url = "s3://bucket/path/file.txt"

        # Create file part with expired URL
        file_part = Part(
            root=FilePart(kind="file", file=FileWithUri(uri=expired_url, name="test.txt", mime_type="text/plain"))
        )

        # Create message with file part
        message = Message(
            conversation_id="conv-1",
            sort_key="MSG#1#msg-1",
            user_id="user-1",
            message_id="msg-1",
            role="assistant",
            parts=[file_part],
            created_at="2025-01-01T00:00:00+00:00",
            state=TaskState.completed,
            metadata={"fileMetadata": {"test.txt": "s3://bucket/path/file.txt"}},
            ttl=0,
        )

        # Hydrate message
        hydrated = await ms.hydrate_message_files(message)

        # Verify URL was regenerated
        assert isinstance(hydrated.parts[0].root, FilePart)
        assert isinstance(hydrated.parts[0].root.file, FileWithUri)
        assert hydrated.parts[0].root.file.uri == "https://bucket.s3.amazonaws.com/file.txt?fresh=true"
        mock_storage.generate_presigned_get_url.assert_called_once_with("path/file.txt")


@pytest.mark.asyncio
async def test_hydrate_skips_valid_urls():
    """Test that valid presigned URLs are not regenerated."""

    ms = MessagesService()

    # Mock FileStorageService
    mock_storage = AsyncMock()

    with patch("playground_backend.services.messages_service.FileStorageService") as mock_fs_class:
        mock_fs_class.return_value = mock_storage

        # Create valid URL (expires in 30 minutes)
        future_date = datetime.now(timezone.utc) - timedelta(minutes=30)
        date_str = future_date.strftime("%Y%m%dT%H%M%SZ")
        valid_url = f"https://bucket.s3.amazonaws.com/file.txt?X-Amz-Date={date_str}&X-Amz-Expires=3600"

        file_part = Part(
            root=FilePart(kind="file", file=FileWithUri(uri=valid_url, name="test.txt", mime_type="text/plain"))
        )

        message = Message(
            conversation_id="conv-1",
            sort_key="MSG#1#msg-1",
            user_id="user-1",
            message_id="msg-1",
            role="assistant",
            parts=[file_part],
            created_at="2025-01-01T00:00:00+00:00",
            state=TaskState.completed,
            metadata={"fileMetadata": {"test.txt": "s3://bucket/path/file.txt"}},
            ttl=0,
        )

        # Hydrate message
        hydrated = await ms.hydrate_message_files(message)

        # Verify URL was NOT regenerated
        assert isinstance(hydrated.parts[0].root, FilePart)
        assert isinstance(hydrated.parts[0].root.file, FileWithUri)
        assert hydrated.parts[0].root.file.uri == valid_url
        mock_storage.generate_presigned_get_url.assert_not_called()


@pytest.mark.asyncio
async def test_hydrate_with_no_file_metadata():
    """Test that hydration skips when no file metadata present."""

    ms = MessagesService()

    file_part = Part(
        root=FilePart(
            kind="file", file=FileWithUri(uri="https://example.com/file.txt", name="test.txt", mime_type="text/plain")
        )
    )

    message = Message(
        conversation_id="conv-1",
        sort_key="MSG#1#msg-1",
        user_id="user-1",
        message_id="msg-1",
        role="assistant",
        parts=[file_part],
        created_at="2025-01-01T00:00:00+00:00",
        state=TaskState.completed,
        metadata={},  # No fileMetadata
        ttl=0,
    )

    # Should return same message without changes
    hydrated = await ms.hydrate_message_files(message)
    assert hydrated == message


@pytest.mark.asyncio
async def test_hydrate_multiple_files():
    """Test hydrating message with multiple file parts."""

    ms = MessagesService()

    mock_storage = AsyncMock()
    mock_storage.generate_presigned_get_url = AsyncMock(
        side_effect=[
            "https://bucket.s3.amazonaws.com/file1.txt?fresh=true",
            "https://bucket.s3.amazonaws.com/file2.pdf?fresh=true",
        ]
    )

    with patch("playground_backend.services.messages_service.FileStorageService") as mock_fs_class:
        mock_fs_class.return_value = mock_storage
        expired_url = "s3://bucket/path/file.txt"

        text_part = Part(root=TextPart(kind="text", text="See attachments"))
        file_part1 = Part(
            root=FilePart(kind="file", file=FileWithUri(uri=expired_url, name="file1.txt", mime_type="text/plain"))
        )
        file_part2 = Part(
            root=FilePart(kind="file", file=FileWithUri(uri=expired_url, name="file2.pdf", mime_type="application/pdf"))
        )

        message = Message(
            conversation_id="conv-1",
            sort_key="MSG#1#msg-1",
            user_id="user-1",
            message_id="msg-1",
            role="assistant",
            parts=[text_part, file_part1, file_part2],
            created_at="2025-01-01T00:00:00+00:00",
            state=TaskState.completed,
            ttl=0,
        )

        hydrated = await ms.hydrate_message_files(message)

        # Text part should be unchanged
        assert isinstance(hydrated.parts[0].root, TextPart)
        assert hydrated.parts[0].root.text == "See attachments"
        # File parts should be updated
        assert isinstance(hydrated.parts[1].root, FilePart)
        assert isinstance(hydrated.parts[1].root.file, FileWithUri)
        assert isinstance(hydrated.parts[2].root, FilePart)
        assert isinstance(hydrated.parts[2].root.file, FileWithUri)
        assert hydrated.parts[1].root.file.uri == "https://bucket.s3.amazonaws.com/file1.txt?fresh=true"
        assert hydrated.parts[2].root.file.uri == "https://bucket.s3.amazonaws.com/file2.pdf?fresh=true"
        assert mock_storage.generate_presigned_get_url.call_count == 2


@pytest.mark.asyncio
async def test_hydrate_multiple_messages():
    """Test batch hydration of multiple messages."""

    ms = MessagesService()

    mock_storage = AsyncMock()
    mock_storage.generate_presigned_get_url = AsyncMock(
        side_effect=[
            "https://bucket.s3.amazonaws.com/msg1.txt?fresh=true",
            "https://bucket.s3.amazonaws.com/msg2.txt?fresh=true",
        ]
    )

    with patch("playground_backend.services.messages_service.FileStorageService") as mock_fs_class:
        mock_fs_class.return_value = mock_storage

        expired_url = "s3://bucket/path/file.txt"

        msg1 = Message(
            conversation_id="conv-1",
            sort_key="MSG#1#msg-1",
            user_id="user-1",
            message_id="msg-1",
            role="assistant",
            parts=[Part(root=FilePart(kind="file", file=FileWithUri(uri=expired_url, name="file1.txt")))],
            created_at="2025-01-01T00:00:00+00:00",
            state=TaskState.completed,
            metadata={"fileMetadata": {"file1.txt": "s3://file1.txt"}},
            ttl=0,
        )

        msg2 = Message(
            conversation_id="conv-1",
            sort_key="MSG#2#msg-2",
            user_id="user-1",
            message_id="msg-2",
            role="assistant",
            parts=[Part(root=FilePart(kind="file", file=FileWithUri(uri=expired_url, name="file2.txt")))],
            created_at="2025-01-01T00:01:00+00:00",
            state=TaskState.completed,
            metadata={"fileMetadata": {"file2.txt": "s3://file2.txt"}},
            ttl=0,
        )

        hydrated = await ms.hydrate_messages_files([msg1, msg2])

        assert len(hydrated) == 2
        assert isinstance(hydrated[0].parts[0].root, FilePart)
        assert isinstance(hydrated[1].parts[0].root, FilePart)
        assert isinstance(hydrated[0].parts[0].root.file, FileWithUri)
        assert isinstance(hydrated[1].parts[0].root.file, FileWithUri)
        assert hydrated[0].parts[0].root.file.uri == "https://bucket.s3.amazonaws.com/msg1.txt?fresh=true"
        assert hydrated[1].parts[0].root.file.uri == "https://bucket.s3.amazonaws.com/msg2.txt?fresh=true"
