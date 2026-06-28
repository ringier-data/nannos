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
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from deepagents.backends.store import StoreBackend

from agent_common.backends.indexing_store import (
    _MAX_STORAGE_KEY_BYTES,
    IndexingStoreBackend,
    bounded_storage_key,
)

# A real-shape Gemini tool-call id from the LiteLLM gateway: the genuine call id
# followed by a multi-KB packed thought-signature. Used to reproduce the
# ProgramLimitExceeded ("index row size exceeds btree maximum") store failure.
_GEMINI_CALL_ID = "call_7acafe2be6f646eaa9562734ce9f"
_THOUGHT_PACKED_ID = f"{_GEMINI_CALL_ID}__thought__" + "AY89a1" + "x" * 2700
_OFFLOAD_PATH = f"/large_tool_results/{_THOUGHT_PACKED_ID}"
_BOUNDED_PATH = f"/large_tool_results/{_GEMINI_CALL_ID}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _make_config(
    user_id: str = "user-xyz",
    conversation_id: str = "conv-123",
    assistant_id: str = "asst-abc",
) -> dict:
    return {
        "metadata": {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "assistant_id": assistant_id,
        }
    }


def _make_store() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_agent_settings():
    return "us-east-1"


class TestIdempotency:
    """Second write to the same path must skip re-indexing when the content is unchanged."""

    @pytest.mark.asyncio
    async def test_skips_indexing_when_content_hash_matches(self, mock_agent_settings):
        """If existing index has a matching content_hash, chunking is NOT called."""
        mock_store = _make_store()
        content = "some content here"

        # Simulate file already indexed with the SAME content hash
        existing_entry = MagicMock()
        existing_entry.value = {"total_chunks": 3, "content_hash": _content_hash(content)}
        mock_store.aget.return_value = existing_entry

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                await backend._index_content("/memories/notes.txt", content)

        mock_chunk.assert_not_called()
        # Only aget (idempotency check) should have been called, not aput
        mock_store.aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_reindexes_when_content_hash_differs(self, mock_agent_settings):
        """If existing index has a different content_hash, stale chunks are dropped and re-indexed."""
        mock_store = _make_store()
        content = "brand new content"

        existing_entry = MagicMock()
        existing_entry.value = {
            "total_chunks": 2,
            "content_hash": "stale-hash",
            "chunk_keys": ["/memories/notes.txt#chunk_0", "/memories/notes.txt#chunk_1"],
        }
        mock_store.aget.return_value = existing_entry

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                mock_chunk.return_value = [("chunk text", "context")]
                with patch("agent_common.backends.indexing_store.create_model", return_value=MagicMock()):
                    await backend._index_content("/memories/notes.txt", content)

        # Re-chunked because the hash changed
        mock_chunk.assert_called_once()
        # Stale chunks were deleted before re-indexing
        deleted_keys = {call.kwargs.get("key") for call in mock_store.adelete.await_args_list}
        assert "/memories/notes.txt#chunk_0" in deleted_keys
        assert "/memories/notes.txt#chunk_1" in deleted_keys

    @pytest.mark.asyncio
    async def test_proceeds_with_indexing_when_not_yet_indexed(self, mock_agent_settings):
        """If documents_store.aget returns None, chunking IS called."""
        mock_store = _make_store()
        mock_store.aget.return_value = None  # No existing index

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                mock_chunk.return_value = [("chunk text", "context")]
                with patch("agent_common.backends.indexing_store.create_model") as mock_model:
                    mock_model.return_value = MagicMock()
                    await backend._index_content("/memories/new.txt", "new content")

        mock_chunk.assert_called_once()

    @pytest.mark.asyncio
    async def test_stores_content_hash_in_file_index(self, mock_agent_settings):
        """The file index value persisted to the store includes the content_hash."""
        mock_store = _make_store()
        mock_store.aget.return_value = None
        content = "indexable content"

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch(
                "agent_common.backends.indexing_store.chunk_with_context",
                return_value=[("chunk", "ctx")],
            ):
                with patch("agent_common.backends.indexing_store.create_model", return_value=MagicMock()):
                    await backend._index_content("/memories/notes.txt", content)

        # Find the file-index aput (key == file_path) and assert content_hash
        file_index_calls = [
            call for call in mock_store.aput.await_args_list if call.kwargs.get("key") == "/memories/notes.txt"
        ]
        assert file_index_calls, "Expected a file-index aput keyed by the file path"
        assert file_index_calls[0].kwargs["value"]["content_hash"] == _content_hash(content)


