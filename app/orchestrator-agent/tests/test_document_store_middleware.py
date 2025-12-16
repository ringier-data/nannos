"""Tests for DocumentStoreMiddleware with tool result indexing."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.middleware.document_store import DocumentStoreMiddleware


@pytest.fixture
def mock_agent_settings():
    """Mock agent settings."""
    return Mock()


@pytest.fixture
def middleware(mock_agent_settings):
    """Create DocumentStoreMiddleware instance."""
    return DocumentStoreMiddleware(mock_agent_settings)


@pytest.fixture
def mock_runtime():
    """Mock runtime with store."""
    runtime = Mock()
    runtime.store = AsyncMock()
    runtime.config = {
        "metadata": {
            "user_id": "test-user-id",
            "assistant_id": "test-assistant-id",
        }
    }
    return runtime


@pytest.mark.asyncio
async def test_detects_large_tool_result_eviction(middleware, mock_runtime):
    """Test that middleware detects and indexes evicted tool results."""
    # Create a Command that simulates FilesystemMiddleware eviction
    tool_call_id = "test-tool-call-123"
    file_path = f"/large_tool_results/{tool_call_id}"

    file_data = {
        "content": [
            "This is a large tool result that was evicted.",
            "It contains multiple lines of content.",
            "The content should be indexed for RAG retrieval.",
        ],
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    eviction_command = Command(
        update={
            "files": {file_path: file_data},
            "messages": [ToolMessage(content="Tool result too large, saved to file", tool_call_id=tool_call_id)],
        }
    )

    # Mock handler that returns the eviction command
    handler = AsyncMock(return_value=eviction_command)

    # Mock tool call request
    request = Mock(spec=ToolCallRequest)
    request.tool_call = {"name": "some_tool", "args": {}}
    request.runtime = mock_runtime

    # Mock the indexing method to verify it's called
    with patch.object(middleware, "_index_evicted_tool_result", new_callable=AsyncMock) as mock_index:
        result = await middleware.awrap_tool_call(request, handler)

        # Verify the handler was called
        handler.assert_called_once_with(request)

        # Verify the result is returned unchanged
        assert result == eviction_command

        # Verify indexing was triggered
        mock_index.assert_called_once()
        call_kwargs = mock_index.call_args[1]
        assert call_kwargs["file_path"] == file_path
        assert call_kwargs["file_data"] == file_data
        assert call_kwargs["tool_call_id"] == tool_call_id
        assert call_kwargs["runtime"] == mock_runtime


@pytest.mark.asyncio
async def test_ignores_non_evicted_commands(middleware, mock_runtime):
    """Test that middleware ignores Commands without evicted tool results."""
    # Create a Command without /large_tool_results/*
    normal_command = Command(
        update={
            "files": {"/normal/file.txt": {"content": ["normal content"]}},
        }
    )

    handler = AsyncMock(return_value=normal_command)
    request = Mock(spec=ToolCallRequest)
    request.tool_call = {"name": "write_file", "args": {"file_path": "/normal/file.txt"}}
    request.runtime = mock_runtime

    with patch.object(middleware, "_index_evicted_tool_result", new_callable=AsyncMock) as mock_index:
        result = await middleware.awrap_tool_call(request, handler)

        # Verify indexing was NOT triggered for non-evicted files
        mock_index.assert_not_called()


@pytest.mark.asyncio
async def test_index_evicted_tool_result_full_flow(middleware, mock_runtime):
    """Test the full indexing flow for evicted tool results."""
    file_path = "/large_tool_results/test-tool-123"
    tool_call_id = "test-tool-123"

    file_data = {
        "content": [
            "Line 1: This is test content",
            "Line 2: With multiple lines",
            "Line 3: To verify chunking works",
        ],
    }

    # Mock the model and chunking
    with (
        patch("app.middleware.document_store.create_model") as mock_create_model,
        patch("app.middleware.document_store.chunk_with_context", new_callable=AsyncMock) as mock_chunk,
    ):
        # Setup mocks
        mock_model = Mock()
        mock_create_model.return_value = mock_model

        # Mock chunk results
        mock_chunks = [
            ("Chunk 1 content", "Context for chunk 1", [0.1, 0.2, 0.3]),
            ("Chunk 2 content", "Context for chunk 2", [0.4, 0.5, 0.6]),
        ]
        mock_chunk.return_value = mock_chunks

        # Call the indexing method
        await middleware._index_evicted_tool_result(
            file_path=file_path,
            file_data=file_data,
            tool_call_id=tool_call_id,
            runtime=mock_runtime,
        )

        # Verify model was created
        mock_create_model.assert_called_once_with("claude-sonnet-4.5", middleware.agent_settings, thinking=False)

        # Verify chunking was called with correct metadata
        mock_chunk.assert_called_once()
        call_args = mock_chunk.call_args[0]
        assert (
            call_args[0]
            == "Line 1: This is test content\nLine 2: With multiple lines\nLine 3: To verify chunking works"
        )
        metadata = call_args[1]
        assert metadata["source"] == "tool_result"
        assert metadata["tool_call_id"] == tool_call_id
        assert metadata["file_path"] == file_path
        assert "original_size" in metadata
        assert "evicted_at" in metadata

        # Verify store operations
        # Should store: 1) file in filesystem namespace, 2) file index, 3) 2 chunks
        assert mock_runtime.store.aput.call_count == 4

        # Verify file was stored in user filesystem namespace
        file_store_call = mock_runtime.store.aput.call_args_list[0]
        assert file_store_call[1]["namespace"] == ("test-user-id", "filesystem")
        assert file_store_call[1]["key"] == file_path
        assert file_store_call[1]["value"] == file_data

        # Verify file index was stored
        index_store_call = mock_runtime.store.aput.call_args_list[1]
        assert index_store_call[1]["namespace"] == ("test-user-id", "documents")
        assert index_store_call[1]["key"] == file_path
        index_value = index_store_call[1]["value"]
        assert index_value["total_chunks"] == 2
        assert index_value["metadata"]["source"] == "tool_result"

        # Verify chunks were stored with embeddings
        chunk1_call = mock_runtime.store.aput.call_args_list[2]
        assert chunk1_call[1]["namespace"] == ("test-user-id", "documents")
        assert chunk1_call[1]["key"] == f"{file_path}#chunk_0"
        assert chunk1_call[1]["value"]["content"] == "Chunk 1 content"
        assert chunk1_call[1]["index"] == [0.1, 0.2, 0.3]

        chunk2_call = mock_runtime.store.aput.call_args_list[3]
        assert chunk2_call[1]["key"] == f"{file_path}#chunk_1"
        assert chunk2_call[1]["value"]["content"] == "Chunk 2 content"


@pytest.mark.asyncio
async def test_handles_multiple_evicted_files_in_one_command(middleware, mock_runtime):
    """Test handling multiple evicted tool results in a single Command."""
    file1 = "/large_tool_results/tool-call-1"
    file2 = "/large_tool_results/tool-call-2"

    eviction_command = Command(
        update={
            "files": {
                file1: {"content": ["Content 1"]},
                file2: {"content": ["Content 2"]},
            }
        }
    )

    handler = AsyncMock(return_value=eviction_command)
    request = Mock(spec=ToolCallRequest)
    request.tool_call = {"name": "some_tool", "args": {}}
    request.runtime = mock_runtime

    with patch.object(middleware, "_index_evicted_tool_result", new_callable=AsyncMock) as mock_index:
        await middleware.awrap_tool_call(request, handler)

        # Verify indexing was called twice
        assert mock_index.call_count == 2


@pytest.mark.asyncio
async def test_handles_empty_content_gracefully(middleware, mock_runtime):
    """Test that empty content is handled without errors."""
    file_path = "/large_tool_results/test-123"
    file_data = {"content": []}  # Empty content

    # Should not raise an exception
    await middleware._index_evicted_tool_result(
        file_path=file_path,
        file_data=file_data,
        tool_call_id="test-123",
        runtime=mock_runtime,
    )

    # Verify no store operations were performed
    mock_runtime.store.aput.assert_not_called()


@pytest.mark.asyncio
async def test_handles_missing_runtime_gracefully(middleware):
    """Test that missing runtime is handled without errors."""
    file_path = "/large_tool_results/test-123"
    file_data = {"content": ["test"]}

    # Should not raise an exception
    await middleware._index_evicted_tool_result(
        file_path=file_path,
        file_data=file_data,
        tool_call_id="test-123",
        runtime=None,
    )
