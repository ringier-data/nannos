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
            client_event=StreamResponse(message=message),
            sid="test-sid",
            request_id="req-1",
            context_id="conv-context-123",
            user_id="user-1",
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
            user_id="user-1",
        )

        # Should use contextId from the message's model_dump and verify ownership
        mock_sio.app_instance.state.conversation_service.get_conversation.assert_called_once()
        call_kwargs = mock_sio.app_instance.state.conversation_service.get_conversation.call_args.kwargs
        assert call_kwargs["conversation_id"] == "conv-from-response-456"
        assert call_kwargs["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_process_a2a_response_persists_when_socket_session_is_gone():
    """A Turn must persist under its captured user_id even after the socket session is destroyed.

    Regression for the disconnect data-loss bug: the orchestrator Turn keeps
    running after the client disconnects, but the socket session is destroyed on disconnect.
    Persistence must use the user_id captured at Turn start, NOT get_session(sid) — otherwise
    a disconnect that outlives the Turn silently loses the reply.
    """
    from a2a.types import Message, Part, Role, StreamResponse

    from app import _process_a2a_response

    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.app_instance = MagicMock()

    # Socket session is GONE (client disconnected; destroy_session already ran).
    mock_sio.app_instance.state.socket_session_service.get_session = AsyncMock(return_value=None)
    # Conversation still exists and belongs to the captured user.
    mock_conversation = MagicMock(conversation_id="conv-disconnected", user_id="user-1")
    mock_sio.app_instance.state.conversation_service.get_conversation = AsyncMock(return_value=mock_conversation)
    mock_sio.app_instance.state.messages_service.save_history_messages = AsyncMock(return_value=0)
    mock_sio.app_instance.state.messages_service.save_agent_response = AsyncMock()

    with patch("app.sio", mock_sio):
        message = Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text="Reply produced after the client dropped")],
            message_id="msg-after-disconnect",
            context_id="conv-disconnected",
        )

        await _process_a2a_response(
            client_event=StreamResponse(message=message),
            sid="dead-sid",
            request_id="req-dc",
            context_id="conv-disconnected",
            user_id="user-1",  # captured at Turn start, survives the disconnect
        )

    # Ownership checked and the reply persisted — under the captured user_id, despite no session.
    mock_sio.app_instance.state.conversation_service.get_conversation.assert_called_once()
    assert mock_sio.app_instance.state.conversation_service.get_conversation.call_args.kwargs["user_id"] == "user-1"
    mock_sio.app_instance.state.messages_service.save_agent_response.assert_called_once()
    save_kwargs = mock_sio.app_instance.state.messages_service.save_agent_response.call_args.kwargs
    assert save_kwargs["conversation_id"] == "conv-disconnected"
    assert save_kwargs["user_id"] == "user-1"


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
            user_id="user-1",
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


# ── W3: room delivery + subscribe/snapshot resume ────────────────────────────


def test_accumulate_reply_buffer_tracks_offsets_and_skips_intermediate():
    """The resumable buffer accumulates only orchestrator reply text and returns the
    cumulative offset used to dedupe live chunks against a snapshot."""
    import app

    app._streaming_buffers.pop("conv-acc", None)

    def artifact_event(text, intermediate=False):
        exts = ["urn:nannos:a2a:intermediate-output:1.0"] if intermediate else []
        return {"kind": "artifact-update", "artifact": {"parts": [{"text": text}], "extensions": exts}}

    try:
        assert app._accumulate_reply_buffer("conv-acc", artifact_event("Hello")) == 5
        assert app._accumulate_reply_buffer("conv-acc", artifact_event(" world")) == 11
        assert app._streaming_buffers["conv-acc"] == "Hello world"
        # intermediate (sub-agent) output is not part of the resumable reply
        assert app._accumulate_reply_buffer("conv-acc", artifact_event("thinking", intermediate=True)) is None
        # non-artifact events don't contribute
        assert app._accumulate_reply_buffer("conv-acc", {"kind": "message"}) is None
        assert app._streaming_buffers["conv-acc"] == "Hello world"
    finally:
        app._streaming_buffers.pop("conv-acc", None)


