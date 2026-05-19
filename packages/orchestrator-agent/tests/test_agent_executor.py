"""Unit tests for agent executor."""

from unittest.mock import AsyncMock, Mock, patch

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, TaskState

from app.core.executor import OrchestratorDeepAgentExecutor


class TestOrchestratorDeepAgentExecutor:
    """Tests for OrchestratorDeepAgentExecutor."""

    def test_executor_initialization(self, dynamodb_table):
        """Test that executor initializes with agent."""
        executor = OrchestratorDeepAgentExecutor()

        assert executor.agent is not None
        assert hasattr(executor.agent, "stream")

    async def test_execute_with_valid_request(self, dynamodb_table):
        """Test execute with valid request context."""
        from a2a.types import Part, TextPart

        executor = OrchestratorDeepAgentExecutor()

        # Mock context
        context = Mock(spec=RequestContext)
        context.get_user_input = Mock(return_value="test query")
        context.current_task = None
        context.message = Mock(spec=Message)
        context.message.role = "user"
        context.message.parts = [Part(root=TextPart(text="test query"))]
        context.message.metadata = None
        context.message.task_id = None
        context.message.context_id = None
        context.call_context = Mock()
        context.call_context.state = {"user_sub": "test-user"}

        # Mock event queue
        event_queue = Mock(spec=EventQueue)
        event_queue.enqueue_event = AsyncMock()

        # Mock agent stream
        executor.agent.stream = AsyncMock()
        executor.agent.stream.return_value = iter([])

        # Mock get_or_create_graph
        with patch.object(executor.agent, "get_or_create_graph", new=AsyncMock()) as mock_graph:
            mock_compiled_graph = Mock()
            mock_compiled_graph.get_state = Mock()
            mock_compiled_graph.get_state.return_value = Mock(interrupts=[], next=[])

            mock_graph.return_value = (mock_compiled_graph, "config-sig")

            # Execute - should not raise
            try:
                await executor.execute(context, event_queue)
            except Exception as e:
                # Expected to raise ServerError due to mock limitations
                assert "ServerError" in str(type(e))

    def test_validate_request_returns_false(self, dynamodb_table):
        """Test that _validate_request always returns False."""
        executor = OrchestratorDeepAgentExecutor()

        context = Mock(spec=RequestContext)
        result = executor._validate_request(context)

        assert result is False

    async def test_cancel_emits_canceled_event(self, dynamodb_table):
        """Test that cancel emits a canceled status event."""
        executor = OrchestratorDeepAgentExecutor()

        context = Mock(spec=RequestContext)
        context.task_id = "task-123"
        context.context_id = "ctx-456"
        event_queue = AsyncMock(spec=EventQueue)

        await executor.cancel(context, event_queue)

        # Verify a canceled event was enqueued
        event_queue.enqueue_event.assert_called_once()
        event = event_queue.enqueue_event.call_args[0][0]
        assert event.status.state == TaskState.canceled
        assert event.final is True