class TestSkipFormattedSearchOutput:
    """Formatted docstore_search output stored as large_tool_result must be skipped."""

    @pytest.mark.asyncio
    async def test_skips_when_content_is_formatted_search_output(self, mock_agent_settings):
        """Content starting with 'Found N' and containing 'relevant chunks:' is skipped."""
        mock_store = _make_store()
        mock_store.aget.return_value = None  # Not previously indexed

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        formatted_output = "Found 5 relevant chunks:\n\n[Result 1] /memories/foo.txt (chunk 1/3)\n..."

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                await backend._index_content("/large_tool_results/tool-456", formatted_output)

        mock_chunk.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_skip_regular_tool_result_content(self, mock_agent_settings):
        """Non-formatted tool result content is indexed normally."""
        mock_store = _make_store()
        mock_store.aget.return_value = None

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        regular_content = '{"status": "merged", "pr_number": 42, "title": "Fix bug"}'

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                mock_chunk.return_value = [("chunk", "ctx")]
                with patch("agent_common.backends.indexing_store.create_model"):
                    await backend._index_content("/large_tool_results/gh-pr", regular_content)

        mock_chunk.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_guard_only_applies_to_large_tool_results(self, mock_agent_settings):
        """The anti-meta guard only triggers for /large_tool_results/ paths, not /memories/."""
        mock_store = _make_store()
        mock_store.aget.return_value = None

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        # Unusual but valid: a memories file starting with "Found"
        content = "Found 5 relevant chunks: ... (user wrote this themselves)"

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
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

        mock_store = _make_store()
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        success_result = WriteResult(path="/memories/data.txt")

        with patch.object(type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=success_result)):
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

        mock_store = _make_store()
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        success_result = WriteResult(path="/memories/data.txt")

        with patch.object(type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=success_result)):
            with patch.object(backend, "_index_content", side_effect=Exception("Bedrock throttled")):
                with caplog.at_level(logging.ERROR, logger="agent_common.backends.indexing_store"):
                    await backend.awrite("/memories/data.txt", "some data")

        assert any("Failed to index" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_parent_write_failure_skips_indexing(self, mock_agent_settings):
        """When the parent write fails, _index_content must NOT be called."""
        from deepagents.backends.protocol import WriteResult

        mock_store = _make_store()
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        failed_result = WriteResult(error="Disk full")

        with patch.object(type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=failed_result)):
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
        mock_store = _make_store()

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        with patch("agent_common.backends.indexing_store.get_config", return_value={"metadata": {}}):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                # Should not raise
                await backend._index_content("/memories/notes.txt", "content")

        mock_chunk.assert_not_called()
        mock_store.aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_conversation_id_for_tool_result_returns_without_exception(self, mock_agent_settings):
        """Tool result path without conversation_id returns early without exception."""
        mock_store = _make_store()

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        config = {"metadata": {"user_id": "u1"}}  # No conversation_id
        with patch("agent_common.backends.indexing_store.get_config", return_value=config):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                await backend._index_content("/large_tool_results/tool-999", "data")

        mock_chunk.assert_not_called()
        mock_store.aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_assistant_id_for_channel_memories_returns_without_exception(self, mock_agent_settings):
        """channel_memories path without assistant_id returns early without exception."""
        mock_store = _make_store()

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        config = {"metadata": {"user_id": "u1"}}  # No assistant_id
        with patch("agent_common.backends.indexing_store.get_config", return_value=config):
            with patch("agent_common.backends.indexing_store.chunk_with_context") as mock_chunk:
                await backend._index_content("/channel_memories/team-notes.txt", "data")

        mock_chunk.assert_not_called()
        mock_store.aput.assert_not_called()


