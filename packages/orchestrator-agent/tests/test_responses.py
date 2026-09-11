"""Unit tests for AgentStreamResponse model."""

from a2a.types import TaskState

from app.models import AgentStreamResponse


class TestAgentStreamResponse:
    """Tests for AgentStreamResponse model."""

    def test_basic_creation(self):
        """Test creating a basic response."""
        response = AgentStreamResponse(state=TaskState.TASK_STATE_WORKING, content="Processing request")

        assert response.state == TaskState.TASK_STATE_WORKING
        assert response.content == "Processing request"
        assert response.interrupt_reason is None
        assert response.pending_nodes is None
        assert response.metadata is None

    def test_with_interrupt_reason(self):
        """Test response with interrupt reason."""
        response = AgentStreamResponse(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            content="Please provide input",
            interrupt_reason="graph_interrupted",
            pending_nodes=["node1", "node2"],
        )

        assert response.interrupt_reason == "graph_interrupted"
        assert response.pending_nodes == ["node1", "node2"]

    def test_with_metadata(self):
        """Test response with metadata."""
        response = AgentStreamResponse(
            state=TaskState.TASK_STATE_COMPLETED, content="Task complete", metadata={"task_id": "123", "duration": 5.2}
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

        assert response.state == TaskState.TASK_STATE_AUTH_REQUIRED
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

        assert response.state == TaskState.TASK_STATE_AUTH_REQUIRED
        assert "Authentication needed" in response.content
        assert "complete the required authentication" in response.content
        assert "visit the following URL" not in response.content

    def test_enum_values_preserved(self):
        """Test that enum values are preserved (not converted to strings)."""
        response = AgentStreamResponse(state=TaskState.TASK_STATE_WORKING, content="Test")

        # Should be actual enum, not string
        assert isinstance(response.state, int)  # A2A v1.0+ TaskState is a protobuf int enum
        assert response.state == TaskState.TASK_STATE_WORKING


class _Intr:
    """Stand-in for ``langgraph.types.Interrupt`` — only ``.value`` is read."""

    def __init__(self, value):
        self.value = value


def _approval(call_id: str, tool: str):
    return {
        "action_requests": [{"name": tool, "args": {"_call_id": call_id}, "description": f"{tool} is risky"}],
        "review_configs": [{"action_name": tool, "allowed_decisions": ["approve", "reject"]}],
    }


class TestInterruptValueSelection:
    """``AgentStreamResponse.interrupt_value`` — see #217.

    A step with two ``eval`` calls now raises one approval each. Rendering only the
    last is a HITL bypass, because the resume path replicates a blanket approve
    across every pending interrupt.
    """

    def test_single_interrupt_is_passed_through(self):
        value = _approval("c1", "delete_file")

        assert AgentStreamResponse.interrupt_value([_Intr(value)]) == value

    def test_no_interrupts_yields_empty(self):
        assert AgentStreamResponse.interrupt_value([]) == {}
        assert AgentStreamResponse.interrupt_value(None) == {}

    def test_two_approvals_are_merged_so_the_user_sees_both(self):
        merged = AgentStreamResponse.interrupt_value([_Intr(_approval("c1", "delete_file")), _Intr(_approval("c2", "send_email"))])

        names = [ar["name"] for ar in merged["action_requests"]]
        call_ids = [ar["args"]["_call_id"] for ar in merged["action_requests"]]
        assert names == ["delete_file", "send_email"]
        # Each request keeps its own id, so decisions still route back to the
        # interrupt that raised it (executor._build_interrupt_resume_map).
        assert call_ids == ["c1", "c2"]
        assert [rc["action_name"] for rc in merged["review_configs"]] == ["delete_file", "send_email"]

    def test_mixed_auth_and_approval_is_not_merged(self):
        """An auth pause needs its own round trip and cannot join an approval card."""
        auth = {"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "auth_url": "https://example/authorize"}
        approval = _approval("c1", "delete_file")

        picked = AgentStreamResponse.interrupt_value([_Intr(approval), _Intr(auth)])

        assert picked == auth

    def test_merged_payload_renders_as_one_approval_card(self):
        merged = AgentStreamResponse.interrupt_value([_Intr(_approval("c1", "delete_file")), _Intr(_approval("c2", "send_email"))])

        response = AgentStreamResponse.from_interrupt(merged)

        assert response.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert [ar["name"] for ar in response.action_requests] == ["delete_file", "send_email"]
        assert len(response.review_configs) == 2
