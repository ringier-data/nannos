"""Tests for the semantic_search_file tool (JIT index + scoped search).

Covers:
- _resolve_namespaces_for_path routing for tool results / channel / personal
- _semantic_search_file_impl reads the in-hand file, JIT-indexes it, and searches
  the chunk namespace filtered to that file
- Missing scoping context and missing file raise ToolException
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import ToolException

from agent_common.core.document_store_tools import (
    _resolve_namespaces_for_path,
    _semantic_search_file_impl,
)


class TestResolveNamespacesForPath:
    def test_large_tool_results_routes_to_conversation(self):
        result = _resolve_namespaces_for_path("/large_tool_results/tool-1", "user-1", {"conversation_id": "conv-1"})
        assert result == (("conv-1", "filesystem"), ("conv-1", "tool_results"))

    def test_large_tool_results_missing_conversation_returns_none(self):
        result = _resolve_namespaces_for_path("/large_tool_results/tool-1", "user-1", {})
        assert result is None

    def test_channel_memories_routes_to_assistant(self):
        result = _resolve_namespaces_for_path("/channel_memories/notes.txt", "user-1", {"assistant_id": "asst-1"})
        assert result == (("asst-1", "filesystem"), ("asst-1", "documents"))

    def test_channel_memories_missing_assistant_returns_none(self):
        result = _resolve_namespaces_for_path("/channel_memories/notes.txt", "user-1", {})
        assert result is None

    def test_personal_memories_routes_to_user(self):
        result = _resolve_namespaces_for_path("/memories/notes.txt", "user-1", {})
        assert result == (("user-1", "filesystem"), ("user-1", "documents"))

    def test_attachments_routes_to_conversation_tool_results(self):
        result = _resolve_namespaces_for_path("/attachments/file.txt", "user-1", {"conversation_id": "conv-1"})
        assert result == (("conv-1", "filesystem"), ("conv-1", "tool_results"))

    def test_attachments_missing_conversation_returns_none(self):
        result = _resolve_namespaces_for_path("/attachments/file.txt", "user-1", {})
        assert result is None


def _make_store_item(content):
    item = MagicMock()
    item.value = {"content": content}
    return item


def _make_chunk(file_path, content, chunk_index=0, score=0.9):
    chunk = MagicMock()
    chunk.value = {
        "file_path": file_path,
        "content": content,
        "chunk_index": chunk_index,
        "total_chunks": 1,
        "context_description": "ctx",
    }
    chunk.score = score
    return chunk


class TestSemanticSearchFileImpl:
    @pytest.mark.asyncio
    async def test_reads_indexes_and_searches_scoped_to_file(self):
        store = AsyncMock()
        store.aget.return_value = _make_store_item(["line one", "line two"])
        # Chunk namespace contains chunks from this file AND another file
        store.asearch.return_value = [
            _make_chunk("/large_tool_results/tool-1", "relevant chunk", score=0.95),
            _make_chunk("/large_tool_results/other", "irrelevant", score=0.99),
        ]

        with patch(
            "agent_common.core.document_store_tools.get_config",
            return_value={"metadata": {"conversation_id": "conv-1"}},
        ):
            with patch("agent_common.core.document_store_tools.IndexingStoreBackend") as MockBackend:
                backend_instance = MockBackend.return_value
                backend_instance.aensure_indexed = AsyncMock()

                result = await _semantic_search_file_impl(
                    file_path="/large_tool_results/tool-1",
                    query="find it",
                    top_k=5,
                    user_id="user-1",
                    store=store,
                    model_name="us-east-1",
                    cost_logger=None,
                )

        # Read from the filesystem namespace
        store.aget.assert_awaited_once()
        assert store.aget.await_args.kwargs["namespace"] == ("conv-1", "filesystem")
        # JIT indexing invoked
        backend_instance.aensure_indexed.assert_awaited_once_with("/large_tool_results/tool-1", "line one\nline two")
        # Searched the chunk namespace
        assert store.asearch.await_args.args[0] == ("conv-1", "tool_results")
        # Only this file's chunk is returned
        assert len(result) == 1
        assert result[0]["page_content"] == "relevant chunk"
        assert result[0]["metadata"]["file_path"] == "/large_tool_results/tool-1"

    @pytest.mark.asyncio
    async def test_missing_scope_raises(self):
        store = AsyncMock()
        with patch(
            "agent_common.core.document_store_tools.get_config",
            return_value={"metadata": {}},  # no conversation_id
        ):
            with pytest.raises(ToolException):
                await _semantic_search_file_impl(
                    file_path="/large_tool_results/tool-1",
                    query="q",
                    top_k=5,
                    user_id="user-1",
                    store=store,
                    model_name=None,
                    cost_logger=None,
                )

    @pytest.mark.asyncio
    async def test_missing_file_raises(self):
        store = AsyncMock()
        store.aget.return_value = None
        with patch(
            "agent_common.core.document_store_tools.get_config",
            return_value={"metadata": {"conversation_id": "conv-1"}},
        ):
            with pytest.raises(ToolException):
                await _semantic_search_file_impl(
                    file_path="/large_tool_results/tool-1",
                    query="q",
                    top_k=5,
                    user_id="user-1",
                    store=store,
                    model_name=None,
                    cost_logger=None,
                )

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_without_indexing(self):
        store = AsyncMock()
        store.aget.return_value = _make_store_item(["", "   "])
        with patch(
            "agent_common.core.document_store_tools.get_config",
            return_value={"metadata": {"conversation_id": "conv-1"}},
        ):
            with patch("agent_common.core.document_store_tools.IndexingStoreBackend") as MockBackend:
                result = await _semantic_search_file_impl(
                    file_path="/large_tool_results/tool-1",
                    query="q",
                    top_k=5,
                    user_id="user-1",
                    store=store,
                    model_name=None,
                    cost_logger=None,
                )

        assert result == []
        MockBackend.assert_not_called()
        store.asearch.assert_not_called()


class TestSemanticSearchFileAttachments:
    """Attachments are read from the per-turn in-memory backend, not PostgreSQL."""

    @pytest.mark.asyncio
    async def test_reads_attachment_from_contextvar_backend(self):
        from agent_common.backends.attachments_store import (
            Attachment,
            AttachmentsStoreBackend,
            reset_current_attachments_backend,
            set_current_attachments_backend,
        )

        store = AsyncMock()
        # store.aget must NOT be used for attachments
        store.aget.return_value = None
        store.asearch.return_value = [
            _make_chunk("/attachments/doc.txt", "matching attachment chunk", score=0.95),
        ]

        attachment = Attachment(filename="doc.txt", mime_type="text/plain", inline_bytes=b"alpha beta gamma")
        backend = AttachmentsStoreBackend([attachment])
        token = set_current_attachments_backend(backend)
        try:
            with patch(
                "agent_common.core.document_store_tools.get_config",
                return_value={"metadata": {"conversation_id": "conv-1"}},
            ):
                with patch("agent_common.core.document_store_tools.IndexingStoreBackend") as MockBackend:
                    backend_instance = MockBackend.return_value
                    backend_instance.aensure_indexed = AsyncMock()

                    result = await _semantic_search_file_impl(
                        file_path="/attachments/doc.txt",
                        query="find it",
                        top_k=5,
                        user_id="user-1",
                        store=store,
                        model_name="us-east-1",
                        cost_logger=None,
                    )
        finally:
            reset_current_attachments_backend(token)

        # Content came from the attachments backend, not the store
        store.aget.assert_not_awaited()
        backend_instance.aensure_indexed.assert_awaited_once_with("/attachments/doc.txt", "alpha beta gamma")
        # Searched the conversation-scoped tool_results namespace
        assert store.asearch.await_args.args[0] == ("conv-1", "tool_results")
        assert len(result) == 1
        assert result[0]["page_content"] == "matching attachment chunk"

    @pytest.mark.asyncio
    async def test_attachment_not_registered_raises(self):
        store = AsyncMock()
        with patch(
            "agent_common.core.document_store_tools.get_config",
            return_value={"metadata": {"conversation_id": "conv-1"}},
        ):
            with pytest.raises(ToolException):
                await _semantic_search_file_impl(
                    file_path="/attachments/missing.txt",
                    query="q",
                    top_k=5,
                    user_id="user-1",
                    store=store,
                    model_name=None,
                    cost_logger=None,
                )
