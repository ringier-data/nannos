"""Tests for A2AClientRunnable - Agent-to-Agent client communication.

These tests focus on the core functionality of A2AClientRunnable with minimal mocking:
- Uses real Pydantic models instead of mocks where possible
- Tests streaming and non-streaming interfaces
- Verifies error handling and edge cases
- Tests distributed tracing header injection
"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from a2a.types import (
    AgentCard,
    Artifact,
    Message,
    Part,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from a2a.types import Role as A2ARole
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.client_runnable import A2AClientRunnable
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.stream_events import ErrorEvent, TaskUpdate
from langchain_core.messages import AIMessage, HumanMessage


@pytest.fixture
def agent_card():
    """Create a test agent card."""
    from a2a.types import AgentCapabilities, AgentSkill

    return AgentCard(
        name="test-agent",
        description="Test agent for unit tests",
        url="https://test-agent.example.com/a2a",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id="test", name="test-skill", description="Test skill", tags=["test"])],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


@pytest.fixture
def client_config():
    """Create a test client configuration."""
    return A2AClientConfig(
        timeout_connect=5.0,
        timeout_read=30.0,
        user_agent_prefix="TestClient/1.0",
    )


@pytest.fixture
def sub_agent_input():
    """Create test input data for sub-agent."""
    return SubAgentInput(
        messages=[HumanMessage(content="Test message")],
        a2a_tracking={},
    )


@pytest.fixture
def a2a_client_runnable(agent_card, client_config):
    """Create A2AClientRunnable instance for testing."""
    return A2AClientRunnable(
        agent_card=agent_card,
        config=client_config,
    )


class TestA2AClientRunnableInitialization:
    """Test initialization and configuration."""

    def test_initialization_with_defaults(self, agent_card):
        """Test runnable initializes with default config."""
        runnable = A2AClientRunnable(agent_card=agent_card)

        assert runnable.agent_card == agent_card
        assert runnable.config is not None
        assert runnable._client is None  # Lazy initialization

    def test_initialization_with_custom_config(self, agent_card, client_config):
        """Test runnable initializes with custom config."""
        runnable = A2AClientRunnable(agent_card=agent_card, config=client_config)

        assert runnable.agent_card == agent_card
        assert runnable.config == client_config
        assert runnable.config.timeout_connect == 5.0

    def test_name_property(self, a2a_client_runnable, agent_card):
        """Test name property returns agent card name."""
        assert a2a_client_runnable.name == agent_card.name

    def test_description_property(self, a2a_client_runnable, agent_card):
        """Test description property returns agent card description."""
        assert a2a_client_runnable.description == agent_card.description


class TestA2AClientRunnableTextExtraction:
    """Test text extraction from A2A parts."""

    def test_extract_text_from_text_parts(self, a2a_client_runnable):
        """Test extracting text from TextPart objects."""
        parts = [
            Part(root=TextPart(text="First part")),
            Part(root=TextPart(text="Second part")),
        ]

        result = a2a_client_runnable._extract_text_from_parts(parts)

        assert result == "First part\nSecond part"

    def test_extract_text_from_empty_parts(self, a2a_client_runnable):
        """Test extracting text from empty parts list."""
        result = a2a_client_runnable._extract_text_from_parts([])

        assert result == ""

    def test_extract_text_from_mixed_parts(self, a2a_client_runnable):
        """Test extracting text from mixed part types."""
        from a2a.types import DataPart

        parts = [
            Part(root=TextPart(text="Text content")),
            Part(root=DataPart(data={"key": "value"})),
        ]

        result = a2a_client_runnable._extract_text_from_parts(parts)

        assert "Text content" in result
        assert '"key": "value"' in result  # JSON serialized


class TestA2AClientRunnableSyntheticMessage:
    """Test synthetic message content creation for LangChain compatibility."""

    def test_create_synthetic_message_completed_task(self, a2a_client_runnable):
        """Test creating synthetic message for completed task."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.completed,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Task completed successfully"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        result = a2a_client_runnable._create_synthetic_message_content(task, {})

        assert result == "Task completed successfully"

    def test_create_synthetic_message_failed_task(self, a2a_client_runnable):
        """Test creating synthetic message for failed task."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.failed,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Database connection failed"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        result = a2a_client_runnable._create_synthetic_message_content(task, {})

        assert "failed" in result.lower()
        assert "Database connection failed" in result

    def test_create_synthetic_message_working_task(self, a2a_client_runnable):
        """Test creating synthetic message for working task."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.working,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Processing data"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        result = a2a_client_runnable._create_synthetic_message_content(task, {})

        assert "INCOMPLETE" in result
        assert "working" in result.lower()
        assert "Processing data" in result

    def test_create_synthetic_message_auth_required(self, a2a_client_runnable):
        """Test creating synthetic message for auth required task."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.auth_required),
        )
        app_metadata = {"instructions": "Please authenticate with OAuth2"}

        result = a2a_client_runnable._create_synthetic_message_content(task, app_metadata)

        assert "Please authenticate with OAuth2" in result

    def test_create_synthetic_message_from_artifact(self, a2a_client_runnable):
        """Test creating synthetic message from artifact when no status message."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.completed),
            artifacts=[
                Artifact(
                    artifact_id="art-1",
                    name="Result",
                    parts=[Part(root=TextPart(text="Result from artifact"))],
                )
            ],
        )

        result = a2a_client_runnable._create_synthetic_message_content(task, {})

        assert result == "Result from artifact"


