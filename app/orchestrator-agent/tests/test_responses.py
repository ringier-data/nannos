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

    def test_working_factory(self):
        """Test working factory method."""
        response = AgentStreamResponse.working("Processing step 1", step=1, total=5)

        assert response.state == TaskState.working
        assert response.content == "Processing step 1"
        assert response.metadata == {"step": 1, "total": 5}

    def test_working_factory_no_metadata(self):
        """Test working factory method without metadata."""
        response = AgentStreamResponse.working("Processing")

        assert response.state == TaskState.working
        assert response.content == "Processing"
        assert response.metadata is None

    def test_completed_factory(self):
        """Test completed factory method."""
        response = AgentStreamResponse.completed("Task successful", result_count=42)

        assert response.state == TaskState.completed
        assert response.content == "Task successful"
        assert response.metadata == {"result_count": 42}

    def test_failed_factory(self):
        """Test failed factory method."""
        response = AgentStreamResponse.failed("Operation failed", error_code="ERR_500")

        assert response.state == TaskState.failed
        assert response.content == "Operation failed"
        assert response.metadata == {"error_code": "ERR_500"}

    def test_input_required_factory(self):
        """Test input_required factory method."""
        response = AgentStreamResponse.input_required(
            "Please provide your name", pending_nodes=["user_input", "validation"], field="name"
        )

        assert response.state == TaskState.input_required
        assert response.content == "Please provide your name"
        assert response.interrupt_reason == "graph_interrupted"
        assert response.pending_nodes == ["user_input", "validation"]
        assert response.metadata == {"field": "name"}

    def test_enum_values_preserved(self):
        """Test that enum values are preserved (not converted to strings)."""
        response = AgentStreamResponse(state=TaskState.working, content="Test")

        # Should be actual enum, not string
        assert isinstance(response.state, TaskState)
        assert response.state == TaskState.working


class TestAgentStreamResponseFactoryMethods:
    """Test suite for AgentStreamResponse factory methods."""

    def test_factory_methods_return_correct_type(self):
        """Test that all factory methods return AgentStreamResponse."""
        assert isinstance(AgentStreamResponse.auth_required("test", "url", "code"), AgentStreamResponse)
        assert isinstance(AgentStreamResponse.working("test"), AgentStreamResponse)
        assert isinstance(AgentStreamResponse.completed("test"), AgentStreamResponse)
        assert isinstance(AgentStreamResponse.failed("test"), AgentStreamResponse)
        assert isinstance(AgentStreamResponse.input_required("test"), AgentStreamResponse)

    def test_metadata_merging(self):
        """Test that additional kwargs are properly merged into metadata."""
        response = AgentStreamResponse.working("test", custom_field="value", another_field=123)

        assert response.metadata["custom_field"] == "value"
        assert response.metadata["another_field"] == 123
