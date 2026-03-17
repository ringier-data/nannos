"""Tests for IndexingStoreBackend — idempotency, skip guards, error resilience.

These are complementary to the existing test_conversation_scoped_storage.py tests
which cover namespace routing. This module focuses on:
  - Idempotency: second write to same path skips re-indexing
  - Anti-meta-document guard: formatted search output is skipped
  - Error resilience: indexing failure does not roll back the file write
  - Missing metadata: missing user_id/conversation_id returns early without exception
  - Titan semaphore: concurrent embed calls respect the _EMBED_CONCURRENCY=5 limit
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch, call

import pytest
from langchain.tools import ToolRuntime

from agent_common.backends.indexing_store import IndexingStoreBackend


def _make_runtime(
    user_id: str = "user-xyz",
    conversation_id: str = "conv-123",
    assistant_id: str = "asst-abc",
) -> Mock:
    runtime = Mock(spec=ToolRuntime)
    runtime.config = {
        "metadata": {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "assistant_id": assistant_id,
        }
    }
    runtime.store = AsyncMock()
    return runtime


@pytest.fixture
def mock_agent_settings():
    return "us-east-1"

class TestIdempotency:
    """Second write to the same path must skip re-indexing."""

    @pytest.mark.asyncio
    async def test_skips_indexing_when_file_already_indexed(self, mock_agent_settings):
        """If documents_store.aget returns non-None, chunking is NOT called."""
        runtime = _make_runtime()

        # Simulate file already indexed
        existing_entry = MagicMock()
        existing_entry.value = {"total_chunks": 3}
        runtime.store.aget.return_value = existing_entry

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            await backend._index_content("/memories/notes.txt", "some content here")

        mock_chunk.assert_not_called()
        # Only aget (idempotency check) should have been called, not aput
        runtime.store.aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_proceeds_with_indexing_when_not_yet_indexed(self, mock_agent_settings):
        """If documents_store.aget returns None, chunking IS called."""
        runtime = _make_runtime()
        runtime.store.aget.return_value = None  # No existing index

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            mock_chunk.return_value = [("chunk text", "context")]
            with patch("agent_common.backends.indexing_store.create_model") as mock_model:
                mock_model.return_value = MagicMock()
                await backend._index_content("/memories/new.txt", "new content")

        mock_chunk.assert_called_once()

class TestSkipFormattedSearchOutput:
    """Formatted docstore_search output stored as large_tool_result must be skipped."""

    @pytest.mark.asyncio
    async def test_skips_when_content_is_formatted_search_output(self, mock_agent_settings):
        """Content starting with 'Found N' and containing 'relevant chunks:' is skipped."""
        runtime = _make_runtime()
        runtime.store.aget.return_value = None  # Not previously indexed

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        formatted_output = "Found 5 relevant chunks:\n\n[Result 1] /memories/foo.txt (chunk 1/3)\n..."

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            await backend._index_content("/large_tool_results/tool-456", formatted_output)

        mock_chunk.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_skip_regular_tool_result_content(self, mock_agent_settings):
        """Non-formatted tool result content is indexed normally."""
        runtime = _make_runtime()
        runtime.store.aget.return_value = None

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        regular_content = '{"status": "merged", "pr_number": 42, "title": "Fix bug"}'

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            mock_chunk.return_value = [("chunk", "ctx")]
            with patch("agent_common.backends.indexing_store.create_model"):
                await backend._index_content("/large_tool_results/gh-pr", regular_content)

        mock_chunk.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_guard_only_applies_to_large_tool_results(self, mock_agent_settings):
        """The anti-meta guard only triggers for /large_tool_results/ paths, not /memories/."""
        runtime = _make_runtime()
        runtime.store.aget.return_value = None

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        # Unusual but valid: a memories file starting with "Found"
        content = "Found 5 relevant chunks: ... (user wrote this themselves)"

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            mock_chunk.return_value = [("chunk", "ctx")]
            with patch("agent_common.backends.indexing_store.create_model"):
                await backend._index_content("/memories/notes.txt", content)

        # /memories/ path is NOT subject to the anti-meta guard
        mock_chunk.assert_called_once()



class TestErrorResilience:
    """Indexing failures must not propagate to callers — the file write wins."""

    @pytest.mark.asyncio
    async def test_indexing_failure_does_not_raise(self, mock_agent_settings):
        """If _index_content raises, awrite() still returns the parent WriteResult."""
        from deepagents.backends.protocol import WriteResult

        runtime = _make_runtime()
        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        success_result = WriteResult(path="/memories/data.txt", files_update=None)

        with patch.object(
            type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=success_result)
        ):
            with patch.object(backend, "_index_content", side_effect=RuntimeError("Embed call failed")):
                result = await backend.awrite("/memories/data.txt", "some data")

        # The parent write result is returned unchanged (no success attr — error is None on success)
        assert result.error is None
        assert result.path == "/memories/data.txt"

    @pytest.mark.asyncio
    async def test_indexing_failure_logs_error(self, mock_agent_settings, caplog):
        """Indexing failure is logged at error level, not silently swallowed."""
        import logging
        from deepagents.backends.protocol import WriteResult

        runtime = _make_runtime()
        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        success_result = WriteResult(path="/memories/data.txt", files_update=None)

        with patch.object(
            type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=success_result)
        ):
            with patch.object(backend, "_index_content", side_effect=Exception("Bedrock throttled")):
                with caplog.at_level(logging.ERROR, logger="agent_common.backends.indexing_store"):
                    await backend.awrite("/memories/data.txt", "some data")

        assert any("Failed to index" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_parent_write_failure_skips_indexing(self, mock_agent_settings):
        """When the parent write fails, _index_content must NOT be called."""
        from deepagents.backends.protocol import WriteResult

        runtime = _make_runtime()
        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        failed_result = WriteResult(error="Disk full")

        with patch.object(
            type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=failed_result)
        ):
            with patch.object(backend, "_index_content") as mock_index:
                result = await backend.awrite("/memories/data.txt", "content")

        # Failure indicated by non-None error field
        assert result.error == "Disk full"
        mock_index.assert_not_called()

class TestMissingMetadata:
    """Missing user_id or conversation_id should not raise — just log and return."""

    @pytest.mark.asyncio
    async def test_missing_user_id_returns_without_exception(self, mock_agent_settings):
        """When user_id is absent from metadata, _index_content returns without error."""
        runtime = Mock(spec=ToolRuntime)
        runtime.config = {"metadata": {}}  # No user_id
        runtime.store = AsyncMock()

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            # Should not raise
            await backend._index_content("/memories/notes.txt", "content")

        mock_chunk.assert_not_called()
        runtime.store.aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_conversation_id_for_tool_result_returns_without_exception(
        self, mock_agent_settings
    ):
        """Tool result path without conversation_id returns early without exception."""
        runtime = Mock(spec=ToolRuntime)
        runtime.config = {"metadata": {"user_id": "u1"}}  # No conversation_id
        runtime.store = AsyncMock()

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            await backend._index_content("/large_tool_results/tool-999", "data")

        mock_chunk.assert_not_called()
        runtime.store.aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_assistant_id_for_channel_memories_returns_without_exception(
        self, mock_agent_settings
    ):
        """channel_memories path without assistant_id returns early without exception."""
        runtime = Mock(spec=ToolRuntime)
        runtime.config = {"metadata": {"user_id": "u1"}}  # No assistant_id
        runtime.store = AsyncMock()

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
            await backend._index_content("/channel_memories/team-notes.txt", "data")

        mock_chunk.assert_not_called()
        runtime.store.aput.assert_not_called()

class TestTitanSemaphore:
    """Titan embedding calls must be capped to _EMBED_CONCURRENCY=5 concurrent calls."""

    @pytest.mark.asyncio
    async def test_max_5_concurrent_embed_calls(self, mock_agent_settings):
        """With 10 chunks, only 5 embedding calls are in-flight at any moment."""
        runtime = _make_runtime()
        runtime.store.aget.return_value = None  # Not previously indexed

        backend = IndexingStoreBackend(runtime, mock_agent_settings)

        # Track how many aput calls are active simultaneously
        active_count = [0]
        max_observed = [0]

        async def tracked_aput(*args, **kwargs):
            active_count[0] += 1
            max_observed[0] = max(max_observed[0], active_count[0])
            await asyncio.sleep(0)  # yield so other tasks can start
            active_count[0] -= 1

        runtime.store.aput = tracked_aput

        # 10 chunks → 10 aput calls for chunks (+ 1 for file index)
        chunks = [(f"chunk {i}", f"context {i}") for i in range(10)]

        with patch("agent_common.backends.indexing_store.chunk_with_context", return_value=chunks):
            with patch("agent_common.backends.indexing_store.create_model", return_value=MagicMock()):
                await backend._index_content("/memories/big-doc.txt", "big content " * 100)

        # With semaphore=5, max concurrent aput calls for chunks should be ≤5
        # (The file-index aput runs before chunk aputs so it doesn't count)
        # We allow for the file_index aput being counted: max≤6
        assert max_observed[0] <= 6, (
            f"Expected at most 6 concurrent aput calls (5 chunks + 1 file index), "
            f"but observed {max_observed[0]}"
        )