class TestA2AClientRunnableAuthPayload:
    """Test authentication payload parsing."""

    def test_parse_auth_payload_with_structured_data(self, a2a_client_runnable):
        """Test parsing auth payload with structured data."""
        from a2a.types import DataPart

        task_status = TaskStatus(
            state=TaskState.auth_required,
            message=Message(
                role=A2ARole.agent,
                parts=[
                    Part(
                        root=DataPart(
                            data={
                                "service": "jira",
                                "auth_methods": [
                                    {
                                        "method": "oauth2",
                                        "description": "OAuth2 authentication",
                                        "instructions": "Complete OAuth flow",
                                    }
                                ],
                            }
                        )
                    )
                ],
                message_id="msg-1",
                context_id="ctx-1",
            ),
        )

        result = a2a_client_runnable._parse_auth_payload(task_status)

        assert result["service"] == "jira"
        assert len(result["auth_methods"]) == 1
        assert result["auth_methods"][0]["method"] == "oauth2"
        assert result["ciba_supported"] is False
        assert result["corporate_sso_preferred"] is True

    def test_parse_auth_payload_with_ciba_method(self, a2a_client_runnable):
        """Test parsing auth payload with CIBA method."""
        from a2a.types import DataPart

        task_status = TaskStatus(
            state=TaskState.auth_required,
            message=Message(
                role=A2ARole.agent,
                parts=[
                    Part(
                        root=DataPart(
                            data={
                                "service": "enterprise-system",
                                "auth_methods": [
                                    {
                                        "method": "ciba",
                                        "description": "CIBA authentication",
                                        "instructions": "Authenticate via corporate SSO",
                                    }
                                ],
                            }
                        )
                    )
                ],
                message_id="msg-1",
                context_id="ctx-1",
            ),
        )

        result = a2a_client_runnable._parse_auth_payload(task_status)

        assert result["ciba_supported"] is True
        assert result["device_code_supported"] is False

    def test_parse_auth_payload_fallback_to_generic(self, a2a_client_runnable):
        """Test parsing auth payload falls back to generic OAuth2."""
        task_status = TaskStatus(
            state=TaskState.auth_required,
            message=Message(
                role=A2ARole.agent,
                parts=[Part(root=TextPart(text="Authentication required"))],
                message_id="msg-1",
                context_id="ctx-1",
            ),
        )

        result = a2a_client_runnable._parse_auth_payload(task_status)

        assert result["service"] == "unknown_service"
        assert len(result["auth_methods"]) == 1
        assert result["auth_methods"][0]["method"] == "oauth2"


