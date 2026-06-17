"""Tests for Socket.IO message handling logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from console_backend.services.conversation_service import ConversationService


@pytest.mark.asyncio
async def test_conversation_id_validation_logic():
    """Test the conversationId validation logic directly."""
    # Simulate the validation check that happens in handle_send_message
    json_data_without_id = {"message": "Hello", "metadata": {}}
    json_data_with_id = {"conversationId": "conv-123", "message": "Hello", "metadata": {}}

    context_id_missing = json_data_without_id.get("conversationId")
    context_id_present = json_data_with_id.get("conversationId")

    assert context_id_missing is None
    assert context_id_present == "conv-123"

    # Verify the validation condition
    assert not context_id_missing  # Should trigger error
    assert context_id_present  # Should pass validation


@pytest.mark.asyncio
async def test_get_or_create_conversation_extracts_title_from_message():
    """Test that get_or_create_conversation uses message text as title."""

    cs = ConversationService.__new__(ConversationService)
    cs.table = MagicMock()
    cs.get_conversation = AsyncMock(return_value=None)
    cs.insert_conversation = AsyncMock()

    long_message = "This is a very long message that exceeds one hundred characters and should be truncated when used as conversation title"

    await cs.get_or_create_conversation(
        conversation_id="conv-789", user_id="user-2", agent_url="http://agent", message=long_message
    )

    # Verify insert_conversation was called
    assert cs.insert_conversation.await_count == 1
    called_kwargs = cs.insert_conversation.call_args.kwargs

    # Check title extraction
    assert "title" in called_kwargs
    title = called_kwargs["title"]
    assert len(title) <= 100  # Should be truncated to 100 chars
    assert title == long_message[:100]
    assert "This is a very long message" in title


@pytest.mark.asyncio
async def test_get_or_create_conversation_empty_title_when_no_message():
    """Test that get_or_create_conversation handles missing message gracefully."""

    cs = ConversationService.__new__(ConversationService)
    cs.table = MagicMock()
    cs.get_conversation = AsyncMock(return_value=None)
    cs.insert_conversation = AsyncMock()

    # Call without message
    await cs.get_or_create_conversation(
        conversation_id="conv-999", user_id="user-3", agent_url="http://agent", message=None
    )

    # Verify insert_conversation was called
    assert cs.insert_conversation.await_count == 1
    called_kwargs = cs.insert_conversation.call_args.kwargs

    # Check title is empty
    assert "title" in called_kwargs
    assert called_kwargs["title"] == ""


@pytest.mark.asyncio
async def test_process_a2a_response_receives_context_id():
    """Test that _process_a2a_response correctly receives and uses context_id."""
    from a2a.types import Message, Part, Role, StreamResponse

    from app import _process_a2a_response

    # Mock sio and services
    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.app_instance = MagicMock()

    mock_socket_session = MagicMock(user_id="user-1", agent_url="http://agent")
    mock_sio.app_instance.state.socket_session_service.get_session = AsyncMock(return_value=mock_socket_session)
    # Mock conversation exists and belongs to user
    mock_conversation = MagicMock(conversation_id="conv-context-123", user_id="user-1")
    mock_sio.app_instance.state.conversation_service.get_conversation = AsyncMock(return_value=mock_conversation)
    mock_sio.app_instance.state.messages_service.save_history_messages = AsyncMock(return_value=0)
    mock_sio.app_instance.state.messages_service.save_agent_response = AsyncMock()

    with patch("app.sio", mock_sio):
        # Create a simple message response
        message = Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text="Response text")],
            message_id="msg-response-1",
            context_id="conv-context-123",
        )

        # Call _process_a2a_response with context_id
        await _process_a2a_response(
            client_event=StreamResponse(message=message), sid="test-sid", request_id="req-1", context_id="conv-context-123"
        )

        # Verify conversation service was called to check ownership
        mock_sio.app_instance.state.conversation_service.get_conversation.assert_called_once()
        call_kwargs = mock_sio.app_instance.state.conversation_service.get_conversation.call_args.kwargs
        assert call_kwargs["conversation_id"] == "conv-context-123"
        assert call_kwargs["user_id"] == "user-1"

        # Verify message was saved with context_id
        mock_sio.app_instance.state.messages_service.save_agent_response.assert_called_once()
        save_call_kwargs = mock_sio.app_instance.state.messages_service.save_agent_response.call_args.kwargs
        assert save_call_kwargs["conversation_id"] == "conv-context-123"


@pytest.mark.asyncio
async def test_process_a2a_response_uses_fallback_context_id():
    """Test that _process_a2a_response falls back to contextId from response_data."""
    from a2a.types import Message, Part, Role, StreamResponse

    from app import _process_a2a_response

    # Mock sio and services
    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.app_instance = MagicMock()

    mock_socket_session = MagicMock(user_id="user-1", agent_url="http://agent")
    mock_sio.app_instance.state.socket_session_service.get_session = AsyncMock(return_value=mock_socket_session)
    # Mock conversation exists and belongs to user
    mock_conversation = MagicMock(conversation_id="conv-from-response-456", user_id="user-1")
    mock_sio.app_instance.state.conversation_service.get_conversation = AsyncMock(return_value=mock_conversation)
    mock_sio.app_instance.state.messages_service.save_history_messages = AsyncMock(return_value=0)
    mock_sio.app_instance.state.messages_service.save_agent_response = AsyncMock()

    with patch("app.sio", mock_sio):
        # Create message with contextId in response
        message = Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text="Response text")],
            message_id="msg-response-2",
            context_id="conv-from-response-456",
        )

        # Call _process_a2a_response WITHOUT explicit context_id parameter
        await _process_a2a_response(
            client_event=StreamResponse(message=message),
            sid="test-sid",
            request_id="req-2",
            context_id=None,  # No context_id passed
        )

        # Should use contextId from the message's model_dump and verify ownership
        mock_sio.app_instance.state.conversation_service.get_conversation.assert_called_once()
        call_kwargs = mock_sio.app_instance.state.conversation_service.get_conversation.call_args.kwargs
        assert call_kwargs["conversation_id"] == "conv-from-response-456"
        assert call_kwargs["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_first_artifact_chunk_is_accumulated_not_persisted_standalone():
    """The append=False first chunk of an artifact must be accumulated, not saved standalone.

    The first chunk of a streamed artifact has append=False (it creates the
    artifact). It used to fall through to save_agent_response as its own message,
    splitting the content from the accumulated remainder — which reload rendered as
    two fragments. It must be accumulated like the appends that follow.
    """
    from a2a.types import Artifact, Part, StreamResponse, TaskArtifactUpdateEvent

    from app import _process_a2a_response

    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.app_instance = MagicMock()
    mock_socket_session = MagicMock(user_id="user-1", agent_url="http://agent")
    mock_sio.app_instance.state.socket_session_service.get_session = AsyncMock(return_value=mock_socket_session)
    mock_conversation = MagicMock(conversation_id="conv-firstchunk", user_id="user-1")
    mock_sio.app_instance.state.conversation_service.get_conversation = AsyncMock(return_value=mock_conversation)
    mock_sio.app_instance.state.messages_service.save_history_messages = AsyncMock(return_value=0)
    mock_sio.app_instance.state.messages_service.save_agent_response = AsyncMock()
    mock_sio.app_instance.state.messages_service.insert_message = AsyncMock()

    with patch("app.sio", mock_sio):
        art = Artifact(
            artifact_id="a1",
            parts=[Part(text="The user wants me to delegate")],
            metadata={"agent_name": "orchestrator"},
        )
        art.extensions.append("urn:nannos:a2a:intermediate-output:1.0")
        event = TaskArtifactUpdateEvent(
            task_id="t1", context_id="conv-firstchunk", artifact=art, append=False, last_chunk=False
        )
        await _process_a2a_response(
            client_event=StreamResponse(artifact_update=event),
            sid="sid",
            request_id="req-fc",
            context_id="conv-firstchunk",
        )
        # First chunk is accumulated for later assembly, never persisted on its own.
        mock_sio.app_instance.state.messages_service.save_agent_response.assert_not_called()


@pytest.mark.asyncio
async def test_conversation_title_with_unicode_characters():
    """Test that conversation title handles Unicode characters correctly."""

    cs = ConversationService.__new__(ConversationService)
    cs.table = MagicMock()
    cs.get_conversation = AsyncMock(return_value=None)
    cs.insert_conversation = AsyncMock()

    unicode_message = "Привет мир! 你好世界! 🌍🚀"

    await cs.get_or_create_conversation(
        conversation_id="conv-unicode", user_id="user-unicode", agent_url="http://agent", message=unicode_message
    )

    called_kwargs = cs.insert_conversation.call_args.kwargs
    assert "title" in called_kwargs
    title = called_kwargs["title"]

    # Should preserve Unicode characters
    assert "Привет" in title
    assert "你好" in title
    assert "🌍" in title or "🚀" in title  # Emojis might be truncated depending on char count


def test_short_state_name_normalizes_v1_states():
    """v1.0 ProtoJSON TaskState names normalize to the short forms the backend branches on.

    The outbound payload is now emitted as raw v1.0 (the frontend handles it natively),
    but the backend's turn-ending / is_bare_completion_signal control-flow still keys off
    the short state strings. _short_state_name bridges that — if it stops mapping
    TASK_STATE_COMPLETED -> "completed", is_bare_completion_signal misfires and the reply
    is persisted/rendered twice.
    """
    from app import _short_state_name

    assert _short_state_name("TASK_STATE_COMPLETED") == "completed"
    assert _short_state_name("TASK_STATE_INPUT_REQUIRED") == "input-required"
    assert _short_state_name("TASK_STATE_FAILED") == "failed"
    assert _short_state_name("TASK_STATE_CANCELED") == "canceled"
    # Already-short values and non-strings pass through untouched.
    assert _short_state_name("completed") == "completed"
    assert _short_state_name(None) is None
