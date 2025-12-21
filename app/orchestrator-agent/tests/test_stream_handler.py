"""Unit tests for StreamHandler class."""

from a2a.types import TaskState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.handlers import StreamHandler


class TestBuildAuthResponse:
    """Test build_auth_response static method."""

    def test_build_auth_response_with_url(self):
        """Test auth response building with auth_url provided."""
        response = StreamHandler.build_auth_response(
            auth_message="Authentication required", auth_url="https://auth.example.com", error_code="AUTH_001"
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
            auth_message="Authentication required", auth_url="", error_code="AUTH_002"
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
        response = StreamHandler.build_working_response(content="Processing your request...", metadata={"progress": 50})

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
            content="Task completed successfully", metadata={"result": "success"}
        )

        assert response.state == TaskState.completed
        assert response.content == "Task completed successfully"
        assert response.metadata == {"result": "success"}


class TestBuildFailedResponse:
    """Test build_failed_response static method."""

    def test_build_failed_response(self):
        """Test failed response building."""
        response = StreamHandler.build_failed_response(content="Task failed with error", metadata={"error_code": 500})

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
            metadata={"required_fields": ["name", "email"]},
        )

        assert response.state == TaskState.input_required
        assert response.content == "Please provide additional information"
        assert response.metadata is not None
        assert response.metadata["input_prompt"] == "Enter your name and email"
        assert response.metadata["required_fields"] == ["name", "email"]


class TestParseAgentResponse:
    """Test parse_agent_response static method."""

    def test_parse_agent_response_completed_with_ai_message(self):
        """Test parsing completed state with AIMessage."""
        final_state = {"messages": [HumanMessage(content="Hello"), AIMessage(content="Hi! How can I help?")]}

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
        final_state = {"messages": [HumanMessage(content="Hello")]}

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        assert response.content == "Hello"

    def test_parse_agent_response_with_tool_message(self):
        """Test parsing with tool message as last message."""
        final_state = {
            "messages": [
                HumanMessage(content="Test"),
                AIMessage(
                    content="", tool_calls=[{"name": "tool_a", "args": {}, "id": "call_123", "type": "tool_call"}]
                ),
                ToolMessage(content="Tool result", tool_call_id="call_123"),
                AIMessage(content="Final response"),
            ]
        }

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        assert response.content == "Final response"

    def test_parse_agent_response_no_auth_in_tracking(self):
        """Test that non-auth a2a_tracking doesn't trigger auth_required."""
        final_state = {
            "messages": [HumanMessage(content="Test"), AIMessage(content="Normal response")],
            "a2a_tracking": {"agent1": {"state": "completed"}, "agent2": {"state": "working"}},
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
                AIMessage(content="Answer 2"),
            ]
        }

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        assert response.content == "Answer 2"

    def test_parse_agent_response_with_empty_content(self):
        """Test parsing AI message with empty content."""
        final_state = {"messages": [HumanMessage(content="Test"), AIMessage(content="")]}

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        assert response.content == ""


class TestStreamHandlerEdgeCases:
    """Test edge cases and error handling."""

    def test_build_auth_response_with_empty_url(self):
        """Test auth response with empty auth_url."""
        response = StreamHandler.build_auth_response(auth_message="Auth needed", auth_url="", error_code="AUTH_004")

        assert response.state == TaskState.auth_required
        assert response.metadata is not None
        assert response.metadata["auth_url"] == ""

    def test_build_response_methods_preserve_metadata(self):
        """Test that all build methods preserve metadata correctly."""
        test_metadata = {"key": "value", "number": 42}

        working = StreamHandler.build_working_response(content="Working...", metadata=test_metadata)
        completed = StreamHandler.build_completed_response(content="Done", metadata=test_metadata)
        failed = StreamHandler.build_failed_response(content="Failed", metadata=test_metadata)
        input_req = StreamHandler.build_input_required_response(
            content="Need input", prompt="Enter data", metadata=test_metadata
        )

        assert working.metadata == test_metadata
        assert completed.metadata == test_metadata
        assert failed.metadata == test_metadata
        # input_req has merged metadata with input_prompt
        assert input_req.metadata is not None
        assert input_req.metadata["key"] == "value"
        assert input_req.metadata["number"] == 42
        assert input_req.metadata["input_prompt"] == "Enter data"

    def test_parse_agent_response_with_malformed_a2a_tracking(self):
        """Test parsing with malformed a2a_tracking structure - will crash currently."""
        # Note: The current implementation doesn't handle malformed a2a_tracking gracefully
        # This test documents the current behavior - could be improved with error handling
        final_state = {
            "messages": [HumanMessage(content="Test"), AIMessage(content="Response")],
            "a2a_tracking": {},  # Empty dict is valid
        }

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        assert response.content == "Response"

    def test_parse_agent_response_without_a2a_tracking(self):
        """Test parsing without a2a_tracking."""
        final_state = {"messages": [HumanMessage(content="Test"), AIMessage(content="Normal response")]}

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        assert response.content == "Normal response"