class TestA2AClientRunnableTaskResponse:
    """Test task response handling."""

    @pytest.mark.asyncio
    async def test_handle_task_response_completed(self, a2a_client_runnable):
        """Test handling completed task response."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.completed,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Task completed"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        result = await a2a_client_runnable._handle_task_response(task)

        assert result.task_id == "task-1"
        assert result.context_id == "ctx-1"
        assert result.state == TaskState.completed
        assert result.is_complete is True
        assert result.requires_auth is False
        assert len(result.messages) == 1
        assert isinstance(result.messages[0], AIMessage)

    @pytest.mark.asyncio
    async def test_handle_task_response_auth_required(self, a2a_client_runnable):
        """Test handling auth required task response."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.auth_required),
        )

        result = await a2a_client_runnable._handle_task_response(task)

        assert result.state == TaskState.auth_required
        assert result.requires_auth is True
        assert result.is_complete is False

    @pytest.mark.asyncio
    async def test_handle_task_response_failed(self, a2a_client_runnable):
        """Test handling failed task response."""
        task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.failed,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Operation failed"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        result = await a2a_client_runnable._handle_task_response(task)

        assert result.state == TaskState.failed
        assert result.is_complete is True
        assert "failed" in result.messages[0].content.lower()


class TestA2AClientRunnableMessageCreation:
    """Test A2A message creation."""

    def test_create_a2a_message(self, a2a_client_runnable):
        """Test creating A2A message with metadata."""
        content = "Test message"
        context_id = "ctx-123"
        task_id = "task-456"

        result = a2a_client_runnable._create_a2a_message(content, context_id, task_id)

        assert isinstance(result, Message)
        assert result.role == A2ARole.user
        assert result.context_id == context_id
        assert result.task_id == task_id
        assert len(result.parts) == 1
        assert result.parts[0].root.text == content
        assert result.message_id  # Generated UUID

    def test_create_a2a_message_without_tracking(self, a2a_client_runnable):
        """Test creating A2A message without tracking IDs."""
        content = "Test message"

        result = a2a_client_runnable._create_a2a_message(content, None, None)

        assert isinstance(result, Message)
        assert result.context_id is None
        assert result.task_id is None


class TestA2AClientRunnableStreaming:
    """Test streaming functionality."""

    @pytest.mark.asyncio
    async def test_astream_completed_task(self, a2a_client_runnable, sub_agent_input):
        """Test streaming a completed task."""
        completed_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.completed,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Task completed"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        # Mock the A2A client
        mock_client = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield (completed_task, None)

        mock_client.send_message = mock_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            updates = []
            async for update in a2a_client_runnable.astream(sub_agent_input.model_dump()):
                updates.append(update)

        assert len(updates) == 1
        assert updates[0].type == "task_update"
        assert updates[0].data.state == TaskState.completed
        assert updates[0].data.is_complete is True

    @pytest.mark.asyncio
    async def test_astream_auth_required_task(self, a2a_client_runnable, sub_agent_input):
        """Test streaming task that requires authentication."""
        auth_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.auth_required),
        )

        mock_client = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield (auth_task, None)

        mock_client.send_message = mock_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            updates = []
            async for update in a2a_client_runnable.astream(sub_agent_input.model_dump()):
                updates.append(update)

        assert len(updates) == 1
        assert updates[0].data.state == TaskState.auth_required
        assert updates[0].data.requires_input is False
        assert updates[0].data.requires_auth is True

    @pytest.mark.asyncio
    async def test_astream_multiple_updates(self, a2a_client_runnable, sub_agent_input):
        """Test streaming multiple task updates."""
        working_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.working),
        )
        completed_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.completed,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Done"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        mock_client = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield (working_task, None)
            yield (completed_task, None)

        mock_client.send_message = mock_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            updates = []
            async for update in a2a_client_runnable.astream(sub_agent_input.model_dump()):
                updates.append(update)

        assert len(updates) == 2
        assert updates[0].data.state == TaskState.working
        assert updates[0].data.is_complete is False
        assert updates[1].data.state == TaskState.completed
        assert updates[1].data.is_complete is True

    @pytest.mark.asyncio
    async def test_astream_ignores_non_task_items(self, a2a_client_runnable, sub_agent_input):
        """Test streaming ignores unknown item types."""
        completed_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.completed),
        )

        mock_client = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield "invalid_item"  # Should be ignored
            yield (completed_task, None)

        mock_client.send_message = mock_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            updates = []
            async for update in a2a_client_runnable.astream(sub_agent_input.model_dump()):
                updates.append(update)

        # Should only get the valid task update
        assert len(updates) == 1
        assert updates[0].type == "task_update"


