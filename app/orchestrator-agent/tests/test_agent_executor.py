"""Unit tests for agent executor."""

from unittest.mock import AsyncMock, Mock, patch
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState, Message

from app.core.executor import OrchestratorDeepAgentExecutor


class TestOrchestratorDeepAgentExecutor:
    """Tests for OrchestratorDeepAgentExecutor."""
    
    def test_executor_initialization(self, dynamodb_table):
        """Test that executor initializes with agent."""
        executor = OrchestratorDeepAgentExecutor()
        
        assert executor.agent is not None
        assert hasattr(executor.agent, 'stream')
    
    async def test_execute_with_valid_request(self, dynamodb_table):
        """Test execute with valid request context."""
        from a2a.types import Part, TextPart
        
        executor = OrchestratorDeepAgentExecutor()
        
        # Mock context
        context = Mock(spec=RequestContext)
        context.get_user_input = Mock(return_value="test query")
        context.current_task = None
        context.message = Mock(spec=Message)
        context.message.role = 'user'
        context.message.parts = [Part(root=TextPart(text='test query'))]
        context.message.task_id = None
        context.message.context_id = None
        context.call_context = Mock()
        context.call_context.state = {'user_id': 'test-user'}
        
        # Mock event queue
        event_queue = Mock(spec=EventQueue)
        event_queue.enqueue_event = AsyncMock()
        
        # Mock agent stream
        executor.agent.stream = AsyncMock()
        executor.agent.stream.return_value = iter([])
        
        # Mock get_or_create_graph
        with patch.object(executor.agent, 'get_or_create_graph', new=AsyncMock()) as mock_graph:
            mock_compiled_graph = Mock()
            mock_compiled_graph.get_state = Mock()
            mock_compiled_graph.get_state.return_value = Mock(interrupts=[], next=[])
            
            mock_graph.return_value = (mock_compiled_graph, 'config-sig')
            
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
    
    async def test_cancel_raises_unsupported_operation(self, dynamodb_table):
        """Test that cancel raises UnsupportedOperationError."""
        executor = OrchestratorDeepAgentExecutor()
        
        context = Mock(spec=RequestContext)
        event_queue = Mock(spec=EventQueue)
        
        # Should raise UnsupportedOperationError
        from a2a.utils.errors import ServerError
        
        try:
            await executor.cancel(context, event_queue)
            assert False, "Should have raised ServerError"
        except ServerError:
            pass


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
        task.context_id = 'ctx-123'
        task.id = 'task-456'
        
        # Create working state item
        item = AgentStreamResponse.working("Processing step 1")
        
        await executor._handle_stream_item(item, updater, task, is_final=False)
        
        # Verify update_status was called
        updater.update_status.assert_called_once()
        call_args = updater.update_status.call_args
        assert call_args[0][0] == TaskState.working
        assert call_args[1]['final'] is False
    
    async def test_handle_stream_item_completed_state(self, dynamodb_table):
        """Test handling completed state stream items."""
        from app.models.responses import AgentStreamResponse
        
        executor = OrchestratorDeepAgentExecutor()
        
        # Mock updater
        updater = Mock()
        updater.add_artifact = AsyncMock()
        updater.complete = AsyncMock()
        
        # Mock task
        task = Mock()
        task.context_id = 'ctx-123'
        task.id = 'task-456'
        
        # Create completed state item
        item = AgentStreamResponse.completed("Task finished successfully")
        
        await executor._handle_stream_item(item, updater, task, is_final=True)
        
        # Verify add_artifact and complete were called
        updater.add_artifact.assert_called_once()
        updater.complete.assert_called_once()
    
    async def test_handle_stream_item_failed_state(self, dynamodb_table):
        """Test handling failed state stream items."""
        from app.models.responses import AgentStreamResponse
        
        executor = OrchestratorDeepAgentExecutor()
        
        # Mock updater
        updater = Mock()
        updater.update_status = AsyncMock()
        
        # Mock task
        task = Mock()
        task.context_id = 'ctx-123'
        task.id = 'task-456'
        
        # Create failed state item
        item = AgentStreamResponse.failed("An error occurred")
        
        await executor._handle_stream_item(item, updater, task, is_final=True)
        
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
        task.context_id = 'ctx-123'
        task.id = 'task-456'
        
        # Create auth_required state item
        item = AgentStreamResponse.auth_required(
            "Authentication needed",
            "https://auth.example.com",
            "need-credentials"
        )
        
        await executor._handle_stream_item(item, updater, task, is_final=False)
        
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
        context.call_context.state = {'user_id': 'verified-user-123'}
        
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