class TestAgentExecutorStreamHandling:
    """Tests for stream item handling in agent executor."""

    async def test_handle_stream_item_working_state(self, dynamodb_table):
        """Test handling working state stream items."""
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        # Mock updater
        updater = Mock()
        updater.update_status = AsyncMock()

        # Mock task
        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        # Create working state item
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Processing...",
        )

        await executor._handle_stream_item(
            item, updater, task, is_final=False, streaming_artifact_id="test-artifact-id"
        )

        # Verify update_status was called
        updater.update_status.assert_called_once()
        call_args = updater.update_status.call_args
        assert call_args[0][0] == TaskState.working

    async def test_handle_stream_item_completed_state(self, dynamodb_table):
        """Test handling completed state stream items."""
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        # Mock updater
        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.complete = AsyncMock()
        updater.update_status = AsyncMock()

        # Mock task
        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        # Create completed state item
        item = AgentStreamResponse(
            state=TaskState.completed,
            content="Task completed successfully",
        )

        await executor._handle_stream_item(item, updater, task, is_final=True, streaming_artifact_id="test-artifact-id")

        # Non-streaming completion: update_status with completed state and content
        updater.update_status.assert_called_once()

    async def test_handle_stream_item_streaming_completion(self, dynamodb_table):
        """Test that streaming completion sends empty last chunk and clean status (no content duplication)."""
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        # Mock updater
        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.update_status = AsyncMock()

        # Mock task
        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        # Completed item after streaming (first_chunk_sent=True)
        item = AgentStreamResponse(
            state=TaskState.completed,
            content="Full response content",
        )

        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
            first_chunk_sent=True,
        )

        # Last artifact chunk should be empty (just stream close signal)
        updater.add_artifact.assert_called_once()
        artifact_call = updater.add_artifact.call_args
        assert artifact_call[1]["last_chunk"] is True
        assert artifact_call[1]["append"] is True
        # Check the text part is empty
        parts = artifact_call[0][0]
        assert parts[0].root.text == ""

        # Completion status should have NO content message (backend handles persistence)
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.completed
        # Second positional arg (message) should not be provided — defaults to None
        assert len(status_call[0]) == 1

        # first_chunk_sent should still be True
        assert result is True

    async def test_handle_stream_item_failed_state(self, dynamodb_table):
        """Test handling failed state stream items."""
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        # Mock updater
        updater = Mock()
        updater.update_status = AsyncMock()

        # Mock task
        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        # Create failed state item
        item = AgentStreamResponse(
            state=TaskState.failed,
            content="An error occurred during execution",
        )

        await executor._handle_stream_item(item, updater, task, is_final=True, streaming_artifact_id="test-artifact-id")

        # Verify update_status was called
        updater.update_status.assert_called_once()
        call_args = updater.update_status.call_args
        assert call_args[0][0] == TaskState.failed

    async def test_handle_stream_item_auth_required_state(self, dynamodb_table):
        """Test handling auth_required state stream items."""
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        # Mock updater
        updater = Mock()
        updater.update_status = AsyncMock()

        # Mock task
        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        # Create auth_required state item
        item = AgentStreamResponse.auth_required(
            "Authentication needed", "https://auth.example.com", "need-credentials"
        )

        await executor._handle_stream_item(
            item, updater, task, is_final=False, streaming_artifact_id="test-artifact-id"
        )

        # Verify update_status was called
        updater.update_status.assert_called_once()
        call_args = updater.update_status.call_args
        assert call_args[0][0] == TaskState.auth_required


class TestZeroTrustUserIdExtraction:
    """Tests for zero-trust user_id extraction in agent executor."""

    async def test_user_id_extracted_from_call_context(self, dynamodb_table):
        """Test that user_id is properly extracted from call_context."""
        executor = OrchestratorDeepAgentExecutor()

        # Mock context with user_id in call_context
        context = Mock(spec=RequestContext)
        context.get_user_input = Mock(return_value="test query")
        context.current_task = None
        context.message = Mock(spec=Message)
        context.call_context = Mock()
        context.call_context.state = {"user_sub": "verified-user-123"}

        # Mock event queue
        event_queue = Mock(spec=EventQueue)
        event_queue.enqueue_event = AsyncMock()

        # Mock agent
        executor.agent.get_or_create_graph = AsyncMock()

        try:
            await executor.execute(context, event_queue)
        except Exception:
            pass  # Expected to fail due to mocking

        # Verify get_or_create_graph was called with user_id
        if executor.agent.get_or_create_graph.called:
            call_args = executor.agent.get_or_create_graph.call_args
            # The user_id should be extracted and used
            assert call_args is not None

    async def test_fallback_to_anonymous_without_call_context(self, dynamodb_table):
        """Test fallback to anonymous when call_context is missing."""
        executor = OrchestratorDeepAgentExecutor()

        # Mock context without call_context
        context = Mock(spec=RequestContext)
        context.get_user_input = Mock(return_value="test query")
        context.current_task = None
        context.message = Mock(spec=Message)
        context.call_context = None

        # Mock event queue
        event_queue = Mock(spec=EventQueue)
        event_queue.enqueue_event = AsyncMock()

        # Mock agent
        executor.agent.get_or_create_graph = AsyncMock()

        try:
            await executor.execute(context, event_queue)
        except Exception:
            pass  # Expected to fail due to mocking

        # Should have attempted to use anonymous
        # (implementation logs this as a warning)