class TestA2AClientRunnableInvoke:
    """Test non-streaming invoke functionality."""

    @pytest.mark.asyncio
    async def test_ainvoke_completed_task(self, a2a_client_runnable, sub_agent_input):
        """Test invoking returns completed task result."""
        completed_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(
                state=TaskState.completed,
                message=Message(
                    role=A2ARole.agent,
                    parts=[Part(root=TextPart(text="Task completed"))],
                    message_id="msg-1",
                    context_id="ctx-1",
                ),
            ),
        )

        mock_client = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield (completed_task, None)

        mock_client.send_message = mock_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            result = await a2a_client_runnable.ainvoke(sub_agent_input.model_dump())

        assert isinstance(result, TaskUpdate)
        assert result.data.task_id == "task-1"
        assert result.data.state == TaskState.completed
        assert result.data.is_complete is True

    @pytest.mark.asyncio
    async def test_ainvoke_handles_connection_error(self, a2a_client_runnable, sub_agent_input):
        """Test ainvoke handles connection errors gracefully."""

        # Create a custom async generator that raises on first iteration
        async def error_stream(*args, **kwargs):
            raise httpx.ConnectError("Connection failed")
            yield  # Never reached but needed for generator syntax

        mock_client = AsyncMock()
        mock_client.send_message = error_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            result = await a2a_client_runnable.ainvoke(sub_agent_input.model_dump())

        assert isinstance(result, ErrorEvent)
        assert result.error
        assert result.error_type == "ConnectError"
        assert result.requires_retry is False

    @pytest.mark.asyncio
    async def test_ainvoke_handles_timeout_error(self, a2a_client_runnable, sub_agent_input):
        """Test ainvoke handles timeout errors gracefully."""

        # Create a custom async generator that raises on first iteration
        async def error_stream(*args, **kwargs):
            raise httpx.TimeoutException("Request timed out")
            yield  # Never reached but needed for generator syntax

        mock_client = AsyncMock()
        mock_client.send_message = error_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            result = await a2a_client_runnable.ainvoke(sub_agent_input.model_dump())

        assert isinstance(result, ErrorEvent)
        assert result.error
        assert result.error_type == "TimeoutException"
        assert result.requires_retry is False

    @pytest.mark.asyncio
    async def test_ainvoke_detects_unexpected_disconnect(self, a2a_client_runnable, sub_agent_input):
        """Test ainvoke detects when stream ends in non-terminal state."""
        working_task = Task(
            id="task-1",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.working),
        )

        mock_client = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield (working_task, None)
            # Stream ends without terminal state

        mock_client.send_message = mock_stream

        with patch.object(a2a_client_runnable, "_get_client", return_value=mock_client):
            result = await a2a_client_runnable.ainvoke(sub_agent_input.model_dump())

        assert isinstance(result, ErrorEvent)
        assert "stopped responding" in result.error