class TestTitanSemaphore:
    """Titan embedding calls must be capped to _EMBED_CONCURRENCY=5 concurrent calls."""

    @pytest.mark.asyncio
    async def test_max_5_concurrent_embed_calls(self, mock_agent_settings):
        """With 10 chunks, only 5 embedding calls are in-flight at any moment."""
        mock_store = _make_store()
        mock_store.aget.return_value = None  # Not previously indexed

        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        # Track how many aput calls are active simultaneously
        active_count = [0]
        max_observed = [0]

        async def tracked_aput(*args, **kwargs):
            active_count[0] += 1
            max_observed[0] = max(max_observed[0], active_count[0])
            await asyncio.sleep(0)  # yield so other tasks can start
            active_count[0] -= 1

        mock_store.aput = tracked_aput

        # 10 chunks → 10 aput calls for chunks (+ 1 for file index)
        chunks = [(f"chunk {i}", f"context {i}") for i in range(10)]

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch("agent_common.backends.indexing_store.chunk_with_context", return_value=chunks):
                with patch("agent_common.backends.indexing_store.create_model", return_value=MagicMock()):
                    await backend._index_content("/memories/big-doc.txt", "big content " * 100)

        # With semaphore=5, max concurrent aput calls for chunks should be ≤5
        # (The file-index aput runs before chunk aputs so it doesn't count)
        # We allow for the file_index aput being counted: max≤6
        assert max_observed[0] <= 6, (
            f"Expected at most 6 concurrent aput calls (5 chunks + 1 file index), but observed {max_observed[0]}"
        )


class TestEagerIndexingSkipsToolResults:
    """awrite must NOT eagerly index /large_tool_results/ (lazy via semantic_search_file)."""

    @pytest.mark.asyncio
    async def test_awrite_skips_eager_index_for_large_tool_results(self, mock_agent_settings):
        from deepagents.backends.protocol import WriteResult

        mock_store = _make_store()
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        success_result = WriteResult(path="/large_tool_results/tool-1")

        with patch.object(type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=success_result)):
            with patch.object(backend, "_index_content") as mock_index:
                result = await backend.awrite("/large_tool_results/tool-1", "huge tool output")

        # File still written, but NOT eagerly indexed
        assert result.path == "/large_tool_results/tool-1"
        mock_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_awrite_still_indexes_memories(self, mock_agent_settings):
        from deepagents.backends.protocol import WriteResult

        mock_store = _make_store()
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        success_result = WriteResult(path="/memories/notes.txt")

        with patch.object(type(backend).__mro__[1], "awrite", new=AsyncMock(return_value=success_result)):
            with patch.object(backend, "_index_content", new=AsyncMock()) as mock_index:
                await backend.awrite("/memories/notes.txt", "durable note")

        mock_index.assert_awaited_once()


class TestAensureIndexed:
    """aensure_indexed is the JIT entry-point used by semantic_search_file."""

    @pytest.mark.asyncio
    async def test_aensure_indexed_delegates_to_index_content(self, mock_agent_settings):
        mock_store = _make_store()
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        with patch.object(backend, "_index_content", new=AsyncMock()) as mock_index:
            await backend.aensure_indexed("/large_tool_results/tool-9", "blob content")

        mock_index.assert_awaited_once_with("/large_tool_results/tool-9", "blob content")