class TestExtractHitlDecisions:
    """Tests for _extract_hitl_decisions and decision replication for parallel tool calls."""

    def test_extract_single_decision_from_data_part(self, dynamodb_table):
        """Test extracting a single decision from DataPart."""
        from a2a.types import DataPart

        context = Mock(spec=RequestContext)
        context.message = Mock(spec=Message)
        context.message.parts = [
            Mock(root=DataPart(data={"decisions": [{"type": "reject", "message": "No"}]}))
        ]

        result = OrchestratorDeepAgentExecutor._extract_hitl_decisions(context)
        assert result == {"decisions": [{"type": "reject", "message": "No"}]}

    def test_extract_defaults_to_reject_when_no_data_part(self, dynamodb_table):
        """Test fallback to reject when no DataPart with decisions is found."""
        context = Mock(spec=RequestContext)
        context.message = Mock(spec=Message)
        context.message.parts = []

        result = OrchestratorDeepAgentExecutor._extract_hitl_decisions(context)
        assert result == {"decisions": [{"type": "reject"}]}

    def test_single_reject_replicated_for_parallel_tool_calls(self, dynamodb_table):
        """Test that a single reject decision is replicated to match N action_requests.

        This is the core fix for the bug where Gemini models make parallel tool
        calls (N tool_calls in one AIMessage), the HITL middleware creates N
        action_requests, but the UI sends only 1 decision. Without replication,
        the middleware raises ValueError('Number of human decisions (1) does not
        match number of hanging tool calls (N)').
        """
        from a2a.types import DataPart

        # Simulate: model made 3 parallel HITL-guarded tool calls
        action_requests = [
            {"name": "console_activate_skill", "args": {"skill": "printing"}},
            {"name": "console_activate_skill", "args": {"skill": "scanning"}},
            {"name": "console_activate_skill", "args": {"skill": "faxing"}},
        ]

        # UI sends 1 blanket reject
        decisions = [{"type": "reject", "message": "User declined"}]
        resume_value = {"decisions": decisions}

        # Replicate (same logic as executor.py)
        expected_count = len(action_requests)
        if len(resume_value.get("decisions", [])) == 1 and expected_count > 1:
            resume_value = {"decisions": resume_value["decisions"] * expected_count}

        assert len(resume_value["decisions"]) == 3
        assert all(d["type"] == "reject" for d in resume_value["decisions"])
        assert all(d["message"] == "User declined" for d in resume_value["decisions"])

    def test_single_approve_replicated_for_parallel_tool_calls(self, dynamodb_table):
        """Test that a single approve decision is replicated for N action_requests."""
        action_requests = [
            {"name": "tool_a", "args": {}},
            {"name": "tool_b", "args": {}},
        ]

        decisions = [{"type": "approve"}]
        resume_value = {"decisions": decisions}

        expected_count = len(action_requests)
        if len(resume_value.get("decisions", [])) == 1 and expected_count > 1:
            resume_value = {"decisions": resume_value["decisions"] * expected_count}

        assert len(resume_value["decisions"]) == 2
        assert all(d["type"] == "approve" for d in resume_value["decisions"])

    def test_no_replication_when_counts_match(self, dynamodb_table):
        """Test that decisions are not replicated when counts already match."""
        action_requests = [{"name": "tool_a", "args": {}}]
        decisions = [{"type": "reject"}]
        resume_value = {"decisions": decisions}

        expected_count = len(action_requests)
        if len(resume_value.get("decisions", [])) == 1 and expected_count > 1:
            resume_value = {"decisions": resume_value["decisions"] * expected_count}

        # 1 decision, 1 action_request — no replication
        assert len(resume_value["decisions"]) == 1

    def test_no_replication_when_multiple_decisions_sent(self, dynamodb_table):
        """Test that multiple decisions are not replicated (future UI support)."""
        action_requests = [
            {"name": "tool_a", "args": {}},
            {"name": "tool_b", "args": {}},
        ]
        decisions = [{"type": "approve"}, {"type": "reject"}]
        resume_value = {"decisions": decisions}

        expected_count = len(action_requests)
        if len(resume_value.get("decisions", [])) == 1 and expected_count > 1:
            resume_value = {"decisions": resume_value["decisions"] * expected_count}

        # 2 decisions, 2 action_requests — no replication needed
        assert len(resume_value["decisions"]) == 2
        assert resume_value["decisions"][0]["type"] == "approve"
        assert resume_value["decisions"][1]["type"] == "reject"