class TestA2AClientRunnableTraceHeaders:
    """Test distributed tracing header injection."""

    @pytest.mark.asyncio
    async def test_inject_trace_headers(self, a2a_client_runnable):
        """Test trace headers are injected into HTTP requests."""
        mock_request = Mock(spec=httpx.Request)
        mock_request.headers = {}

        # Mock LangSmith run tree
        mock_run_tree = Mock()
        mock_run_tree.to_headers.return_value = {
            "langsmith-trace": "trace-123",
            "baggage": "key=value",
        }

        with patch("agent_common.a2a.client_runnable.get_current_run_tree", return_value=mock_run_tree):
            await a2a_client_runnable._inject_trace_headers(mock_request)

        assert mock_request.headers["langsmith-trace"] == "trace-123"
        assert mock_request.headers["baggage"] == "key=value"

    @pytest.mark.asyncio
    async def test_inject_trace_headers_no_run_tree(self, a2a_client_runnable):
        """Test no headers injected when no active run tree."""
        mock_request = Mock(spec=httpx.Request)
        mock_request.headers = {}

        with patch("agent_common.a2a.client_runnable.get_current_run_tree", return_value=None):
            await a2a_client_runnable._inject_trace_headers(mock_request)

        # Headers should remain empty
        assert len(mock_request.headers) == 0

    @pytest.mark.asyncio
    async def test_get_client_registers_trace_hook(self, a2a_client_runnable):
        """Test that HTTP client is created with trace injection hook."""
        with patch("agent_common.a2a.client_runnable.ClientFactory") as mock_factory:
            mock_factory_instance = Mock()
            mock_factory.return_value = mock_factory_instance
            mock_factory_instance.create.return_value = Mock()

            await a2a_client_runnable._get_client()

            # Verify httpx client was created with event hooks
            assert a2a_client_runnable._http_client is not None
            assert "request" in a2a_client_runnable._http_client._event_hooks


class TestA2AClientRunnableContextManager:
    """Test async context manager functionality."""

    @pytest.mark.asyncio
    async def test_context_manager_cleans_up_http_client(self, agent_card):
        """Test context manager properly cleans up HTTP client."""
        runnable = A2AClientRunnable(agent_card=agent_card)

        async with runnable:
            # Trigger client creation
            await runnable._get_client()
            assert runnable._http_client is not None

        # HTTP client should be closed after exit
        # We can't directly test if aclose was called, but we verify it was set up

    @pytest.mark.asyncio
    async def test_context_manager_with_external_client(self, agent_card):
        """Test context manager doesn't close externally provided client."""
        external_client = httpx.AsyncClient()
        runnable = A2AClientRunnable(agent_card=agent_card, http_client=external_client)

        async with runnable:
            await runnable._get_client()
            assert runnable._http_client == external_client

        # External client should NOT be closed
        # The flag _close_http_client should be False
        assert runnable._close_http_client is False

        # Clean up external client
        await external_client.aclose()


