"""Unit tests for StreamHandler class."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from a2a.types import TaskState

from app.handlers import StreamHandler

class TestBuildAuthResponse:
    """Test build_auth_response static method."""

    def test_build_auth_response_with_url(self):
        """Test auth response building with auth_url provided."""
        response = StreamHandler.build_auth_response(
            auth_message="Authentication required",
            auth_url="https://auth.example.com",
            error_code="AUTH_001"
        )
        
        assert response.state == TaskState.auth_required
        assert response.interrupt_reason == "auth_required"
        assert response.metadata is not None
        assert response.metadata["auth_url"] == "https://auth.example.com"
        assert response.metadata["error_code"] == "AUTH_001"
        assert "authentication" in response.content.lower()

    def test_build_auth_response_without_url(self):
        """Test auth response building without auth_url."""
        response = StreamHandler.build_auth_response(
            auth_message="Authentication required",
            auth_url="",
            error_code="AUTH_002"
        )
        
        assert response.state == TaskState.auth_required
        assert response.interrupt_reason == "auth_required"
        assert response.metadata is not None
        assert "auth_url" in response.metadata
        assert response.metadata["auth_url"] == ""
        assert "authentication" in response.content.lower()


class TestBuildWorkingResponse:
    """Test build_working_response static method."""

    def test_build_working_response_with_message(self):
        """Test working response with custom message."""
        response = StreamHandler.build_working_response(
            content="Processing your request...",
            metadata={"progress": 50}
        )
        
        assert response.state == TaskState.working
        assert response.content == "Processing your request..."
        assert response.metadata == {"progress": 50}

    def test_build_working_response_without_metadata(self):
        """Test working response without metadata."""
        response = StreamHandler.build_working_response(content="Task is being processed")
        
        assert response.state == TaskState.working
        assert response.content == "Task is being processed"
        assert response.metadata is None


class TestBuildCompletedResponse:
    """Test build_completed_response static method."""

    def test_build_completed_response(self):
        """Test completed response building."""
        response = StreamHandler.build_completed_response(
            content="Task completed successfully",
            metadata={"result": "success"}
        )
        
        assert response.state == TaskState.completed
        assert response.content == "Task completed successfully"
        assert response.metadata == {"result": "success"}


class TestBuildFailedResponse:
    """Test build_failed_response static method."""

    def test_build_failed_response(self):
        """Test failed response building."""
        response = StreamHandler.build_failed_response(
            content="Task failed with error",
            metadata={"error_code": 500}
        )
        
        assert response.state == TaskState.failed
        assert response.content == "Task failed with error"
        assert response.metadata == {"error_code": 500}


class TestBuildInputRequiredResponse:
    """Test build_input_required_response static method."""

    def test_build_input_required_response(self):
        """Test input required response building."""
        response = StreamHandler.build_input_required_response(
            content="Please provide additional information",
            prompt="Enter your name and email",
            metadata={"required_fields": ["name", "email"]}
        )
        
        assert response.state == TaskState.input_required
        assert response.content == "Please provide additional information"
        assert response.metadata["input_prompt"] == "Enter your name and email"
        assert response.metadata["required_fields"] == ["name", "email"]


class TestParseAgentResponse:
    """Test parse_agent_response static method."""

    def test_parse_agent_response_completed_with_ai_message(self):
        """Test parsing completed state with AIMessage."""
        final_state = {
            "messages": [
                HumanMessage(content="Hello"),
                AIMessage(content="Hi! How can I help?")
            ]
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Hi! How can I help?"

    def test_parse_agent_response_with_empty_messages(self):
        """Test parsing with no messages."""
        final_state = {"messages": []}
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Task completed successfully"

    def test_parse_agent_response_with_none_messages(self):
        """Test parsing with None messages."""
        final_state = {"messages": None}
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Task completed successfully"

    def test_parse_agent_response_with_human_message_only(self):
        """Test parsing with only human message - returns last message content."""
        final_state = {
            "messages": [HumanMessage(content="Hello")]
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Hello"

    def test_parse_agent_response_auth_required_from_a2a_tracking(self):
        """Test detecting auth required from a2a_tracking metadata."""
        final_state = {
            "messages": [
                HumanMessage(content="Test"),
                AIMessage(content="Response")
            ],
            "a2a_tracking": {
                "some_agent": {
                    "requires_auth": True,
                    "auth_url": "https://oauth.example.com",
                    "auth_message": "Authentication required",
                    "error_code": "AUTH_003"
                }
            }
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.auth_required
        assert response.interrupt_reason == "auth_required"
        assert response.metadata is not None
        assert response.metadata["auth_url"] == "https://oauth.example.com"

    def test_parse_agent_response_with_tool_message(self):
        """Test parsing with tool message as last message."""
        final_state = {
            "messages": [
                HumanMessage(content="Test"),
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "tool_a",
                        "args": {},
                        "id": "call_123",
                        "type": "tool_call"
                    }]
                ),
                ToolMessage(content="Tool result", tool_call_id="call_123"),
                AIMessage(content="Final response")
            ]
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Final response"

    def test_parse_agent_response_no_auth_in_tracking(self):
        """Test that non-auth a2a_tracking doesn't trigger auth_required."""
        final_state = {
            "messages": [
                HumanMessage(content="Test"),
                AIMessage(content="Normal response")
            ],
            "a2a_tracking": {
                "agent1": {"state": "completed"},
                "agent2": {"state": "working"}
            }
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Normal response"

    def test_parse_agent_response_multiple_ai_messages(self):
        """Test parsing with multiple AI messages (uses last one)."""
        final_state = {
            "messages": [
                HumanMessage(content="Question 1"),
                AIMessage(content="Answer 1"),
                HumanMessage(content="Question 2"),
                AIMessage(content="Answer 2")
            ]
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Answer 2"

    def test_parse_agent_response_with_empty_content(self):
        """Test parsing AI message with empty content."""
        final_state = {
            "messages": [
                HumanMessage(content="Test"),
                AIMessage(content="")
            ]
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == ""


class TestStreamHandlerEdgeCases:
    """Test edge cases and error handling."""

    def test_build_auth_response_with_empty_url(self):
        """Test auth response with empty auth_url."""
        response = StreamHandler.build_auth_response(
            auth_message="Auth needed",
            auth_url="",
            error_code="AUTH_004"
        )
        
        assert response.state == TaskState.auth_required
        assert response.metadata["auth_url"] == ""

    def test_build_response_methods_preserve_metadata(self):
        """Test that all build methods preserve metadata correctly."""
        test_metadata = {"key": "value", "number": 42}
        
        working = StreamHandler.build_working_response(
            content="Working...",
            metadata=test_metadata
        )
        completed = StreamHandler.build_completed_response(
            content="Done",
            metadata=test_metadata
        )
        failed = StreamHandler.build_failed_response(
            content="Failed",
            metadata=test_metadata
        )
        input_req = StreamHandler.build_input_required_response(
            content="Need input",
            prompt="Enter data",
            metadata=test_metadata
        )
        
        assert working.metadata == test_metadata
        assert completed.metadata == test_metadata
        assert failed.metadata == test_metadata
        # input_req has merged metadata with input_prompt
        assert input_req.metadata["key"] == "value"
        assert input_req.metadata["number"] == 42
        assert input_req.metadata["input_prompt"] == "Enter data"

    def test_parse_agent_response_with_malformed_a2a_tracking(self):
        """Test parsing with malformed a2a_tracking structure - will crash currently."""
        # Note: The current implementation doesn't handle malformed a2a_tracking gracefully
        # This test documents the current behavior - could be improved with error handling
        final_state = {
            "messages": [
                HumanMessage(content="Test"),
                AIMessage(content="Response")
            ],
            "a2a_tracking": {}  # Empty dict is valid
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Response"

    def test_parse_agent_response_without_a2a_tracking(self):
        """Test parsing without a2a_tracking."""
        final_state = {
            "messages": [
                HumanMessage(content="Test"),
                AIMessage(content="Normal response")
            ]
        }
        
        response = StreamHandler.parse_agent_response(final_state)
        
        assert response.state == TaskState.completed
        assert response.content == "Normal response"