class TestBoundedStorageKey:
    """The store key derived from a path must never exceed the btree limit.

    LiteLLM packs Gemini thought-signatures into the tool-call id
    (``call_<id>__thought__<sig>``), which makes the offload path multi-KB and
    overflows the Postgres store_pkey index. bounded_storage_key() must drop the
    signature, stay under the cap, and be idempotent and prefix-preserving.
    """

    def test_drops_thought_signature_tail(self):
        assert bounded_storage_key(_OFFLOAD_PATH) == _BOUNDED_PATH

    def test_result_is_within_byte_cap(self):
        out = bounded_storage_key(_OFFLOAD_PATH)
        assert len(out.encode("utf-8")) <= _MAX_STORAGE_KEY_BYTES

    def test_idempotent(self):
        once = bounded_storage_key(_OFFLOAD_PATH)
        assert bounded_storage_key(once) == once

    def test_noop_for_normal_paths(self):
        for path in (
            _BOUNDED_PATH,
            "/memories/notes.md",
            "/channel_memories/team/playbook.md",
            "/attachments/image.png",
        ):
            assert bounded_storage_key(path) == path

    def test_caps_oversized_path_without_delimiter(self):
        # Defends against a future provider/convention that bloats the id without
        # the __thought__ marker: still capped, still idempotent.
        huge = "/large_tool_results/" + "q" * 5000
        out = bounded_storage_key(huge)
        assert len(out.encode("utf-8")) <= _MAX_STORAGE_KEY_BYTES
        assert bounded_storage_key(out) == out

    def test_distinct_ids_yield_distinct_keys(self):
        # The genuine call id (before __thought__) is unique per call, so bounding
        # must not collapse two different tool calls onto the same key.
        a = bounded_storage_key("/large_tool_results/call_aaaa__thought__" + "x" * 2700)
        b = bounded_storage_key("/large_tool_results/call_bbbb__thought__" + "x" * 2700)
        assert a != b

    def test_legit_thought_substring_in_user_path_is_preserved(self):
        # The __thought__ drop is scoped to /large_tool_results/. A user file that
        # happens to contain that substring must NOT be truncated — otherwise two
        # distinct files would collide onto one key and become unaddressable.
        a = "/memories/my__thought__journal.md"
        b = "/memories/my__thought__notes.md"
        assert bounded_storage_key(a) == a
        assert bounded_storage_key(b) == b
        assert bounded_storage_key(a) != bounded_storage_key(b)