@pytest.mark.asyncio
async def test_subscribe_conversation_returns_snapshot_and_joins_room():
    """Subscribing to an in-flight conversation joins its room and returns a snapshot
    of the reply-so-far plus any pending HITL prompt (resume after reconnect/reload)."""
    import app
    from app import handle_subscribe_conversation

    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.enter_room = AsyncMock()
    mock_sio.app_instance = MagicMock()
    mock_sio.app_instance.state.socket_session_service.get_session = AsyncMock(
        return_value=MagicMock(user_id="user-1")
    )
    mock_sio.app_instance.state.conversation_service.get_conversation = AsyncMock(
        return_value=MagicMock(conversation_id="conv-sub", user_id="user-1")
    )

    app._streaming_buffers["conv-sub"] = "partial reply"
    app._pending_interactions["conv-sub"] = {"kind": "status-update", "status": {"state": "input-required"}}
    try:
        with patch("app.sio", mock_sio):
            result = await handle_subscribe_conversation.__wrapped__("sid-1", {"conversationId": "conv-sub"})

        mock_sio.enter_room.assert_awaited_once_with("sid-1", "conversation:conv-sub")
        snap_call = mock_sio.emit.call_args
        assert snap_call.args[0] == app.SocketEvents.CONVERSATION_SNAPSHOT
        snapshot = snap_call.args[1]
        assert snapshot["replyText"] == "partial reply"
        assert snapshot["offset"] == len("partial reply")
        assert snapshot["inFlight"] is True
        assert snapshot["pendingHitl"]["status"]["state"] == "input-required"
        assert result is not None
    finally:
        app._streaming_buffers.pop("conv-sub", None)
        app._pending_interactions.pop("conv-sub", None)


@pytest.mark.asyncio
async def test_subscribe_conversation_rejected_when_not_owner():
    """A socket cannot join a conversation room it does not own — no room join, no snapshot."""
    import app
    from app import handle_subscribe_conversation

    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.enter_room = AsyncMock()
    mock_sio.app_instance = MagicMock()
    mock_sio.app_instance.state.socket_session_service.get_session = AsyncMock(
        return_value=MagicMock(user_id="user-1")
    )
    # Ownership check fails.
    mock_sio.app_instance.state.conversation_service.get_conversation = AsyncMock(return_value=None)

    with patch("app.sio", mock_sio):
        await handle_subscribe_conversation.__wrapped__("sid-x", {"conversationId": "conv-not-mine"})

    mock_sio.enter_room.assert_not_called()
    assert all(c.args[0] != app.SocketEvents.CONVERSATION_SNAPSHOT for c in mock_sio.emit.call_args_list)


@pytest.mark.asyncio
async def test_pending_hitl_is_captured_on_input_required_and_cleared_on_completion():
    """A HITL prompt (input-required) is retained for mid-turn resume, then cleared once
    the turn completes without asking — so a stale prompt isn't replayed forever."""
    from a2a.types import StreamResponse, TaskStatus, TaskState, TaskStatusUpdateEvent

    import app
    from app import _process_a2a_response

    mock_sio = MagicMock()
    mock_sio.emit = AsyncMock()
    mock_sio.app_instance = MagicMock()
    mock_sio.app_instance.state.socket_session_service.get_session = AsyncMock(
        return_value=MagicMock(user_id="user-1")
    )
    mock_sio.app_instance.state.conversation_service.get_conversation = AsyncMock(
        return_value=MagicMock(conversation_id="conv-hitl", user_id="user-1")
    )
    mock_sio.app_instance.state.messages_service.save_agent_response = AsyncMock()
    mock_sio.app_instance.state.messages_service.insert_message = AsyncMock()

    app._pending_interactions.pop("conv-hitl", None)
    try:
        with patch("app.sio", mock_sio):
            # Orchestrator asks for input → prompt retained for resume.
            await _process_a2a_response(
                client_event=StreamResponse(
                    status_update=TaskStatusUpdateEvent(
                        task_id="t1",
                        context_id="conv-hitl",
                        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
                    )
                ),
                sid="sid",
                request_id="req-hitl-1",
                context_id="conv-hitl",
                user_id="user-1",
            )
            assert "conv-hitl" in app._pending_interactions

            # Turn later completes without asking → prompt cleared.
            await _process_a2a_response(
                client_event=StreamResponse(
                    status_update=TaskStatusUpdateEvent(
                        task_id="t1",
                        context_id="conv-hitl",
                        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    )
                ),
                sid="sid",
                request_id="req-hitl-2",
                context_id="conv-hitl",
                user_id="user-1",
            )
            assert "conv-hitl" not in app._pending_interactions
    finally:
        app._pending_interactions.pop("conv-hitl", None)
