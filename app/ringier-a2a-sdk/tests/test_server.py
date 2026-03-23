"""Tests for server components."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import TaskState

from ringier_a2a_sdk.models import AgentStreamResponse
from ringier_a2a_sdk.server import AuthRequestContextBuilder, BaseAgentExecutor


class TestAuthRequestContextBuilder:
    """Tests for AuthRequestContextBuilder."""

    @pytest.mark.asyncio
    async def test_build_with_user_context(self):
        """Test building request context with user information."""
        builder = AuthRequestContextBuilder()

        # Set user context
        user_context = {
            "user_sub": "sub-123",
            "email": "test@example.com",
            "name": "Test User",
            "token": "jwt-token",
            "scopes": ["read"],
        }

        with patch("ringier_a2a_sdk.server.context_builder.current_user_context") as mock_context:
            mock_context.get.return_value = user_context

            # Build context (no params needed, just testing user context extraction)
            context = await builder.build(context_id="ctx-123")

            # Verify user_sub is set
            assert context.call_context is not None
            assert context.call_context.state is not None
            assert context.call_context.state["user_sub"] == "sub-123"
            assert context.call_context.state["user_email"] == "test@example.com"
            assert context.call_context.state["user_name"] == "Test User"
            assert context.call_context.state["user_token"] == "jwt-token"
            assert context.call_context.state["user_scopes"] == ["read"]

    @pytest.mark.asyncio
    async def test_build_without_user_context(self):
        """Test building request context without user information."""
        builder = AuthRequestContextBuilder()

        with patch("ringier_a2a_sdk.server.context_builder.current_user_context") as mock_context:
            mock_context.get.return_value = None

            # Build context
            context = await builder.build(context_id="ctx-123")

            # Should fall back to anonymous
            assert context.call_context is not None
            assert context.call_context.state is not None
            assert context.call_context.state["user_sub"] == "anonymous"

    @pytest.mark.asyncio
    async def test_build_with_existing_call_context(self):
        """Test building context with existing ServerCallContext."""
        builder = AuthRequestContextBuilder()
        existing_context = ServerCallContext()
        existing_context.state["existing_key"] = "existing_value"

        user_context = {"user_sub": "sub-123", "email": "test@example.com"}

        with patch("ringier_a2a_sdk.server.context_builder.current_user_context") as mock_context:
            mock_context.get.return_value = user_context

            context = await builder.build(context=existing_context)
            assert context.call_context is not None
            assert context.call_context.state is not None
            # Should preserve existing state
            assert context.call_context.state["existing_key"] == "existing_value"
            # Should add user info
            assert context.call_context.state["user_sub"] == "sub-123"


class TestBaseAgentExecutor:
    """Tests for BaseAgentExecutor."""

    @pytest.mark.asyncio
    async def test_executor_initialization(self):
        """Test executor initialization with agent."""
        mock_agent = Mock()
        executor = BaseAgentExecutor(mock_agent)

        assert executor.agent == mock_agent

    @pytest.mark.asyncio
    async def test_execute_with_valid_context(self):
        """Test successful execution with valid user context."""
        # Create mock agent
        mock_agent = Mock()

        async def mock_stream(query, user_config, task):
            yield AgentStreamResponse(state=TaskState.working, content="Processing...")
            yield AgentStreamResponse(state=TaskState.completed, content="Done")

        mock_agent.stream = mock_stream

        # Create executor
        executor = BaseAgentExecutor(mock_agent)

        # Create mock context with user info
        mock_context = Mock()
        mock_context.get_user_input.return_value = "test query"
        mock_context.current_task = None
        mock_context.message = Mock()

        call_context = Mock()
        call_context.state = {
            "user_sub": "sub-123",
            "user_token": "token-123",
            "user_name": "Test User",
            "user_email": "test@example.com",
        }
        mock_context.call_context = call_context

        # Create mock event queue
        mock_event_queue = AsyncMock()

        # Mock new_task
        with patch("ringier_a2a_sdk.server.executor.new_task") as mock_new_task:
            mock_task = Mock()
            mock_task.id = "task-1"
            mock_task.context_id = "ctx-1"
            mock_new_task.return_value = mock_task

            # Execute
            await executor.execute(mock_context, mock_event_queue)

            # Verify task was created and events were enqueued
            assert mock_event_queue.enqueue_event.call_count >= 1
            # First call should be the task
            assert mock_event_queue.enqueue_event.call_args_list[0][0][0].id == "task-1"

    @pytest.mark.asyncio
    async def test_handle_stream_item_working_state(self):
        """Test handling working state stream item."""
        mock_agent = Mock()
        executor = BaseAgentExecutor(mock_agent)

        mock_updater = AsyncMock()
        mock_task = Mock()
        mock_task.id = "task-1"
        mock_task.context_id = "ctx-1"

        item = AgentStreamResponse(state=TaskState.working, content="Processing...")

        with patch("ringier_a2a_sdk.server.executor.new_agent_text_message") as mock_msg:
            mock_msg.return_value = Mock()

            await executor._handle_stream_item(item, mock_updater, mock_task, "artifact-1")

            mock_updater.update_status.assert_called_once()
            call_args = mock_updater.update_status.call_args
            assert call_args[0][0] == TaskState.working

    @pytest.mark.asyncio
    async def test_handle_stream_item_completed_state(self):
        """Test handling completed state stream item."""
        mock_agent = Mock()
        executor = BaseAgentExecutor(mock_agent)

        mock_updater = AsyncMock()
        mock_task = Mock()
        mock_task.id = "task-1"
        mock_task.context_id = "ctx-1"

        item = AgentStreamResponse(state=TaskState.completed, content="Result")

        with patch("ringier_a2a_sdk.server.executor.Part") as mock_part:
            mock_part.return_value = Mock()

            await executor._handle_stream_item(item, mock_updater, mock_task, "artifact-1")

            mock_updater.add_artifact.assert_called_once()
            mock_updater.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_stream_item_failed_state(self):
        """Test handling failed state stream item."""
        mock_agent = Mock()
        executor = BaseAgentExecutor(mock_agent)

        mock_updater = AsyncMock()
        mock_task = Mock()
        mock_task.id = "task-1"
        mock_task.context_id = "ctx-1"

        item = AgentStreamResponse(state=TaskState.failed, content="Error occurred")

        with patch("ringier_a2a_sdk.server.executor.new_agent_text_message") as mock_msg:
            mock_msg.return_value = Mock()

            await executor._handle_stream_item(item, mock_updater, mock_task, "artifact-1")

            mock_updater.update_status.assert_called_once()
            call_args = mock_updater.update_status.call_args
            assert call_args[0][0] == TaskState.failed

    @pytest.mark.asyncio
    async def test_cancel_raises_unsupported(self):
        """Test that cancel operation raises UnsupportedOperationError."""
        from a2a.utils.errors import ServerError

        mock_agent = Mock()
        executor = BaseAgentExecutor(mock_agent)

        mock_context = Mock()
        mock_event_queue = AsyncMock()

        with pytest.raises(ServerError):
            await executor.cancel(mock_context, mock_event_queue)