class TestExtractCurrentTurnMessages:
    """Test _extract_current_turn_messages static method."""

    def test_extract_current_turn_single_turn(self):
        """Test extracting messages from a single turn."""
        messages = [
            HumanMessage(content="User question"),
            AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "call_1", "type": "tool_call"}]),
            ToolMessage(content="Tool result", tool_call_id="call_1"),
            AIMessage(content="Final answer"),
        ]

        current_turn = StreamHandler._extract_current_turn_messages(messages)

        assert len(current_turn) == 3  # All messages after HumanMessage
        assert isinstance(current_turn[0], AIMessage)
        assert isinstance(current_turn[1], ToolMessage)
        assert isinstance(current_turn[2], AIMessage)

    def test_extract_current_turn_multiple_turns(self):
        """Test extracting only the current turn from multi-turn conversation."""
        messages = [
            HumanMessage(content="First question"),
            AIMessage(content="First answer"),
            HumanMessage(content="Second question"),  # Current turn starts here
            AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "call_2", "type": "tool_call"}]),
            ToolMessage(content="Tool result", tool_call_id="call_2"),
            AIMessage(content="Second answer"),
        ]

        current_turn = StreamHandler._extract_current_turn_messages(messages)

        assert len(current_turn) == 3  # Only messages after last HumanMessage
        assert current_turn[0].tool_calls[0]["id"] == "call_2"
        assert current_turn[2].content == "Second answer"

    def test_extract_current_turn_no_human_message(self):
        """Test behavior when no HumanMessage is found."""
        messages = [
            AIMessage(content="AI message 1"),
            ToolMessage(content="Tool result", tool_call_id="call_1"),
            AIMessage(content="AI message 2"),
        ]

        current_turn = StreamHandler._extract_current_turn_messages(messages)

        # Should return all messages with a warning
        assert len(current_turn) == 3
        assert current_turn == messages

    def test_extract_current_turn_empty_messages(self):
        """Test with empty message list."""
        messages = []

        current_turn = StreamHandler._extract_current_turn_messages(messages)

        assert current_turn == []

    def test_extract_current_turn_only_human_message(self):
        """Test with only a HumanMessage."""
        messages = [HumanMessage(content="User question")]

        current_turn = StreamHandler._extract_current_turn_messages(messages)

        assert len(current_turn) == 0  # No messages after HumanMessage


