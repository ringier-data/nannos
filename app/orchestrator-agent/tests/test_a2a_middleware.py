"""Tests for A2A Task Tracking Middleware."""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage

from app.subagents.middleware import A2ATaskTrackingMiddleware


class TestA2ATaskTrackingMiddlewareFallback:
    """Test fallback to general-purpose when subagent_type doesn't exist."""

    def setup_method(self):
        """Set up test fixtures."""
        self.middleware = A2ATaskTrackingMiddleware()

    def test_wrap_tool_call_fallback_to_general_purpose(self):
        """Test that wrap_tool_call falls back to general-purpose when subagent_type doesn't exist."""
        # Create a mock request with a non-existent subagent type
        request = MagicMock()
        request.tool_call = {
            "name": "task",
            "args": {
                "subagent_type": "non-existent-agent",
                "description": "test task",
            },
        }
        request.state = {}

        # Create handler that raises ValueError for non-existent agent, succeeds for general-purpose
        call_count = 0

        def mock_handler(req):
            nonlocal call_count
            call_count += 1
            subagent_type = req.tool_call["args"]["subagent_type"]
            if subagent_type != "general-purpose":
                raise ValueError(
                    f"Error: invoked agent of type {subagent_type}, the only allowed types are [`general-purpose`]"
                )
            return ToolMessage(content="success", tool_call_id="test-id")

        # Execute
        result = self.middleware.wrap_tool_call(request, mock_handler)

        # Verify fallback occurred
        assert call_count == 2  # First call fails, second succeeds
        assert request.tool_call["args"]["subagent_type"] == "general-purpose"
        assert isinstance(result, ToolMessage)
        assert result.content == "success"

    def test_wrap_tool_call_no_fallback_for_general_purpose(self):
        """Test that wrap_tool_call doesn't attempt fallback if already using general-purpose."""
        request = MagicMock()
        request.tool_call = {
            "name": "task",
            "args": {
                "subagent_type": "general-purpose",
                "description": "test task",
            },
        }
        request.state = {}

        def mock_handler(req):
            raise ValueError("Error: invoked agent of type general-purpose, the only allowed types are []")

        # Should raise the error since we can't fall back further
        with pytest.raises(ValueError):
            self.middleware.wrap_tool_call(request, mock_handler)

    def test_wrap_tool_call_no_fallback_for_other_errors(self):
        """Test that wrap_tool_call doesn't catch other ValueError types."""
        request = MagicMock()
        request.tool_call = {
            "name": "task",
            "args": {
                "subagent_type": "some-agent",
                "description": "test task",
            },
        }
        request.state = {}

        def mock_handler(req):
            raise ValueError("Some other error not related to subagent types")

        # Should raise the original error
        with pytest.raises(ValueError, match="Some other error"):
            self.middleware.wrap_tool_call(request, mock_handler)

    @pytest.mark.asyncio
    async def test_awrap_tool_call_fallback_to_general_purpose(self):
        """Test that awrap_tool_call falls back to general-purpose when subagent_type doesn't exist."""
        request = MagicMock()
        request.tool_call = {
            "name": "task",
            "args": {
                "subagent_type": "hallucinated-agent",
                "description": "test task",
            },
        }
        request.state = {}

        call_count = 0

        async def mock_handler(req):
            nonlocal call_count
            call_count += 1
            subagent_type = req.tool_call["args"]["subagent_type"]
            if subagent_type != "general-purpose":
                raise ValueError(
                    f"Error: invoked agent of type {subagent_type}, the only allowed types are [`general-purpose`]"
                )
            return ToolMessage(content="async success", tool_call_id="test-id")

        # Execute
        result = await self.middleware.awrap_tool_call(request, mock_handler)

        # Verify fallback occurred
        assert call_count == 2
        assert request.tool_call["args"]["subagent_type"] == "general-purpose"
        assert isinstance(result, ToolMessage)
        assert result.content == "async success"

    @pytest.mark.asyncio
    async def test_awrap_tool_call_no_fallback_for_general_purpose(self):
        """Test that awrap_tool_call doesn't attempt fallback if already using general-purpose."""
        request = MagicMock()
        request.tool_call = {
            "name": "task",
            "args": {
                "subagent_type": "general-purpose",
                "description": "test task",
            },
        }
        request.state = {}

        async def mock_handler(req):
            raise ValueError("Error: invoked agent of type general-purpose, the only allowed types are []")

        with pytest.raises(ValueError):
            await self.middleware.awrap_tool_call(request, mock_handler)

    @pytest.mark.asyncio
    async def test_awrap_tool_call_no_fallback_for_other_errors(self):
        """Test that awrap_tool_call doesn't catch other ValueError types."""
        request = MagicMock()
        request.tool_call = {
            "name": "task",
            "args": {
                "subagent_type": "some-agent",
                "description": "test task",
            },
        }
        request.state = {}

        async def mock_handler(req):
            raise ValueError("Different error type")

        with pytest.raises(ValueError, match="Different error type"):
            await self.middleware.awrap_tool_call(request, mock_handler)

    def test_wrap_tool_call_passes_through_non_task_tools(self):
        """Test that wrap_tool_call doesn't intercept non-task tools."""
        request = MagicMock()
        request.tool_call = {
            "name": "other_tool",
            "args": {"param": "value"},
        }
        request.state = {}

        handler_called = False

        def mock_handler(req):
            nonlocal handler_called
            handler_called = True
            return ToolMessage(content="result", tool_call_id="test-id")

        result = self.middleware.wrap_tool_call(request, mock_handler)

        assert handler_called
        assert isinstance(result, ToolMessage)

    @pytest.mark.asyncio
    async def test_awrap_tool_call_passes_through_non_task_tools(self):
        """Test that awrap_tool_call doesn't intercept non-task tools."""
        request = MagicMock()
        request.tool_call = {
            "name": "other_tool",
            "args": {"param": "value"},
        }
        request.state = {}

        handler_called = False

        async def mock_handler(req):
            nonlocal handler_called
            handler_called = True
            return ToolMessage(content="async result", tool_call_id="test-id")

        result = await self.middleware.awrap_tool_call(request, mock_handler)

        assert handler_called
        assert isinstance(result, ToolMessage)