class TestBackendOpsBoundKeys:
    """IndexingStoreBackend op overrides must bound the key before it hits the store."""

    @pytest.mark.asyncio
    async def test_awrite_writes_under_bounded_key(self, mock_agent_settings):
        mock_store = _make_store()
        mock_store.aget.return_value = None  # parent existence check: not present
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        # /large_tool_results/ is not eagerly indexed, so only the filesystem
        # aput (parent awrite) should fire — and it must use the bounded key.
        result = await backend.awrite(_OFFLOAD_PATH, "huge tool result")

        assert not result.error
        assert mock_store.aput.await_args is not None
        written_key = mock_store.aput.await_args.args[1]  # store.aput(namespace, key, value)
        assert written_key == _BOUNDED_PATH
        assert len(written_key.encode("utf-8")) <= _MAX_STORAGE_KEY_BYTES

    @pytest.mark.asyncio
    async def test_aread_reads_under_bounded_key(self, mock_agent_settings):
        mock_store = _make_store()
        mock_store.aget.return_value = None  # content irrelevant; we assert the key
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        await backend.aread(_OFFLOAD_PATH)

        # aread -> parent uses store.aget(namespace, key); key must be bounded so a
        # file written under the bounded key is found when read by the long path.
        assert mock_store.aget.await_args.args[1] == _BOUNDED_PATH

    @pytest.mark.asyncio
    async def test_index_content_chunk_keys_are_bounded(self, mock_agent_settings):
        mock_store = _make_store()
        mock_store.aget.return_value = None  # not previously indexed
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        with patch("agent_common.backends.indexing_store.get_config", return_value=_make_config()):
            with patch(
                "agent_common.backends.indexing_store.chunk_with_context",
                return_value=[("chunk-a", "ctx-a"), ("chunk-b", "ctx-b")],
            ):
                with patch("agent_common.backends.indexing_store.create_model", return_value=MagicMock()):
                    await backend._index_content(_OFFLOAD_PATH, "indexable blob content")

        # Every key written (file-index + #chunk_N) must be bounded and rooted at
        # the bounded path — this is the path that produced the #chunk_6 failure.
        keys = [call.kwargs.get("key") for call in mock_store.aput.await_args_list]
        assert keys, "expected index writes"
        # Bounded path (<= cap) plus a "#chunk_<i>" suffix. Size the allowance off
        # the actual key count so the bound is correct even past 999 chunks.
        max_suffix = len(f"#chunk_{len(keys)}")
        for key in keys:
            assert key.startswith(_BOUNDED_PATH), key
            assert len(key.encode("utf-8")) <= _MAX_STORAGE_KEY_BYTES + max_suffix
        assert _BOUNDED_PATH in keys  # file-index entry
        assert f"{_BOUNDED_PATH}#chunk_0" in keys  # chunk entry

    @pytest.mark.asyncio
    async def test_aedit_edits_under_bounded_key(self, mock_agent_settings):
        mock_store = _make_store()
        mock_store.aget.return_value = None  # file-not-found early return; we assert the key
        backend = IndexingStoreBackend(store=mock_store, model_name=mock_agent_settings)

        await backend.aedit(_OFFLOAD_PATH, "old", "new")

        assert _BOUNDED_PATH in mock_store.aget.await_args.args
        assert _OFFLOAD_PATH not in mock_store.aget.await_args.args

    def test_sync_read_reads_under_bounded_key(self, mock_agent_settings):
        sync_store = MagicMock()
        sync_store.get.return_value = None
        backend = IndexingStoreBackend(store=sync_store, model_name=mock_agent_settings)

        backend.read(_OFFLOAD_PATH)

        assert _BOUNDED_PATH in sync_store.get.call_args.args

    def test_sync_write_writes_under_bounded_key(self, mock_agent_settings):
        sync_store = MagicMock()
        sync_store.get.return_value = None  # parent existence check: not present
        backend = IndexingStoreBackend(store=sync_store, model_name=mock_agent_settings)

        result = backend.write(_OFFLOAD_PATH, "huge tool result")

        assert not result.error
        assert _BOUNDED_PATH in sync_store.put.call_args.args
        assert _OFFLOAD_PATH not in sync_store.put.call_args.args

    def test_sync_edit_edits_under_bounded_key(self, mock_agent_settings):
        sync_store = MagicMock()
        sync_store.get.return_value = None  # file-not-found early return; we assert the key
        backend = IndexingStoreBackend(store=sync_store, model_name=mock_agent_settings)

        backend.edit(_OFFLOAD_PATH, "old", "new")

        assert _BOUNDED_PATH in sync_store.get.call_args.args

    def test_grep_bounds_the_path_filter(self, mock_agent_settings):
        # The agent is handed the raw offload path in the eviction message; an
        # exact-path grep on the raw path must be bounded so it matches the
        # bounded stored key rather than finding nothing.
        backend = IndexingStoreBackend(store=_make_store(), model_name=mock_agent_settings)
        with patch.object(StoreBackend, "grep", return_value=MagicMock()) as parent_grep:
            backend.grep("pattern", _OFFLOAD_PATH)
        assert _BOUNDED_PATH in parent_grep.call_args.args
        assert _OFFLOAD_PATH not in parent_grep.call_args.args

    def test_grep_passes_through_none_path(self, mock_agent_settings):
        backend = IndexingStoreBackend(store=_make_store(), model_name=mock_agent_settings)
        with patch.object(StoreBackend, "grep", return_value=MagicMock()) as parent_grep:
            backend.grep("pattern")
        assert None in parent_grep.call_args.args

    def test_glob_bounds_the_path_filter(self, mock_agent_settings):
        backend = IndexingStoreBackend(store=_make_store(), model_name=mock_agent_settings)
        with patch.object(StoreBackend, "glob", return_value=MagicMock()) as parent_glob:
            backend.glob("*.md", _OFFLOAD_PATH)
        assert _BOUNDED_PATH in parent_glob.call_args.args

    def test_ls_bounds_the_path(self, mock_agent_settings):
        backend = IndexingStoreBackend(store=_make_store(), model_name=mock_agent_settings)
        with patch.object(StoreBackend, "ls", return_value=MagicMock()) as parent_ls:
            backend.ls(_OFFLOAD_PATH)
        assert _BOUNDED_PATH in parent_ls.call_args.args
