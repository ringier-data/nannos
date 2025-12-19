"""Tests for A2A Task Tracking Middleware."""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.middleware import A2ATaskTrackingMiddleware, A2ATrackingState


class TestA2ATaskTrackingMiddlewareBeforeModel:
    """Test before_model extraction of A2A metadata from ToolMessages."""

    def setup_method(self):
        """Set up test fixtures."""
        self.middleware = A2ATaskTrackingMiddleware()
        self.mock_runtime = MagicMock()

    def test_before_model_extracts_metadata_from_tool_message(self):
        """Test that before_model extracts A2A metadata from ToolMessage additional_kwargs."""
        # Create state with a ToolMessage containing A2A metadata
        tool_message = ToolMessage(
            content="Task completed successfully",
            tool_call_id="call-123",
            additional_kwargs={
                "a2a_metadata": {
                    "task_id": "task-456",
                    "context_id": "ctx-789",
                    "is_complete": False,
                    "state": "working",
                }
            },
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "task", "args": {"subagent_type": "jira-agent"}}],
        )
        state: A2ATrackingState = {"messages": [ai_message, tool_message]}

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is not None
        assert "a2a_tracking" in result
        assert "jira-agent" in result["a2a_tracking"]
        assert result["a2a_tracking"]["jira-agent"]["task_id"] == "task-456"
        assert result["a2a_tracking"]["jira-agent"]["context_id"] == "ctx-789"
        assert result["a2a_tracking"]["jira-agent"]["is_complete"] is False

    def test_before_model_clears_task_id_when_complete(self):
        """Test that before_model clears task_id when task is marked complete."""
        tool_message = ToolMessage(
            content="Task completed",
            tool_call_id="call-123",
            additional_kwargs={
                "a2a_metadata": {
                    "task_id": "task-456",
                    "context_id": "ctx-789",
                    "is_complete": True,
                    "state": "completed",
                }
            },
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "task", "args": {"subagent_type": "jira-agent"}}],
        )
        # Start with existing tracking
        state: A2ATrackingState = {
            "messages": [ai_message, tool_message],
            "a2a_tracking": {"jira-agent": {"task_id": "old-task", "context_id": "ctx-789"}},
        }

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is not None
        # task_id should be cleared, but context_id preserved
        assert "task_id" not in result["a2a_tracking"]["jira-agent"]
        assert result["a2a_tracking"]["jira-agent"]["context_id"] == "ctx-789"

    def test_before_model_clears_task_id_when_failed(self):
        """Test that before_model clears task_id when task state indicates failure."""
        tool_message = ToolMessage(
            content="Task failed",
            tool_call_id="call-123",
            additional_kwargs={
                "a2a_metadata": {
                    "task_id": "task-456",
                    "context_id": "ctx-789",
                    "is_complete": False,
                    "state": "TaskState.failed",
                }
            },
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "task", "args": {"subagent_type": "jira-agent"}}],
        )
        state: A2ATrackingState = {
            "messages": [ai_message, tool_message],
            "a2a_tracking": {"jira-agent": {"task_id": "old-task", "context_id": "ctx-789"}},
        }

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is not None
        assert "task_id" not in result["a2a_tracking"]["jira-agent"]
        assert result["a2a_tracking"]["jira-agent"]["context_id"] == "ctx-789"

    def test_before_model_returns_none_for_non_tool_message(self):
        """Test that before_model returns None when last message is not a ToolMessage."""
        ai_message = AIMessage(content="Hello")
        state: A2ATrackingState = {"messages": [ai_message]}

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is None

    def test_before_model_returns_none_for_empty_messages(self):
        """Test that before_model returns None when messages is empty."""
        state: A2ATrackingState = {"messages": []}

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is None

    def test_before_model_returns_none_without_a2a_metadata(self):
        """Test that before_model returns None when ToolMessage has no a2a_metadata."""
        tool_message = ToolMessage(
            content="Some result",
            tool_call_id="call-123",
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "task", "args": {"subagent_type": "jira-agent"}}],
        )
        state: A2ATrackingState = {"messages": [ai_message, tool_message]}

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is None

    def test_before_model_handles_task_does_not_exist_error(self):
        """Test that before_model clears stale task_id on 'task does not exist' error."""
        tool_message = ToolMessage(
            content="Error: The task 'task-old' does not exist",
            tool_call_id="call-123",
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "task", "args": {"subagent_type": "jira-agent"}}],
        )
        state: A2ATrackingState = {
            "messages": [ai_message, tool_message],
            "a2a_tracking": {"jira-agent": {"task_id": "task-old", "context_id": "ctx-789"}},
        }

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is not None
        assert "task_id" not in result["a2a_tracking"]["jira-agent"]
        assert result["a2a_tracking"]["jira-agent"]["is_complete"] is True

    def test_before_model_preserves_additional_metadata_fields(self):
        """Test that before_model preserves requires_auth, requires_input, artifacts."""
        tool_message = ToolMessage(
            content="Need authentication",
            tool_call_id="call-123",
            additional_kwargs={
                "a2a_metadata": {
                    "task_id": "task-456",
                    "context_id": "ctx-789",
                    "is_complete": False,
                    "requires_auth": True,
                    "requires_input": False,
                    "artifacts": [{"type": "file", "uri": "s3://bucket/file"}],
                }
            },
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "task", "args": {"subagent_type": "jira-agent"}}],
        )
        state: A2ATrackingState = {"messages": [ai_message, tool_message]}

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is not None
        tracking = result["a2a_tracking"]["jira-agent"]
        assert tracking["requires_auth"] is True
        assert tracking["requires_input"] is False
        assert tracking["artifacts"] == [{"type": "file", "uri": "s3://bucket/file"}]

    def test_before_model_ignores_non_task_tools(self):
        """Test that before_model returns None for non-task tool calls."""
        tool_message = ToolMessage(
            content="Some result",
            tool_call_id="call-123",
            additional_kwargs={"a2a_metadata": {"task_id": "123"}},
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "write_todos", "args": {}}],
        )
        state: A2ATrackingState = {"messages": [ai_message, tool_message]}

        result = self.middleware.before_model(state, self.mock_runtime)

        assert result is None

    @pytest.mark.asyncio
    async def test_abefore_model_delegates_to_sync(self):
        """Test that abefore_model delegates to the sync before_model."""
        tool_message = ToolMessage(
            content="Task done",
            tool_call_id="call-123",
            additional_kwargs={
                "a2a_metadata": {
                    "task_id": "task-456",
                    "context_id": "ctx-789",
                    "is_complete": False,
                }
            },
        )
        ai_message = AIMessage(
            content="",
            tool_calls=[{"id": "call-123", "name": "task", "args": {"subagent_type": "jira-agent"}}],
        )
        state: A2ATrackingState = {"messages": [ai_message, tool_message]}

        result = await self.middleware.abefore_model(state, self.mock_runtime)

        assert result is not None
        assert result["a2a_tracking"]["jira-agent"]["task_id"] == "task-456"


class TestA2ATrackingState:
    """Test A2ATrackingState TypedDict."""

    def test_state_accepts_a2a_tracking(self):
        """Test that A2ATrackingState accepts a2a_tracking field."""
        state: A2ATrackingState = {
            "messages": [],
            "a2a_tracking": {
                "jira-agent": {
                    "task_id": "123",
                    "context_id": "456",
                    "is_complete": False,
                }
            },
        }
        assert state["a2a_tracking"]["jira-agent"]["task_id"] == "123"

    def test_state_works_without_a2a_tracking(self):
        """Test that A2ATrackingState works without a2a_tracking (NotRequired)."""
        state: A2ATrackingState = {"messages": []}
        assert "a2a_tracking" not in state
