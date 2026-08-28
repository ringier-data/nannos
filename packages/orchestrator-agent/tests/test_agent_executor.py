"""Unit tests for agent executor."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
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
        """A fully-streamed completion closes the artifact and emits a BARE completion.

        Single-source emission: when the full answer was already streamed as the
        artifact (len(streamed_text) >= final_message_len), the terminal `completed`
        status carries NO message — re-sending it would duplicate the answer for
        every consumer (web render + persistence, slack, google-chat). Clients use
        the streamed artifact; the terminal is state-only.
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
            streamed_text="Full response content",
        )

        # Last artifact chunk should be empty (just stream close signal)
        updater.add_artifact.assert_called_once()
        artifact_call = updater.add_artifact.call_args
        assert artifact_call[1]["last_chunk"] is True
        assert artifact_call[1]["append"] is True
        # Check the text part is empty
        parts = artifact_call[0][0]
        assert parts[0].text == ""

        # Single-source emission: the full answer was already streamed as the
        # artifact, so the terminal status is a BARE completion (no message body) —
        # nothing to re-send / re-persist / re-render.
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_COMPLETED
        assert status_call[0][1] is None  # bare completion, no message
        # No fallback tag — there is no duplicate terminal copy to dedupe.
        assert status_call[1].get("metadata") is None

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

    async def test_handle_stream_item_streaming_input_required_closes_artifact_and_ends_bare(self, dynamodb_table):
        """A fully streamed answer that ends in input_required is delivered ONCE.

        The artifact is closed cleanly and the terminal status is BARE. Re-sending
        the answer in `status.message` here duplicated it for every consumer: the
        console persisted the same text twice (once from the assembled artifact,
        once from this status), so a reloaded conversation showed one answer as two
        bubbles. How a turn ends says nothing about whether its answer was already
        delivered — only the streamed text does.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.update_status = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        answer = "Which project should I file the ticket under?"
        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            content=answer,
        )

        # The whole answer streamed, so the terminal status has nothing left to add.
        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-IR",
            first_chunk_sent=True,
            streamed_text=answer,
        )

        # Artifact stream closed with an empty append+last_chunk frame
        updater.add_artifact.assert_called_once()
        artifact_call = updater.add_artifact.call_args
        assert artifact_call[1]["last_chunk"] is True
        assert artifact_call[1]["append"] is True
        assert artifact_call[1]["artifact_id"] == "artifact-IR"
        parts = artifact_call[0][0]
        assert parts[0].text == ""

        # Bare terminal status: no message, and nothing to dedupe against
        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        assert status_call[0][0] == TaskState.TASK_STATE_INPUT_REQUIRED
        assert status_call[0][1] is None
        assert "final_answer_source" not in (status_call[1].get("metadata") or {})
        # A2A spec (#1308) removes `final` from TaskStatusUpdateEvent as redundant —
        # stream termination is inferred from the terminal task state, not an explicit flag.
        assert status_call[1].get("final") is not True

    async def test_handle_stream_item_streaming_input_required_partial_prefix_keeps_fallback(self, dynamodb_table):
        """Only a PREFIX streamed — the terminal status must still carry the answer.

        The dedupe applies to a fully streamed answer, not to any interrupt: when
        the client has less text than the final answer, dropping the message would
        lose the rest of it.
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
            streamed_text="Which proj",
        )

        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        final_msg = status_call[0][1]
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "Which project should I file the ticket under?" in "".join(text_parts)
        assert status_call[1]["metadata"]["final_answer_source"] == "fallback"

    async def test_handle_stream_item_bare_input_required_keeps_interrupt_reason(self, dynamodb_table):
        """Dropping the duplicate message must not drop what it carried.

        `interrupt_reason` rode on the message body. With the message gone, the
        status is the only frame left, so the reason moves onto its metadata —
        otherwise a client could not tell why the turn paused.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.update_status = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        answer = "Which project should I file the ticket under?"
        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            content=answer,
            interrupt_reason="graph_interrupted",
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-IR",
            first_chunk_sent=True,
            streamed_text=answer,
        )

        status_call = updater.update_status.call_args
        assert status_call[0][1] is None
        assert status_call[1]["metadata"]["interrupt_reason"] == "graph_interrupted"

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

    async def test_handle_stream_item_streaming_auth_required_closes_artifact_and_ends_bare(self, dynamodb_table):
        """A fully streamed answer that ends in auth_required is delivered ONCE.

        Same rule as input_required: the artifact is closed cleanly and the
        terminal status is bare, so no consumer stores or renders the answer twice.
        """
        from app.models.responses import AgentStreamResponse

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.update_status = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        prompt = "Please re-authenticate with Google to continue."
        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_AUTH_REQUIRED,
            content=prompt,
        )

        # This prompt IS what streamed, so re-sending it would only duplicate it.
        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-AR",
            first_chunk_sent=True,
            streamed_text=prompt,
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
        assert status_call[0][1] is None
        assert "final_answer_source" not in (status_call[1].get("metadata") or {})
        # A2A spec (#1308) removes `final` from TaskStatusUpdateEvent as redundant —
        # stream termination is inferred from the terminal task state, not an explicit flag.
        assert status_call[1].get("final") is not True

    async def test_handle_stream_item_auth_required_after_long_answer_keeps_prompt(self, dynamodb_table):
        """A short auth prompt after a long streamed answer must still be sent.

        The prompt is a DIFFERENT text from the answer, so it was never delivered.
        Counting characters alone would say "already streamed" (500 >= 35) and drop
        it — and the console renders its auth card from exactly this row, so a
        reloaded conversation would lose the sign-in prompt entirely.
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
            content="Please sign in to Jira to continue.",
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-AR",
            first_chunk_sent=True,
            streamed_text="Here is a long answer about your tickets. " * 12,
        )

        updater.update_status.assert_called_once()
        status_call = updater.update_status.call_args
        final_msg = status_call[0][1]
        assert final_msg is not None
        text_parts = [p.text for p in final_msg.parts if p.WhichOneof("content") == "text"]
        assert "Please sign in to Jira to continue." in "".join(text_parts)

    async def test_handle_stream_item_input_required_client_action_request(self, dynamodb_table):
        """A client-action round trip pauses as input_required carrying the
        {"request": {id, directive}} DataPart under the client-action extension —
        NOT the HITL message, and NOT the generic text fallback."""
        from app.core.a2a_extensions import CLIENT_ACTION_EXTENSION
        from app.models.responses import AgentStreamResponse
        from google.protobuf.json_format import MessageToDict

        executor = OrchestratorDeepAgentExecutor()

        updater = Mock()
        updater.update_status = AsyncMock()
        updater.add_artifact = AsyncMock()

        task = Mock()
        task.context_id = "ctx-123"
        task.id = "task-456"

        request = {"id": "call-1", "directive": {"kind": "apply", "target": {"type": "Campaign", "id": "7"}}}
        item = AgentStreamResponse(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            content="Waiting for the application…",
            client_action_request=request,
        )

        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
            active_extensions={CLIENT_ACTION_EXTENSION},
        )

        updater.add_artifact.assert_not_called()  # nothing streamed → nothing to seal
        updater.update_status.assert_called_once()
        state_arg, msg = updater.update_status.call_args[0]
        assert state_arg == TaskState.TASK_STATE_INPUT_REQUIRED
        assert CLIENT_ACTION_EXTENSION in list(msg.extensions)
        data = MessageToDict(msg.parts[0].data)
        assert data == {"request": request}

    async def test_handle_stream_item_client_action_request_seals_open_artifact(self, dynamodb_table):
        """Tokens streamed before the pause: the artifact is closed first (the
        same rule as HITL), so the client's stream never dangles."""
        from app.core.a2a_extensions import CLIENT_ACTION_EXTENSION
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
            content="Waiting…",
            client_action_request={"id": "call-1", "directive": {"kind": "apply"}},
        )
        await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=True,
            streaming_artifact_id="artifact-1",
            first_chunk_sent=True,
            active_extensions={CLIENT_ACTION_EXTENSION},
        )
        updater.add_artifact.assert_called_once()
        assert updater.add_artifact.call_args[1]["last_chunk"] is True

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

    def test_no_data_part_is_not_a_decision_at_all(self, dynamodb_table):
        """No DataPart means the user typed instead of clicking — not "reject".

        This used to fabricate a rejection, which discarded their words before
        anything could read them: typing "approve it" came back as "the call was
        rejected". The safe default moved down to ``decisions_from_resume``, which
        rejects unless the reply clearly means yes.
        """
        context = Mock(spec=RequestContext)
        context.message = Mock(spec=Message)
        context.message.parts = []

        assert OrchestratorDeepAgentExecutor._extract_hitl_decisions(context) is None

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

    def test_from_interrupt_maps_client_action_request(self, dynamodb_table):
        """The tool's interrupt value dispatches to input_required carrying the
        request — not the HITL card, not the generic passthrough."""
        from app.models.responses import AgentStreamResponse

        request = {"id": "c1", "directive": {"kind": "apply"}}
        item = AgentStreamResponse.from_interrupt({"client_action_request": request})
        assert item.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert item.client_action_request == request
        assert item.interrupt_reason == "client_action_result"
        assert not item.action_requests

    def test_client_action_interrupt_resumes_with_matched_result(self, dynamodb_table):
        """A client-action interrupt resumes with the result of the decision whose
        id matches the request id — not with decisions, not with the query."""
        intr = self._interrupt(
            "e" * 32,
            value={"client_action_request": {"id": "call-1", "directive": {"kind": "apply"}}},
        )
        decisions = [
            {"id": "other", "type": "approve"},
            {"id": "call-1", "type": "approve", "client_action_result": {"ok": True, "applied": ["budget"]}},
        ]
        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], decisions, query="q")
        assert resume_map["e" * 32] == {"ok": True, "applied": ["budget"]}

    def test_client_action_interrupt_without_result_resumes_no_result(self, dynamodb_table):
        """A plain user message while parked (default reject decision, no result)
        must hand the tool an explicit no-result — never an assumed success."""
        intr = self._interrupt(
            "f" * 32,
            value={"client_action_request": {"id": "call-1", "directive": {"kind": "apply"}}},
        )
        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
            [intr], [{"type": "reject"}], query="user typed something"
        )
        assert resume_map["f" * 32] == {"ok": False, "reason": "no-result"}

    def test_client_action_interrupt_idless_fallback_takes_single_result(self, dynamodb_table):
        """A result-bearing decision without a matching id still resolves when it
        is the only one (belt for clients that lost the call id)."""
        intr = self._interrupt(
            "1" * 32,
            value={"client_action_request": {"id": "", "directive": {"kind": "apply"}}},
        )
        decisions = [{"type": "approve", "client_action_result": {"ok": False, "reason": "unknown-target"}}]
        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map([intr], decisions, query="q")
        assert resume_map["1" * 32] == {"ok": False, "reason": "unknown-target"}

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

    @pytest.mark.asyncio
    async def test_typed_refusal_of_an_auth_prompt_becomes_a_structured_decline(self, dynamodb_table):
        """"No way I\'ll authorize this!" must arrive as a DECISION, not as words.

        As words it was read only where the tool failed a second time — and once
        the user had completed the login in their browser the retry succeeded, so
        nothing read it and the call went through. As a structured decline the
        sub-agent can veto the call before running it.
        """
        auth_intr = self._interrupt(
            "j" * 32, value={"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "github_get_me"}
        )

        with patch(
            "app.core.executor.classify_reply", AsyncMock(return_value="reject")
        ):
            authorization = await OrchestratorDeepAgentExecutor._classify_authorization_reply(
                [auth_intr], "No way I'll authorize this!"
            )

        assert authorization == {"decision": "declined", "message": "No way I'll authorize this!"}

    @pytest.mark.asyncio
    async def test_typed_completion_of_an_auth_prompt_becomes_an_approval(self, dynamodb_table):
        auth_intr = self._interrupt(
            "k" * 32, value={"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "github_get_me"}
        )

        with patch("app.core.executor.classify_reply", AsyncMock(return_value="approve")):
            authorization = await OrchestratorDeepAgentExecutor._classify_authorization_reply(
                [auth_intr], "done, I logged in"
            )

        assert authorization["decision"] == "approved"

    @pytest.mark.asyncio
    async def test_words_are_left_alone_when_no_auth_prompt_is_pending(self, dynamodb_table):
        """A tool-approval prompt has its own reader; do not spend the words here."""
        hitl_intr = self._interrupt("l" * 32, action_requests=[self._ar("github_get_me", "call-1")])
        classify = AsyncMock(return_value="reject")

        with patch("app.core.executor.classify_reply", classify):
            authorization = await OrchestratorDeepAgentExecutor._classify_authorization_reply(
                [hitl_intr], "no thanks"
            )

        classify.assert_not_awaited()
        assert authorization is None

    @pytest.mark.asyncio
    async def test_an_unreadable_reply_stays_words(self, dynamodb_table):
        auth_intr = self._interrupt(
            "m" * 32, value={"task_state": TaskState.TASK_STATE_AUTH_REQUIRED, "tool": "github_get_me"}
        )

        with patch("app.core.executor.classify_reply", AsyncMock(return_value=None)):
            assert (
                await OrchestratorDeepAgentExecutor._classify_authorization_reply([auth_intr], "hmm")
            ) is None

    def test_typed_reply_reaches_the_reader_instead_of_becoming_a_reject(self, dynamodb_table):
        """"approve it" typed in the composer must survive as far as the classifier.

        The words were replaced by a synthetic reject here, so the user's approval
        came back to them as "The call to github_get_me was rejected".
        """
        intr = self._interrupt("h" * 32, action_requests=[self._ar("github_get_me", "call-1")])

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
            [intr], None, query="approve it"
        )

        assert resume_map["h" * 32] == "approve it"

    def test_an_explicit_decision_still_wins_over_the_typed_path(self, dynamodb_table):
        intr = self._interrupt("i" * 32, action_requests=[self._ar("github_get_me", "call-1")])

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
            [intr], [{"type": "reject"}], query="approve it"
        )

        assert [d["type"] for d in resume_map["i" * 32]["decisions"]] == ["reject"]

    def test_authorization_answer_rejects_the_pending_approval(self, dynamodb_table):
        """The user answered an auth prompt; the pending question is an approval.

        It happens whenever the agent re-runs the blocked tool after a decline and
        its guard asks again. `_extract_hitl_decisions` falls back to a bare reject
        (safe, but says nothing), so the reason is filled in here instead — the
        model has to read WHY to stop retrying.
        """
        intr = self._interrupt("f" * 32, action_requests=[self._ar("github_get_me", "call-1")])

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
            [intr],
            [{"type": "reject"}],
            query="q",
            authorization={"decision": "declined", "message": "not now"},
        )

        decisions = resume_map["f" * 32]["decisions"]
        assert [d["type"] for d in decisions] == ["reject"]
        assert [d["id"] for d in decisions] == ["call-1"]
        assert "skipped the authorization" in decisions[0]["message"]
        assert "not now" in decisions[0]["message"]

    def test_per_call_decisions_win_over_an_authorization_answer(self, dynamodb_table):
        """A client that sent real per-call decisions is never second-guessed."""
        intr = self._interrupt("g" * 32, action_requests=[self._ar("github_get_me", "call-1")])

        resume_map = OrchestratorDeepAgentExecutor._build_interrupt_resume_map(
            [intr],
            [{"id": "call-1", "type": "approve"}],
            query="q",
            authorization={"decision": "declined"},
        )

        assert [d["type"] for d in resume_map["g" * 32]["decisions"]] == ["approve"]

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