class TestA2AClientRunnableSyncStream:
    """Test synchronous streaming (not supported)."""

    def test_stream_raises_not_implemented(self, a2a_client_runnable, sub_agent_input):
        """Test that sync stream raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="Synchronous streaming not supported"):
            a2a_client_runnable.stream(sub_agent_input.model_dump())


class TestA2AClientRunnableMessageExtraction:
    """Test message and part extraction from sub-agent input."""

    def test_extract_parts(self, a2a_client_runnable):
        """Test extracting parts from A2A Part list."""
        parts = [
            Part(root=TextPart(text="First part", metadata={"type": "greeting"})),
            Part(root=TextPart(text="Second part")),
        ]

        result = a2a_client_runnable._extract_parts(parts)

        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[0]["content"] == "First part"
        assert result[0]["metadata"]["type"] == "greeting"
        assert result[1]["content"] == "Second part"

    @pytest.mark.asyncio
    async def test_handle_message_response(self, a2a_client_runnable):
        """Test handling message response returns TaskResponseData."""
        message = Message(
            role=A2ARole.agent,
            parts=[Part(root=TextPart(text="Response message"))],
            message_id="msg-1",
            context_id="ctx-1",
            task_id="task-1",
        )

        result = await a2a_client_runnable._handle_message_response(message)

        assert result.task_id == "task-1"
        assert result.context_id == "ctx-1"
        assert len(result.messages) == 1
        assert result.messages[0].content == "Response message"
        assert result.metadata["message_id"] == "msg-1"
        assert result.metadata["role"] == str(A2ARole.agent)


class TestA2AClientRunnableTrackingIDExtraction:
    """Test conversation ID propagation with waterfall logic."""

    def test_extract_tracking_ids_with_persisted_context(self, a2a_client_runnable):
        """Test extracting tracking IDs when sub-agent has persisted context_id."""
        input_data = SubAgentInput(
            messages=[HumanMessage(content="Follow-up message")],
            a2a_tracking={
                "test-agent": {
                    "context_id": "sub-agent-ctx-456",
                    "task_id": "task-789",
                    "is_complete": False,
                }
            },
            orchestrator_conversation_id="orchestrator-conv-123",
        )

        context_id, task_id = a2a_client_runnable._extract_tracking_ids(input_data)

        # Should use persisted sub-agent context_id (not orchestrator's)
        assert context_id == "sub-agent-ctx-456"
        assert task_id == "task-789"

    def test_extract_tracking_ids_first_call_uses_orchestrator_id(self, a2a_client_runnable):
        """Test first call to sub-agent uses orchestrator's conversation_id."""
        input_data = SubAgentInput(
            messages=[HumanMessage(content="First message")],
            a2a_tracking={},  # No tracking yet
            orchestrator_conversation_id="orchestrator-conv-123",
        )

        context_id, task_id = a2a_client_runnable._extract_tracking_ids(input_data)

        # Should use orchestrator's conversation_id for first call
        assert context_id == "orchestrator-conv-123"
        assert task_id is None

    def test_extract_tracking_ids_no_tracking_no_orchestrator_id(self, a2a_client_runnable):
        """Test extraction with no tracking and no orchestrator ID."""
        input_data = SubAgentInput(
            messages=[HumanMessage(content="Message")],
            a2a_tracking={},
            orchestrator_conversation_id=None,
        )

        context_id, task_id = a2a_client_runnable._extract_tracking_ids(input_data)

        # Should return None for both
        assert context_id is None
        assert task_id is None

    def test_extract_tracking_ids_completed_task_clears_task_id(self, a2a_client_runnable):
        """Test that completed task_id is cleared but context_id preserved."""
        input_data = SubAgentInput(
            messages=[HumanMessage(content="Message")],
            a2a_tracking={
                "test-agent": {
                    "context_id": "sub-ctx-456",
                    "task_id": "task-789",
                    "is_complete": True,  # Task completed
                }
            },
            orchestrator_conversation_id="orchestrator-conv-123",
        )

        context_id, task_id = a2a_client_runnable._extract_tracking_ids(input_data)

        # Should preserve context_id but clear completed task_id
        assert context_id == "sub-ctx-456"
        assert task_id is None

    def test_extract_tracking_ids_waterfall_preference(self, a2a_client_runnable):
        """Test waterfall: persisted context_id takes precedence over orchestrator's."""
        input_data = SubAgentInput(
            messages=[HumanMessage(content="Message")],
            a2a_tracking={
                "test-agent": {
                    "context_id": "sub-ctx-456",  # Persisted ID
                    "is_complete": True,
                }
            },
            orchestrator_conversation_id="orchestrator-conv-123",  # Orchestrator ID
        )

        context_id, task_id = a2a_client_runnable._extract_tracking_ids(input_data)

        # Should prefer persisted sub-agent context_id
        assert context_id == "sub-ctx-456"
        assert task_id is None

    def test_extract_tracking_ids_multi_turn_conversation(self, a2a_client_runnable):
        """Test multi-turn conversation uses same context_id."""
        # First call: no tracking, uses orchestrator ID
        input_data_first = SubAgentInput(
            messages=[HumanMessage(content="First message")],
            a2a_tracking={},
            orchestrator_conversation_id="orchestrator-conv-123",
        )

        context_id_first, _ = a2a_client_runnable._extract_tracking_ids(input_data_first)
        assert context_id_first == "orchestrator-conv-123"

        # Second call: has tracking from first call (simulating orchestrator storing response)
        input_data_second = SubAgentInput(
            messages=[HumanMessage(content="Follow-up message")],
            a2a_tracking={
                "test-agent": {
                    "context_id": "orchestrator-conv-123",  # Sub-agent returned orchestrator's ID
                    "task_id": "task-789",
                    "is_complete": False,
                }
            },
            orchestrator_conversation_id="orchestrator-conv-123",
        )

        context_id_second, task_id_second = a2a_client_runnable._extract_tracking_ids(input_data_second)

        # Should continue with same conversation_id
        assert context_id_second == "orchestrator-conv-123"
        assert task_id_second == "task-789"
        # Verify unified conversation tracking
        assert context_id_first == context_id_second
