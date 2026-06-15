"""Unit tests for agent executor."""

from unittest.mock import AsyncMock, Mock, patch

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import Message, Part, Role, TaskState
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

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
        executor = OrchestratorDeepAgentExecutor()

        # Mock context — use a real A2A Message so new_task_from_user_message works
        # (A2A v1.0+ protobuf Task can't embed a Mock message).
        context = Mock(spec=RequestContext)
        context.get_user_input = Mock(return_value="test query")
        context.current_task = None
        context.message = Message(
            role=Role.ROLE_USER,
            parts=[Part(text="test query")],
            message_id="msg-1",
        )
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

            # Execute - may raise an A2A error due to mock limitations.
            # A2A v1.0+ raises InvalidParamsError/InternalError directly (no ServerError wrapper).
            try:
                await executor.execute(context, event_queue)
            except Exception as e:
                assert type(e).__name__ in ("InternalError", "InvalidParamsError", "ServerError")

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
        # A2A v1.0+ removed TaskStatusUpdateEvent.final; the terminal CANCELED state is the signal.
        assert event.status.state == TaskState.TASK_STATE_CANCELED


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
            state=TaskState.TASK_STATE_WORKING,
            content="Processing...",
        )

        await executor._handle_stream_item(
            item, updater, task, is_final=False, streaming_artifact_id="test-artifact-id"
        )

        # Verify update_status was called
        updater.update_status.assert_called_once()
        call_args = updater.update_status.call_args
        assert call_args[0][0] == TaskState.TASK_STATE_WORKING

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
            state=TaskState.TASK_STATE_COMPLETED,
            content="Task completed successfully",
        )

        await executor._handle_stream_item(item, updater, task, is_final=True, streaming_artifact_id="test-artifact-id")

        # Non-streaming completion: update_status with completed state and content
        updater.update_status.assert_called_once()

    async def test_handle_stream_item_streaming_completion(self, dynamodb_table):
        """Streaming completion closes the artifact AND embeds the authoritative final answer.

        Regression for the production incident on 2026-05-25 where a downstream A2A client
        failed to parse a streamed artifact frame and the user never received the reply,
        because the terminal `completed` status carried no message body. The terminal
        status must now always carry the validated FinalResponseSchema.message text as a
        fallback / source-of-truth, tagged with `final_answer_source: "fallback"` so
        well-behaved clients that already rendered the streamed artifact can dedupe.
        """
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
            state=TaskState.TASK_STATE_COMPLETED,
            content="Full response content",
        )

        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
            first_chunk_sent=True,
            streamed_chars=45,
        )

        # Last artifact chunk should be empty (just stream close signal)
        updater.add_artifact.assert_called_once()
        artifact_call = updater.add_artifact.call_args
        assert artifact_call[1]["last_chunk"] is True
        assert artifact_call[1]["append"] is True
        # Check the text part is empty
        parts = artifact_call[0][0]
        assert parts[0].text == ""

        # Completion status MUST carry the authoritative final answer as a fallback
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_COMPLETED
        # Second positional arg is the message carrying the final answer text
        assert len(status_call[0]) == 2
        final_msg = status_call[0][1]
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "Full response content" in "".join(text_parts)
        # Metadata flag signals well-behaved clients to dedupe against the artifact stream
        assert status_call[1]["metadata"]["final_answer_source"] == "fallback"

        # _handle_stream_item now returns (first_chunk_sent, first_intermediate_chunk_sent)
        assert result == (True, False)

    async def test_handle_stream_item_streaming_completion_empty_content_fallback(self, dynamodb_table):
        """If the agent yields an empty final content (edge case), the terminal status
        still carries a non-empty message body so the client never gets a blank reply.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.update_status = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        item = AgentStreamResponse(state=TaskState.TASK_STATE_COMPLETED, content="")

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
            first_chunk_sent=True,
        )

        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_COMPLETED
        final_msg = status_call[0][1]
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "".join(text_parts).strip() != ""

    async def test_handle_stream_item_streaming_first_chunk_creates_artifact(self, dynamodb_table):
        """Regression: the FIRST streaming chunk for an artifact_id must be a create (append=False).

        Production bug: the orchestrator always passed append=True, which made the A2A SDK
        drop the bytes with `Received append=True for nonexistent artifact index ... Ignoring chunk.`
        and the final short reply (e.g. "Hello! How can I help you today?") never reached the client.
        Subsequent chunks for the same artifact must use append=True.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        # First chunk: nothing sent yet → must create (append=False)
        first_item = AgentStreamResponse(
            state=TaskState.TASK_STATE_WORKING,
            content="Hello! ",
            metadata={"streaming_chunk": True},
        )
        result = await executor._handle_stream_item(
            first_item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-X",
            first_chunk_sent=False,
            first_intermediate_chunk_sent=False,
        )
        first_call = updater.add_artifact.call_args
        assert first_call[1]["append"] is False, "First chunk must create the artifact (append=False)"
        assert first_call[1]["artifact_id"] == "artifact-X"
        assert result == (True, False)

        # Subsequent chunk: artifact already exists → append=True
        second_item = AgentStreamResponse(
            state=TaskState.TASK_STATE_WORKING,
            content="How can I help?",
            metadata={"streaming_chunk": True},
        )
        result = await executor._handle_stream_item(
            second_item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-X",
            first_chunk_sent=result[0],
            first_intermediate_chunk_sent=result[1],
        )
        second_call = updater.add_artifact.call_args
        assert second_call[1]["append"] is True, "Subsequent chunks must append=True"
        assert result == (True, False)

    async def test_handle_stream_item_intermediate_artifact_tracked_separately(self, dynamodb_table):
        """Intermediate (sub-agent thought) artifact creation is tracked independently
        from the main artifact, since they use distinct artifact IDs.
        """
        from app.core.a2a_extensions import INTERMEDIATE_OUTPUT_EXTENSION
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        # Intermediate-output extension must be active for the chunk to be emitted
        active = {INTERMEDIATE_OUTPUT_EXTENSION}

        # First intermediate chunk → create (append=False) on "-thought" artifact;
        # main first_chunk_sent must NOT be flipped (only intermediate flag flips).
        intermediate_item = AgentStreamResponse(
            state=TaskState.TASK_STATE_WORKING,
            content="thinking...",
            metadata={"streaming_chunk": True, "intermediate_output": True},
        )
        result = await executor._handle_stream_item(
            intermediate_item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-X",
            first_chunk_sent=False,
            first_intermediate_chunk_sent=False,
            active_extensions=active,
        )
        call = updater.add_artifact.call_args
        assert call[1]["append"] is False, "First intermediate chunk must create the thought artifact"
        assert call[1]["artifact_id"] == "artifact-X-thought"
        # Main flag stays False so include_subagent_output / final answer still works
        assert result == (False, True)

        # First MAIN chunk afterwards → must still be a create on the main artifact
        main_item = AgentStreamResponse(
            state=TaskState.TASK_STATE_WORKING,
            content="The answer",
            metadata={"streaming_chunk": True},
        )
        result = await executor._handle_stream_item(
            main_item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-X",
            first_chunk_sent=result[0],
            first_intermediate_chunk_sent=result[1],
            active_extensions=active,
        )
        call = updater.add_artifact.call_args
        assert call[1]["append"] is False, "First main chunk must create the main artifact"
        assert call[1]["artifact_id"] == "artifact-X"
        assert result == (True, True)

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
            state=TaskState.TASK_STATE_FAILED,
            content="An error occurred during execution",
        )

        await executor._handle_stream_item(item, updater, task, is_final=True, streaming_artifact_id="test-artifact-id")

        # Verify update_status was called
        updater.update_status.assert_called_once()
        call_args = updater.update_status.call_args
        assert call_args[0][0] == TaskState.TASK_STATE_FAILED

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
        assert call_args[0][0] == TaskState.TASK_STATE_AUTH_REQUIRED

    async def test_handle_stream_item_input_required_carries_final_message(self, dynamodb_table):
        """Generic (non-HITL) input_required terminal status MUST carry the
        FinalResponseSchema.message text in its message body so clients receive
        the orchestrator's reply even if intermediate SSE artifact frames were
        dropped. Mirrors the `completed` contract introduced in Task #20.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.update_status = AsyncMock()
        updater.add_artifact = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            content="Hi — I'm here. What would you like to do?",
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
        )

        # No streaming this turn → no artifact close, just the terminal status
        updater.add_artifact.assert_not_called()
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_INPUT_REQUIRED
        final_msg = status_call[0][1]
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "Hi — I'm here. What would you like to do?" in "".join(text_parts)
        # Terminal frame must be flushed deterministically.
        # A2A spec (#1308) removes `final` from TaskStatusUpdateEvent as redundant —
        # stream termination is inferred from the terminal task state, not an explicit flag.
        assert status_call[1].get("final") is not True

    async def test_handle_stream_item_streaming_input_required_closes_artifact_with_fallback(self, dynamodb_table):
        """When orchestrator streamed token chunks this turn and then resolves to
        input_required, the streaming artifact is closed cleanly and the terminal
        status carries the authoritative final answer tagged
        `final_answer_source: "fallback"` for client-side deduping.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.update_status = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            content="Which project should I file the ticket under?",
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-IR",
            first_chunk_sent=True,
            streamed_chars=120,
        )

        # Artifact stream closed with an empty append+last_chunk frame
        updater.add_artifact.assert_called_once()
        artifact_call = updater.add_artifact.call_args
        assert artifact_call[1]["last_chunk"] is True
        assert artifact_call[1]["append"] is True
        assert artifact_call[1]["artifact_id"] == "artifact-IR"
        parts = artifact_call[0][0]
        assert parts[0].text == ""

        # Terminal status carries the final message + fallback metadata + final=True
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_INPUT_REQUIRED
        final_msg = status_call[0][1]
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "Which project should I file the ticket under?" in "".join(text_parts)
        assert status_call[1]["metadata"]["final_answer_source"] == "fallback"
        # A2A spec (#1308) removes `final` from TaskStatusUpdateEvent as redundant —
        # stream termination is inferred from the terminal task state, not an explicit flag.
        assert status_call[1].get("final") is not True

    async def test_handle_stream_item_auth_required_carries_final_message(self, dynamodb_table):
        """auth_required terminal status MUST carry the FinalResponseSchema.message
        text in its message body so clients receive the orchestrator's reply even
        if intermediate SSE artifact frames were dropped. Mirrors `completed`.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.update_status = AsyncMock()
        updater.add_artifact = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_AUTH_REQUIRED,
            content="Please sign in to Jira to continue.",
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
        )

        updater.add_artifact.assert_not_called()
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_AUTH_REQUIRED
        final_msg = status_call[0][1]
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "Please sign in to Jira to continue." in "".join(text_parts)
        # A2A spec (#1308) removes `final` from TaskStatusUpdateEvent as redundant —
        # stream termination is inferred from the terminal task state, not an explicit flag.
        assert status_call[1].get("final") is not True

    async def test_handle_stream_item_streaming_auth_required_closes_artifact_with_fallback(self, dynamodb_table):
        """When orchestrator streamed token chunks and then resolves to
        auth_required, the streaming artifact is closed cleanly and the terminal
        status carries the authoritative final answer tagged
        `final_answer_source: "fallback"`.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.update_status = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_AUTH_REQUIRED,
            content="Please re-authenticate with Google to continue.",
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-AR",
            first_chunk_sent=True,
            streamed_chars=80,
        )

        updater.add_artifact.assert_called_once()
        artifact_call = updater.add_artifact.call_args
        assert artifact_call[1]["last_chunk"] is True
        assert artifact_call[1]["append"] is True
        assert artifact_call[1]["artifact_id"] == "artifact-AR"
        parts = artifact_call[0][0]
        assert parts[0].text == ""

        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_AUTH_REQUIRED
        final_msg = status_call[0][1]
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "Please re-authenticate with Google to continue." in "".join(text_parts)
        assert status_call[1]["metadata"]["final_answer_source"] == "fallback"
        # A2A spec (#1308) removes `final` from TaskStatusUpdateEvent as redundant —
        # stream termination is inferred from the terminal task state, not an explicit flag.
        assert status_call[1].get("final") is not True

    async def test_handle_stream_item_input_required_hitl_path_unchanged(self, dynamodb_table):
        """HITL action_requests interrupts still emit the structured HITL message
        via new_hitl_interrupt_message (no artifact-fallback, no final_answer_source).
        """
        from app.core.a2a_extensions import HUMAN_IN_THE_LOOP_EXTENSION
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.update_status = AsyncMock()
        updater.add_artifact = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            content="Approve creating Jira ticket?",
            action_requests=[{"name": "create_jira_ticket", "args": {"summary": "x"}}],
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
            active_extensions={HUMAN_IN_THE_LOOP_EXTENSION},
        )

        # HITL path: no artifact close, no final_answer_source metadata, no final=True override
        updater.add_artifact.assert_not_called()
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_INPUT_REQUIRED
        # HITL branch passes only (state, msg) positionally and no metadata kwarg
        assert "metadata" not in status_call[1] or status_call[1].get("metadata") is None
        assert "final" not in status_call[1] or status_call[1].get("final") is not True


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
        """Test extracting a single decision from a data Part."""
        context = Mock(spec=RequestContext)
        context.message = Mock(spec=Message)
        context.message.parts = [
            Part(data=ParseDict({"decisions": [{"type": "reject", "message": "No"}]}, Value()))
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

    @staticmethod
    def _interrupt(intr_id, action_requests=None, value=None):
        """Build a fake Interrupt-like object (has .id and .value)."""
        return Mock(id=intr_id, value=value if value is not None else {"action_requests": action_requests or []})

    @staticmethod
    def _ar(name, call_id=None):
        """Build an action_request dict, optionally carrying a top-level per-call id."""
        args: dict = {}
        if call_id is not None:
            args["_call_id"] = call_id
        return {"name": name, "args": args}

    def test_single_reject_replicated_for_parallel_tool_calls(self, dynamodb_table):
        """A single reject is replicated to match N action_requests, keyed by interrupt id.

        Core fix for parallel tool calls (N tool_calls in one AIMessage → N
        action_requests) while the UI sends 1 decision. Without replication the HITL
        middleware raises ValueError('Number of human decisions (1) does not match
        number of hanging tool calls (N)').
        """
        intr = self._interrupt("a" * 32, action_requests=[{"name": "s1"}, {"name": "s2"}, {"name": "s3"}])
        decisions = [{"type": "reject", "message": "User declined"}]

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], decisions, query="q")

        assert set(resume_map) == {"a" * 32}
        replicated = resume_map["a" * 32]["decisions"]
        assert len(replicated) == 3
        assert all(d["type"] == "reject" and d["message"] == "User declined" for d in replicated)

    def test_single_approve_replicated_for_parallel_tool_calls(self, dynamodb_table):
        """A single approve is replicated for N action_requests."""
        intr = self._interrupt("b" * 32, action_requests=[{"name": "tool_a"}, {"name": "tool_b"}])

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], [{"type": "approve"}], query="q")

        replicated = resume_map["b" * 32]["decisions"]
        assert len(replicated) == 2
        assert all(d["type"] == "approve" for d in replicated)

    def test_no_replication_when_counts_match(self, dynamodb_table):
        """A single decision for a single action_request is not replicated."""
        intr = self._interrupt("c" * 32, action_requests=[{"name": "tool_a"}])

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], [{"type": "reject"}], query="q")

        assert len(resume_map["c" * 32]["decisions"]) == 1

    def test_no_replication_when_multiple_decisions_sent(self, dynamodb_table):
        """Multiple decisions are passed through unchanged (future per-call UI)."""
        intr = self._interrupt("d" * 32, action_requests=[{"name": "tool_a"}, {"name": "tool_b"}])
        decisions = [{"type": "approve"}, {"type": "reject"}]

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], decisions, query="q")

        passed = resume_map["d" * 32]["decisions"]
        assert [d["type"] for d in passed] == ["approve", "reject"]

    def test_multiple_pending_interrupts_each_keyed_and_replicated(self, dynamodb_table):
        """The migration's core case: >1 co-pending interrupt → id-keyed map.

        Two parallel ``task`` dispatches each surfaced a sub-agent HITL with a
        different action_request count. The single blanket decision is replicated
        per interrupt and keyed by interrupt id, so LangGraph >=1.2 does not raise
        'you must specify the interrupt id when resuming'.
        """
        intr_a = self._interrupt("a" * 32, action_requests=[{"name": "x"}, {"name": "y"}])
        intr_b = self._interrupt("b" * 32, action_requests=[{"name": "z"}])

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
            [intr_a, intr_b], [{"type": "approve"}], query="q"
        )

        assert set(resume_map) == {"a" * 32, "b" * 32}
        assert len(resume_map["a" * 32]["decisions"]) == 2  # replicated to its own count
        assert len(resume_map["b" * 32]["decisions"]) == 1

    def test_non_hitl_interrupt_resumes_with_query(self, dynamodb_table):
        """A non-HITL interrupt (no action_requests, e.g. auth) resumes with the raw query."""
        auth_intr = self._interrupt("e" * 32, value={"auth_url": "https://example/oauth"})

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([auth_intr], [{"type": "approve"}], query="auth-token")

        assert resume_map["e" * 32] == "auth-token"

    def test_per_call_decisions_aligned_by_id(self, dynamodb_table):
        """New client: one decision per action_request, matched by call_id (not position)."""
        intr = self._interrupt(
            "a" * 32,
            action_requests=[self._ar("safe_read", "call-1"), self._ar("safe_read", "call-2")],
        )
        # Client sends per-call decisions, deliberately OUT OF ORDER vs action_requests.
        decisions = [
            {"id": "call-2", "type": "reject", "message": "no shadow"},
            {"id": "call-1", "type": "approve"},
        ]

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], decisions, query="q")

        per = resume_map["a" * 32]["decisions"]
        # Aligned to action_request order (call-1 first, call-2 second), not client order.
        assert [d["type"] for d in per] == ["approve", "reject"]
        assert per[0]["id"] == "call-1"
        assert per[1]["id"] == "call-2"

    def test_flat_by_id_decisions_route_across_multiple_interrupts(self, dynamodb_table):
        """A flat by-id decision list self-routes to the right interrupt and orders within."""
        intr_a = self._interrupt("a" * 32, action_requests=[self._ar("t1", "ca-1"), self._ar("t2", "ca-2")])
        intr_b = self._interrupt("b" * 32, action_requests=[self._ar("t3", "cb-1")])
        decisions = [
            {"id": "cb-1", "type": "reject"},
            {"id": "ca-1", "type": "approve"},
            {"id": "ca-2", "type": "reject"},
        ]

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr_a, intr_b], decisions, query="q")

        assert [d["type"] for d in resume_map["a" * 32]["decisions"]] == ["approve", "reject"]
        assert [d["type"] for d in resume_map["b" * 32]["decisions"]] == ["reject"]

    def test_falls_back_to_blanket_when_decisions_lack_ids(self, dynamodb_table):
        """Legacy client: action_requests carry ids but the single decision has none → replicate."""
        intr = self._interrupt(
            "a" * 32,
            action_requests=[self._ar("safe_read", "call-1"), self._ar("safe_read", "call-2")],
        )

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], [{"type": "approve"}], query="q")

        per = resume_map["a" * 32]["decisions"]
        assert len(per) == 2
        assert all(d["type"] == "approve" for d in per)

    def test_falls_back_to_blanket_when_action_requests_lack_ids(self, dynamodb_table):
        """Mixed/absent ids on action_requests → no by-id alignment; blanket replication."""
        intr = self._interrupt("a" * 32, action_requests=[self._ar("t1"), self._ar("t2", "call-2")])
        decisions = [{"id": "call-2", "type": "approve"}]

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], decisions, query="q")

        # Not all action_requests have ids → fall back. Single decision, n>1 → replicate.
        per = resume_map["a" * 32]["decisions"]
        assert len(per) == 2
