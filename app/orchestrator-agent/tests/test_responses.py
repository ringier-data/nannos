"""Unit tests for AgentStreamResponse model."""

from a2a.types import TaskState

from app.models import AgentStreamResponse


class TestAgentStreamResponse:
    """Tests for AgentStreamResponse model."""

    def test_basic_creation(self):
        """Test creating a basic response."""
        response = AgentStreamResponse(state=TaskState.working, content="Processing request")

        assert response.state == TaskState.working
        assert response.content == "Processing request"
        assert response.interrupt_reason is None
        assert response.pending_nodes is None
        assert response.metadata is None

    def test_with_interrupt_reason(self):
        """Test response with interrupt reason."""
        response = AgentStreamResponse(
            state=TaskState.input_required,
            content="Please provide input",
            interrupt_reason="graph_interrupted",
            pending_nodes=["node1", "node2"],
        )

        assert response.interrupt_reason == "graph_interrupted"
        assert response.pending_nodes == ["node1", "node2"]

    def test_with_metadata(self):
        """Test response with metadata."""
        response = AgentStreamResponse(
            state=TaskState.completed, content="Task complete", metadata={"task_id": "123", "duration": 5.2}
        )

        assert response.metadata == {"task_id": "123", "duration": 5.2}

    def test_auth_required_factory_with_url(self):
        """Test auth_required factory method with URL."""
        response = AgentStreamResponse.auth_required(
            message="Authentication needed",
            auth_url="https://auth.example.com",
            error_code="need-credentials",
            tool="test_tool",
        )

        assert response.state == TaskState.auth_required
        assert "Authentication needed" in response.content
        assert "https://auth.example.com" in response.content
        assert response.interrupt_reason == "auth_required"
        assert response.metadata is not None
        assert response.metadata["auth_url"] == "https://auth.example.com"
        assert response.metadata["error_code"] == "need-credentials"
        assert response.metadata["requires_auth"] is True
        assert response.metadata["tool"] == "test_tool"

    def test_auth_required_factory_without_url(self):
        """Test auth_required factory method without URL."""
        response = AgentStreamResponse.auth_required(message="Authentication needed", error_code="need-credentials")

        assert response.state == TaskState.auth_required
        assert "Authentication needed" in response.content
        assert "complete the required authentication" in response.content
        assert "visit the following URL" not in response.content

    def test_enum_values_preserved(self):
        """Test that enum values are preserved (not converted to strings)."""
        response = AgentStreamResponse(state=TaskState.working, content="Test")

        # Should be actual enum, not string
        assert isinstance(response.state, TaskState)
        assert response.state == TaskState.working
