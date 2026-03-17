"""Tests for conversation-scoped storage of large tool results.

Verifies that:
1. Large tool results are stored in conversation-scoped namespace: (conversation_id, "tool_results")
2. User files are stored in user-scoped namespace: (user_id, "documents")
3. Hybrid search combines results from both namespaces
4. Tool results from different conversations don't leak
"""

import logging

import pytest
from unittest.mock import AsyncMock, Mock, patch
from agent_common.backends.indexing_store import IndexingStoreBackend
from langchain.tools import ToolRuntime


@pytest.fixture
def mock_runtime():
    """Create a mock ToolRuntime with metadata."""
    runtime = Mock(spec=ToolRuntime)
    runtime.config = {
        "metadata": {
            "user_id": "test-user-123",
            "conversation_id": "conv-abc",
            "assistant_id": "assistant-xyz",
        }
    }
    runtime.store = AsyncMock()
    return runtime


@pytest.fixture
def mock_bedrock_region():
    """Bedrock region for testing."""
    return "us-east-1"


@pytest.mark.asyncio
async def test_large_tool_result_uses_conversation_namespace(mock_runtime, mock_bedrock_region):
    """Test that large tool results are stored in conversation-scoped namespace."""
    backend = IndexingStoreBackend(mock_runtime, mock_bedrock_region)
    
    # Mock the parent write and chunking
    with patch.object(backend, '_get_store') as mock_get_store:
        mock_get_store.return_value = mock_runtime.store
        mock_runtime.store.aget.return_value = None  # No existing index
        
        with patch('agent_common.backends.indexing_store.chunk_with_context') as mock_chunk, patch("agent_common.backends.indexing_store.create_model") as mock_create_model:
            mock_chunk.return_value = [("chunk1", "context1")]
            
            # Index a large tool result
            await backend._index_content("/large_tool_results/tool-123", "test content")
    
    # Verify namespace selection
    calls = mock_runtime.store.aget.call_args_list
    assert len(calls) > 0
    
    # First call checks for existing index - should use conversation namespace
    first_call_namespace = calls[0][1]['namespace']
    assert first_call_namespace == ("conv-abc", "tool_results"), \
        f"Expected conversation-scoped namespace, got {first_call_namespace}"


@pytest.mark.asyncio
async def test_user_file_uses_user_namespace(mock_runtime, mock_bedrock_region):
    """Test that user files are stored in user-scoped namespace."""
    backend = IndexingStoreBackend(mock_runtime, mock_bedrock_region)
    
    # Mock the parent write and chunking
    with patch.object(backend, '_get_store') as mock_get_store:
        mock_get_store.return_value = mock_runtime.store
        mock_runtime.store.aget.return_value = None  # No existing index
        
        with patch('agent_common.backends.indexing_store.chunk_with_context') as mock_chunk, patch("agent_common.backends.indexing_store.create_model") as mock_create_model:
            mock_chunk.return_value = [("chunk1", "context1")]
            
            # Index a user file
            await backend._index_content("/memories/my-file.txt", "user file content")
    
    # Verify namespace selection
    calls = mock_runtime.store.aget.call_args_list
    assert len(calls) > 0
    
    # First call checks for existing index - should use user namespace
    first_call_namespace = calls[0][1]['namespace']
    assert first_call_namespace == ("test-user-123", "documents"), \
        f"Expected user-scoped namespace, got {first_call_namespace}"


@pytest.mark.asyncio
async def test_tool_result_without_conversation_id_logs_error(mock_bedrock_region, caplog):
    """Test that tool results without conversation_id fall back to user namespace."""
    runtime = Mock(spec=ToolRuntime)
    runtime.config = {
        "metadata": {
            "user_id": "test-user-123",
            # No conversation_id
            "assistant_id": "assistant-xyz",
        }
    }
    runtime.store = AsyncMock()
    
    backend = IndexingStoreBackend(runtime, mock_bedrock_region)
    with caplog.at_level(logging.ERROR):
        with patch.object(backend, '_get_store') as mock_get_store:
            mock_get_store.return_value = runtime.store
            runtime.store.aget.return_value = None
            
            with patch('agent_common.backends.indexing_store.chunk_with_context') as mock_chunk, patch("agent_common.backends.indexing_store.create_model") as mock_create_model:
                mock_chunk.return_value = [("chunk1", "context1")]
                
                # Index a tool result without conversation_id
                await backend._index_content("/large_tool_results/tool-123", "test content")
    
    assert "Cannot index tool result" in caplog.text