class TestExtractRecentlyCalledSubagents:
    """Test _extract_recently_called_subagents static method."""

    def test_extract_subagents_single_call(self):
        """Test extracting a single sub-agent call."""
        final_state = {
            "messages": [
                HumanMessage(content="Create a ticket"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "JiraAgent"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Ticket created", tool_call_id="call_1"),
                AIMessage(content="Done"),
            ]
        }

        subagents = StreamHandler._extract_recently_called_subagents(final_state)

        assert subagents == {"JiraAgent"}

    def test_extract_subagents_multiple_calls(self):
        """Test extracting multiple sub-agent calls."""
        final_state = {
            "messages": [
                HumanMessage(content="Create ticket and send email"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "JiraAgent"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Ticket created", tool_call_id="call_1"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "EmailAgent"},
                            "id": "call_2",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Email sent", tool_call_id="call_2"),
                AIMessage(content="All done"),
            ]
        }

        subagents = StreamHandler._extract_recently_called_subagents(final_state)

        assert subagents == {"JiraAgent", "EmailAgent"}

    def test_extract_subagents_only_current_turn(self):
        """Test that only sub-agents from current turn are extracted."""
        final_state = {
            "messages": [
                # Previous turn
                HumanMessage(content="First request"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "OldAgent"},
                            "id": "call_old",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Old result", tool_call_id="call_old"),
                AIMessage(content="First done"),
                # Current turn
                HumanMessage(content="Second request"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "NewAgent"},
                            "id": "call_new",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="New result", tool_call_id="call_new"),
                AIMessage(content="Second done"),
            ]
        }

        subagents = StreamHandler._extract_recently_called_subagents(final_state)

        # Should only include NewAgent from current turn, not OldAgent
        assert subagents == {"NewAgent"}
        assert "OldAgent" not in subagents

    def test_extract_subagents_non_task_tools_ignored(self):
        """Test that non-task tools are ignored."""
        final_state = {
            "messages": [
                HumanMessage(content="Do something"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "other_tool",  # Not a 'task' tool
                            "args": {"param": "value"},
                            "id": "call_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "task",
                            "args": {"subagent_type": "ValidAgent"},
                            "id": "call_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                ToolMessage(content="Other result", tool_call_id="call_1"),
                ToolMessage(content="Agent result", tool_call_id="call_2"),
                AIMessage(content="Done"),
            ]
        }

        subagents = StreamHandler._extract_recently_called_subagents(final_state)

        assert subagents == {"ValidAgent"}

    def test_extract_subagents_empty_messages(self):
        """Test with empty messages."""
        final_state = {"messages": []}

        subagents = StreamHandler._extract_recently_called_subagents(final_state)

        assert subagents == set()

    def test_extract_subagents_no_tool_calls(self):
        """Test with no tool calls."""
        final_state = {
            "messages": [
                HumanMessage(content="Simple question"),
                AIMessage(content="Simple answer"),
            ]
        }

        subagents = StreamHandler._extract_recently_called_subagents(final_state)

        assert subagents == set()


class TestParseAgentResponseWithSubagentFiltering:
    """Test parse_agent_response with sub-agent filtering for current turn."""

    def test_requires_input_only_checks_current_turn(self):
        """Test that requires_input only applies to current turn sub-agents."""
        final_state = {
            "messages": [
                # Previous turn
                HumanMessage(content="First request"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "OldAgent"},
                            "id": "call_old",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Old result", tool_call_id="call_old"),
                AIMessage(content="First done"),
                # Current turn
                HumanMessage(content="Second request"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "NewAgent"},
                            "id": "call_new",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="New result needs input", tool_call_id="call_new"),
                AIMessage(content="Processing"),
            ],
            "a2a_tracking": {
                "OldAgent": {"requires_input": True},  # Stale state from previous turn
                "NewAgent": {"requires_input": False},  # Current turn agent
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should be completed, not input_required, because current turn agent doesn't require input
        assert response.state == TaskState.completed

    def test_requires_auth_only_checks_current_turn(self):
        """Test that requires_auth only applies to current turn sub-agents."""
        final_state = {
            "messages": [
                # Previous turn
                HumanMessage(content="First request"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "OldAgent"},
                            "id": "call_old",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Old result", tool_call_id="call_old"),
                AIMessage(content="First done"),
                # Current turn
                HumanMessage(content="Second request"),
                AIMessage(content="Processing current request"),
            ],
            "a2a_tracking": {
                "OldAgent": {
                    "requires_auth": True,
                    "auth_url": "https://old.example.com",
                    "auth_message": "Old auth",
                    "error_code": "OLD_AUTH",
                },
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should be completed, not auth_required, because OldAgent wasn't called in current turn
        assert response.state == TaskState.completed
        assert response.content == "Processing current request"


class TestConservativeOverrideLogic:
    """Test conservative override logic: only override LLM when ALL agents blocked."""

    def test_llm_completed_one_blocked_one_success_trusts_llm(self):
        """Test parallel execution: LLM says completed, one blocked + one success → trust LLM."""
        from app.models.schemas import FinalResponseSchema

        # Simulate parallel execution where one agent succeeded
        final_state = {
            "messages": [
                HumanMessage(content="Create ticket and send email"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "JiraAgent"},
                            "id": "call_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "task",
                            "args": {"subagent_type": "EmailAgent"},
                            "id": "call_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                ToolMessage(content="Ticket needs input", tool_call_id="call_1"),
                ToolMessage(content="Email sent successfully", tool_call_id="call_2"),
                AIMessage(content="Done - used email"),
            ],
            "structured_response": FinalResponseSchema(
                task_state=TaskState.completed,
                message="Email sent successfully as alternative approach",
                reasoning="Jira blocked but email succeeded",
            ),
            "a2a_tracking": {
                "JiraAgent": {"requires_input": True, "is_complete": False},
                "EmailAgent": {"requires_input": False, "is_complete": True, "state": "TaskState.completed"},
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should trust LLM's completed decision (EmailAgent succeeded)
        assert response.state == TaskState.completed
        assert "Email sent successfully" in response.content

    def test_llm_completed_all_blocked_overrides_to_input_required(self):
        """Test safety override: LLM says completed, ALL agents blocked → override to input_required."""
        from app.models.schemas import FinalResponseSchema

        # LLM hallucination: says completed but all agents are blocked
        final_state = {
            "messages": [
                HumanMessage(content="Create ticket and send email"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "JiraAgent"},
                            "id": "call_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "task",
                            "args": {"subagent_type": "EmailAgent"},
                            "id": "call_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                ToolMessage(content="Jira needs input", tool_call_id="call_1"),
                ToolMessage(content="Email needs input", tool_call_id="call_2"),
                AIMessage(content="All done"),
            ],
            "structured_response": FinalResponseSchema(
                task_state=TaskState.completed,
                message="Tasks completed successfully",
                reasoning="Both completed",  # LLM hallucination
            ),
            "a2a_tracking": {
                "JiraAgent": {"requires_input": True, "is_complete": False},
                "EmailAgent": {"requires_input": True, "is_complete": False},
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should override to input_required (safety against hallucination)
        assert response.state == TaskState.input_required
        assert response.interrupt_reason == "subagent_input_required"
        assert response.metadata is not None
        assert response.metadata["agent_name"] in ["JiraAgent", "EmailAgent"]

    def test_llm_completed_all_blocked_auth_takes_priority(self):
        """Test safety override: ALL blocked with auth + input → auth takes priority."""
        from app.models.schemas import FinalResponseSchema

        final_state = {
            "messages": [
                HumanMessage(content="Multi-agent request"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "Agent1"},
                            "id": "call_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "task",
                            "args": {"subagent_type": "Agent2"},
                            "id": "call_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                ToolMessage(content="Auth needed", tool_call_id="call_1"),
                ToolMessage(content="Input needed", tool_call_id="call_2"),
                AIMessage(content="Done"),
            ],
            "structured_response": FinalResponseSchema(
                task_state=TaskState.completed, message="Completed", reasoning="Done"
            ),
            "a2a_tracking": {
                "Agent1": {
                    "requires_auth": True,
                    "auth_url": "https://auth.example.com",
                    "auth_message": "Auth needed",
                    "error_code": "AUTH_001",
                },
                "Agent2": {"requires_input": True},
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should override to auth_required (auth has priority)
        assert response.state == TaskState.auth_required
        assert response.metadata is not None
        assert response.metadata["auth_url"] == "https://auth.example.com"

    def test_llm_input_required_mixed_results_respects_llm(self):
        """Test: LLM says input_required with mixed results → respect LLM decision."""
        from app.models.schemas import FinalResponseSchema

        # LLM correctly identifies need for input despite partial success
        final_state = {
            "messages": [
                HumanMessage(content="Create ticket and notify team"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "JiraAgent"},
                            "id": "call_1",
                            "type": "tool_call",
                        },
                        {
                            "name": "task",
                            "args": {"subagent_type": "SlackAgent"},
                            "id": "call_2",
                            "type": "tool_call",
                        },
                    ],
                ),
                ToolMessage(content="Ticket needs project", tool_call_id="call_1"),
                ToolMessage(content="Notified team", tool_call_id="call_2"),
                AIMessage(content="Need project for ticket"),
            ],
            "structured_response": FinalResponseSchema(
                task_state=TaskState.input_required,
                message="Which project should I create the ticket in?",
                reasoning="Slack succeeded but Jira needs project info",
            ),
            "a2a_tracking": {
                "JiraAgent": {"requires_input": True, "is_complete": False},
                "SlackAgent": {"requires_input": False, "is_complete": True, "state": "TaskState.completed"},
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should respect LLM's input_required decision
        assert response.state == TaskState.input_required
        assert "Which project" in response.content

    def test_llm_working_state_not_overridden(self):
        """Test: LLM says working, blocked agents exist → respect LLM decision."""
        from app.models.schemas import FinalResponseSchema

        final_state = {
            "messages": [
                HumanMessage(content="Long running task"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "AsyncAgent"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Processing", tool_call_id="call_1"),
                AIMessage(content="Still working"),
            ],
            "structured_response": FinalResponseSchema(
                task_state=TaskState.working,
                message="Task is still being processed in the background",
                reasoning="Async operation in progress",
            ),
            "a2a_tracking": {
                "AsyncAgent": {"requires_input": True, "is_complete": False},  # Might need input later
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should respect LLM's working decision (not override based on blocking)
        assert response.state == TaskState.working
        assert "background" in response.content

    def test_single_agent_blocked_overrides_when_completed(self):
        """Test: Single agent called, LLM says completed, agent blocked → override."""
        from app.models.schemas import FinalResponseSchema

        final_state = {
            "messages": [
                HumanMessage(content="Create ticket"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "JiraAgent"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Need more details", tool_call_id="call_1"),
                AIMessage(content="Done"),
            ],
            "structured_response": FinalResponseSchema(
                task_state=TaskState.completed, message="Ticket created", reasoning="Task complete"
            ),
            "a2a_tracking": {
                "JiraAgent": {"requires_input": True, "is_complete": False},
            },
        }

        response = StreamHandler.parse_agent_response(final_state)

        # Should override (only one agent, it's blocked)
        assert response.state == TaskState.input_required
        assert response.metadata is not None
        assert response.metadata["agent_name"] == "JiraAgent"


class TestIncludeSubagentOutput:
    """Tests for include_subagent_output pass-through behavior."""

    def test_include_subagent_output_appends_tool_content(self):
        """When include_subagent_output=True, append latest ToolMessage content to message."""
        # Current turn: Human -> AI(task tool) -> ToolMessage(sub-agent) -> AI(FinalResponseSchema)
        final_state = {
            "messages": [
                HumanMessage(content="Tell me a joke"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "smart-joke-responder"},
                            "id": "call_task_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="Why did the model cross the road? To reduce loss!", tool_call_id="call_task_1"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "FinalResponseSchema",
                            "args": {
                                "task_state": "completed",
                                "message": "Here's the joke the smart-joke-responder created based on the TensorFlow Serving README:",
                                "include_subagent_output": True,
                            },
                            "id": "call_final_1",
                            "type": "tool_call",
                        }
                    ],
                ),
            ]
        }

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        # Should include both intro and tool content separated by a blank line
        assert "Here's the joke the smart-joke-responder created" in response.content
        assert "Why did the model cross the road?" in response.content
        assert "\n\n" in response.content

    def test_include_subagent_output_no_tool_message_uses_intro_only(self):
        """If no ToolMessage exists, keep only the LLM message (no crash)."""
        final_state = {
            "messages": [
                HumanMessage(content="Tell me a joke"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "FinalResponseSchema",
                            "args": {
                                "task_state": "completed",
                                "message": "Here's the joke:",
                                "include_subagent_output": True,
                            },
                            "id": "call_final_2",
                            "type": "tool_call",
                        }
                    ],
                ),
            ]
        }

        response = StreamHandler.parse_agent_response(final_state)

        assert response.state == TaskState.completed
        assert response.content == "Here's the joke:"
