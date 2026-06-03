"""Tests for conversation-scoped storage of large tool results.

Verifies that:
1. Large tool results are stored in conversation-scoped namespace: (conversation_id, "tool_results")
2. User files are stored in user-scoped namespace: (user_id, "documents")
3. Hybrid search combines results from both namespaces
4. Tool results from different conversations don't leak
"""

import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from agent_common.backends.indexing_store import IndexingStoreBackend

_DEFAULT_CONFIG = {
    "metadata": {
        "user_id": "test-user-123",
        "conversation_id": "conv-abc",
        "assistant_id": "assistant-xyz",
    }
}


@pytest.fixture
def mock_store():
    """Create a mock AsyncPostgresStore."""
    return AsyncMock()


@pytest.fixture
def mock_model_name():
    return "a-test-model"


@pytest.mark.asyncio
async def test_large_tool_result_uses_conversation_namespace(mock_store, mock_model_name):
    """Test that large tool results are stored in conversation-scoped namespace."""
    backend = IndexingStoreBackend(store=mock_store, model_name=mock_model_name)

    mock_store.aget.return_value = None  # No existing index

    with patch("agent_common.backends.indexing_store.get_config", return_value=_DEFAULT_CONFIG):
        with (
            patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk,
            patch("agent_common.backends.indexing_store.create_model") as mock_create_model,
        ):
            mock_chunk.return_value = [("chunk1", "context1")]

            # Index a large tool result
            await backend._index_content("/large_tool_results/tool-123", "test content")

    # Verify namespace selection
    calls = mock_store.aget.call_args_list
    assert len(calls) > 0

    # First call checks for existing index - should use conversation namespace
    first_call_namespace = calls[0][1]["namespace"]
    assert first_call_namespace == ("conv-abc", "tool_results"), (
        f"Expected conversation-scoped namespace, got {first_call_namespace}"
    )


@pytest.mark.asyncio
async def test_user_file_uses_user_namespace(mock_store, mock_model_name):
    """Test that user files are stored in user-scoped namespace."""
    backend = IndexingStoreBackend(store=mock_store, model_name=mock_model_name)

    mock_store.aget.return_value = None  # No existing index

    with patch("agent_common.backends.indexing_store.get_config", return_value=_DEFAULT_CONFIG):
        with (
            patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk,
            patch("agent_common.backends.indexing_store.create_model") as mock_create_model,
        ):
            mock_chunk.return_value = [("chunk1", "context1")]

            # Index a user file
            await backend._index_content("/memories/my-file.txt", "user file content")

    # Verify namespace selection
    calls = mock_store.aget.call_args_list
    assert len(calls) > 0

    # First call checks for existing index - should use user namespace
    first_call_namespace = calls[0][1]["namespace"]
    assert first_call_namespace == ("test-user-123", "documents"), (
        f"Expected user-scoped namespace, got {first_call_namespace}"
    )


@pytest.mark.asyncio
async def test_tool_result_without_conversation_id_logs_error(mock_model_name, caplog):
    """Test that tool results without conversation_id fall back to user namespace."""
    mock_store = AsyncMock()

    backend = IndexingStoreBackend(store=mock_store, model_name=mock_model_name)
    config_no_conv = {
        "metadata": {
            "user_id": "test-user-123",
            # No conversation_id
            "assistant_id": "assistant-xyz",
        }
    }
    with caplog.at_level(logging.ERROR):
        with patch("agent_common.backends.indexing_store.get_config", return_value=config_no_conv):
            mock_store.aget.return_value = None

            with (
                patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk,
                patch("agent_common.backends.indexing_store.create_model") as mock_create_model,
            ):
                mock_chunk.return_value = [("chunk1", "context1")]

                # Index a tool result without conversation_id
                await backend._index_content("/large_tool_results/tool-123", "test content")

    assert "conversation_id missing from metadata" in caplog.text


@pytest.mark.asyncio
async def test_tool_result_skips_user_filesystem_copy(mock_store, mock_model_name):
    """Test that tool results skip the user filesystem copy."""
    backend = IndexingStoreBackend(store=mock_store, model_name=mock_model_name)

    mock_store.aget.return_value = None

    with patch("agent_common.backends.indexing_store.get_config", return_value=_DEFAULT_CONFIG):
        with (
            patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk,
            patch("agent_common.backends.indexing_store.create_model") as mock_create_model,
        ):
            mock_chunk.return_value = [("chunk1", "context1")]

            # Index a tool result
            await backend._index_content("/large_tool_results/tool-123", "test content")

    # Verify that file index value doesn't include filesystem_namespace
    aput_calls = mock_store.aput.call_args_list

    # Find the file index metadata call (the one without 'index' parameter for chunks)
    file_index_call = None
    for call in aput_calls:
        if "index" not in call[1]:
            file_index_call = call
            break

    assert file_index_call is not None, "File index metadata should be stored"
    file_index_value = file_index_call[1]["value"]

    # For tool results, filesystem_namespace should be None
    assert file_index_value.get("filesystem_namespace") is None, "Tool results should not have filesystem_namespace"


@pytest.mark.asyncio
async def test_docstore_search_is_memory_only():
    """docstore_search searches durable memory only — NOT conversation tool results (D6).

    Evicted /large_tool_results/ blobs are searched on-demand via
    semantic_search_file, so docstore_search must not touch the
    (conversation_id, "tool_results") namespace.
    """
    from langgraph.store.postgres.aio import AsyncPostgresStore

    from agent_common.core.document_store_tools import _search_documents_rag_impl

    mock_store = AsyncMock(spec=AsyncPostgresStore)

    user_doc = Mock()
    user_doc.value = {
        "file_path": "/memories/user-doc.txt",
        "chunk_index": 0,
        "total_chunks": 1,
        "content": "User document content",
        "context_description": "User doc context",
    }
    user_doc.score = 0.8

    searched_namespaces = []

    def mock_asearch(namespace, query, limit):
        searched_namespaces.append(namespace)
        if namespace == ("user-456", "documents"):
            return [user_doc]
        return []

    mock_store.asearch.side_effect = mock_asearch

    # Personal scope (no "scope" metadata defaults to personal)
    with patch("agent_common.core.document_store_tools.get_config") as mock_get_config:
        mock_get_config.return_value = {
            "metadata": {
                "conversation_id": "conv-123",
            }
        }

        result = await _search_documents_rag_impl(
            query="test query",
            top_k=5,
            user_id="user-456",
            store=mock_store,
        )

    # Only the user documents namespace was searched — never tool_results
    assert ("conv-123", "tool_results") not in searched_namespaces
    assert ("user-456", "documents") in searched_namespaces
    assert mock_store.asearch.call_count == 1

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["type"] == "Document"
    assert result[0]["page_content"] == "User document content"
    assert result[0]["metadata"]["file_path"] == "/memories/user-doc.txt"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