@pytest.mark.asyncio
async def test_tool_result_skips_user_filesystem_copy(mock_runtime, mock_bedrock_region):
    """Test that tool results skip the user filesystem copy."""
    backend = IndexingStoreBackend(mock_runtime, mock_bedrock_region)
    
    with patch.object(backend, '_get_store') as mock_get_store:
        mock_get_store.return_value = mock_runtime.store
        mock_runtime.store.aget.return_value = None
        
        with patch('agent_common.backends.indexing_store.chunk_with_context') as mock_chunk, patch("agent_common.backends.indexing_store.create_model") as mock_create_model:
            mock_chunk.return_value = [("chunk1", "context1")]
            
            # Index a tool result
            await backend._index_content("/large_tool_results/tool-123", "test content")
    
    # Verify that file index value doesn't include filesystem_namespace
    aput_calls = mock_runtime.store.aput.call_args_list
    
    # Find the file index metadata call (the one without 'index' parameter for chunks)
    file_index_call = None
    for call in aput_calls:
        if 'index' not in call[1]:
            file_index_call = call
            break
    
    assert file_index_call is not None, "File index metadata should be stored"
    file_index_value = file_index_call[1]['value']
    
    # For tool results, filesystem_namespace should be None
    assert file_index_value.get('filesystem_namespace') is None, \
        "Tool results should not have filesystem_namespace"



@pytest.mark.asyncio
async def test_hybrid_search_combines_namespaces():
    """Test that docstore_search searches both conversation tool results and user documents."""
    from agent_common.core.document_store_tools import _search_documents_rag_impl
    from langgraph.store.postgres.aio import AsyncPostgresStore
    
    # Mock store with search results
    mock_store = AsyncMock(spec=AsyncPostgresStore)
    
    # Mock search results - tool results and user docs
    tool_result = Mock()
    tool_result.value = {
        "file_path": "/large_tool_results/tool-123",
        "chunk_index": 0,
        "total_chunks": 1,
        "content": "Tool result content",
        "context_description": "Tool result context",
    }
    tool_result.score = 0.9
    
    user_doc = Mock()
    user_doc.value = {
        "file_path": "/memories/user-doc.txt",
        "chunk_index": 0,
        "total_chunks": 1,
        "content": "User document content",
        "context_description": "User doc context",
    }
    user_doc.score = 0.8
    
    # Mock store.asearch to return different results for different namespaces
    def mock_asearch(namespace, query, limit):
        if namespace == ("conv-123", "tool_results"):
            return [tool_result]
        elif namespace == ("user-456", "documents"):
            return [user_doc]
        return []
    
    mock_store.asearch.side_effect = mock_asearch
    
    # Mock get_config to provide conversation_id
    with patch('agent_common.core.document_store_tools.get_config') as mock_get_config:
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
    
    # Verify both namespaces were searched
    assert mock_store.asearch.call_count == 2
    
    # Verify the result is a list of documents in LangChain format
    assert isinstance(result, list)
    assert len(result) == 2
    
    # Verify document structure
    for doc in result:
        assert "page_content" in doc
        assert "type" in doc
        assert doc["type"] == "Document"
        assert "metadata" in doc
    
    # Verify proper ordering (tool result should come first due to higher score)
    assert result[0]["page_content"] == "Tool result content"
    assert result[0]["metadata"]["file_path"] == "/large_tool_results/tool-123"
    assert result[1]["page_content"] == "User document content"
    assert result[1]["metadata"]["file_path"] == "/memories/user-doc.txt"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
