"""IndexingStoreBackend: StoreBackend with automatic semantic indexing.

This backend extends StoreBackend to automatically index files written to
long-term storage for semantic search. Files are stored in the filesystem
namespace and indexed chunks are stored in the documents namespace.

Architecture - Three-Route System:
- Personal files (/memories/): (user_id, "filesystem") → (user_id, "documents")
- Tool results (/large_tool_results/): (conversation_id, "filesystem") → (conversation_id, "tool_results")
- Channel files (/channel_memories/): (assistant_id, "filesystem") → (assistant_id, "documents")

Each route has its own IndexingStoreBackend instance with dedicated namespace.
Application logic chooses the path based on scope.

CRITICAL: Namespaces MUST NOT overlap:
- user_id ≠ conversation_id ≠ assistant_id
- "documents" ≠ "tool_results"
- No fallback between scopes (prevents data contamination)

Handles both:
1. Normal write_file/edit_file operations
2. Large tool result evictions from FilesystemMiddleware
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from deepagents.backends.protocol import EditResult, GlobResult, GrepResult, LsResult, ReadResult, WriteResult
from deepagents.backends.store import StoreBackend
from langgraph.config import get_config
from langgraph.store.postgres.aio import AsyncPostgresStore
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.cost_tracking.logger import (
    flush_cost_batch,
    set_request_conversation_id,
    set_request_user_sub,
    start_cost_batching,
)

from agent_common.core.model_factory import create_model, get_default_indexing_model, require_default_model
from agent_common.core.semantic_chunking import TITAN_EMBED_MAX_CHARS, chunk_with_context

logger = logging.getLogger(__name__)

# Largest store key we will ever write. The Postgres store primary key
# (``store_pkey``) indexes ``prefix + key``, and a btree entry cannot exceed
# ~2704 bytes (1/3 of an 8 KiB page). Gemini tool-call ids arrive from the
# LiteLLM gateway with the thought-signature packed into the id
# (``call_<id>__thought__<base64-sig>`` — an intentional, non-configurable
# LiteLLM behaviour, see BerriAI/litellm#17949). Offload paths derived from such
# ids (``/large_tool_results/<id>`` and their ``#chunk_N`` keys) blow past the
# btree limit and the write raises ``ProgramLimitExceeded``. Capping the key well
# under the limit leaves ample headroom for the namespace ``prefix``.
_MAX_STORAGE_KEY_BYTES = 200
_THOUGHT_SIGNATURE_DELIMITER = "__thought__"
_KEY_HASH_LEN = 16


def bounded_storage_key(file_path: str) -> str:
    """Map a (possibly oversized) file path to a bounded, stable store key.

    Tool-result paths can embed a LiteLLM Gemini thought-signature in the
    tool-call id. The signature is load-bearing for the Gemini round-trip and must
    stay on the message, but it must NOT leak into the store key (the Postgres
    btree primary key cannot index a value larger than ~2704 bytes). This:

    1. Drops the ``__thought__<sig>`` tail, but ONLY for ``/large_tool_results/``
       paths (the sole place tool-call ids appear). The part before the delimiter
       is Gemini's genuine per-call id, so distinct tool calls keep distinct
       (collision-free) keys. Scoping to that prefix means a legitimate user
       filename that happens to contain ``__thought__`` (a ``/memories/`` or
       ``/channel_memories/`` path) is never silently truncated.
    2. Hard-caps the remaining length with a content-hash suffix, applied to ANY
       path, so no provider convention (now or future) can overflow the index even
       if the ``__thought__`` delimiter changes or disappears.

    Idempotent: applying it to an already-bounded key returns it unchanged, so it
    is safe to call at every store boundary (write, read, edit, indexing, search).
    A true no-op for normal paths (those at/under 200 bytes without a tool-result
    thought-signature).
    """
    base = file_path
    if file_path.startswith("/large_tool_results/"):
        base = file_path.split(_THOUGHT_SIGNATURE_DELIMITER, 1)[0]
    encoded = base.encode("utf-8")
    if len(encoded) <= _MAX_STORAGE_KEY_BYTES:
        return base
    digest = hashlib.sha256(encoded).hexdigest()[:_KEY_HASH_LEN]
    # Reserve room for "_" + digest so the result stays within the cap (hence
    # idempotent on a second pass). Decode with "ignore" so a multi-byte char
    # split at the truncation boundary can't raise.
    keep = _MAX_STORAGE_KEY_BYTES - _KEY_HASH_LEN - 1
    truncated = encoded[:keep].decode("utf-8", "ignore")
    return f"{truncated}_{digest}"


class IndexingStoreBackend(StoreBackend):
    """StoreBackend with automatic semantic indexing of written files.

    Extends StoreBackend to automatically index file content when written.
    Uses semantic chunking with Claude for contextualization and stores
    chunks in a separate documents namespace for semantic search.

    Architecture - Three-Route System:
    - Personal files (/memories/):
      * Files: (user_id, "filesystem")
      * Chunks: (user_id, "documents")
    - Tool results (/large_tool_results/):
      * Files: (conversation_id, "filesystem")
      * Chunks: (conversation_id, "tool_results")
    - Channel files (/channel_memories/):
      * Files: (assistant_id, "filesystem")
      * Chunks: (assistant_id, "documents")

    Each path routes to a separate IndexingStoreBackend instance with its
    own namespace. Application logic (write_file tool) chooses the path.

    Args:
        runtime: ToolRuntime providing store access and metadata
        model_name: Model to use for indexing/chunking contextualization. When
            ``None``, ``get_default_indexing_model()`` uses the low chat tier
            (the fleet's cheap chat model), falling back to the chat default.
        cost_logger: Optional CostLogger for reporting indexing costs
        namespace_factory: Optional callable for determining write namespace
    """

    def __init__(
        self,
        store: AsyncPostgresStore,
        model_name: str | None = None,
        cost_logger: Optional[CostLogger] = None,
        namespace_factory: Optional[Any] = None,
    ):
        """Initialize IndexingStoreBackend.

        Args:
            store: AsyncPostgresStore for storage and indexing
            model_name: Model to use for chunking/contextualization. When ``None``,
                ``get_default_indexing_model()`` uses the low chat tier (cheap), then
                falls back to the chat default.
            cost_logger: Optional CostLogger for reporting LLM usage costs
            namespace_factory: Optional callable that takes a Runtime and returns
                namespace tuple. Used to scope file storage (read/write operations).
                If None, uses legacy assistant_id-based scoping.
        """
        # Pass store and namespace factory to parent StoreBackend
        # This ensures read operations (grep, read_file, etc.) are scoped correctly
        super().__init__(store=store, namespace=namespace_factory)
        self._model_name = model_name
        self.documents_store: AsyncPostgresStore = store
        self._cost_logger = cost_logger

    # ------------------------------------------------------------------
    # Key-bounding op overrides
    #
    # Every store key is run through ``bounded_storage_key`` before it reaches the
    # Postgres store so an oversized tool-call id (Gemini ``__thought__`` packing)
    # cannot overflow the btree primary key. The transform is idempotent and a
    # no-op for normal paths, and is applied uniformly to write AND read/edit/grep/
    # glob/ls so the agent can still address a file by the (long) path it was handed
    # in the eviction message. ``_index_content`` and ``_semantic_search_file_impl``
    # apply the same transform at their own direct-store boundaries.
    # ------------------------------------------------------------------

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return super().read(bounded_storage_key(file_path), offset, limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await super().aread(bounded_storage_key(file_path), offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        # Parent sync write does no indexing (matching prior behaviour); only the
        # async awrite path indexes. Here we just bound the key.
        return super().write(bounded_storage_key(file_path), content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return super().edit(bounded_storage_key(file_path), old_string, new_string, replace_all)

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return await super().aedit(bounded_storage_key(file_path), old_string, new_string, replace_all)

    def ls(self, path: str) -> LsResult:
        return super().ls(bounded_storage_key(path))

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        # Bound the path filter too: the agent is handed the (long, raw) offload
        # path in the eviction message, but the blob is stored under the bounded
        # key, so an exact-path grep on the raw path would otherwise match nothing.
        # A directory prefix like "/large_tool_results/" is short and unchanged.
        return super().grep(pattern, bounded_storage_key(path) if path else path, glob)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        return super().glob(pattern, bounded_storage_key(path))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Write file and automatically index content.

        Calls parent awrite() to store file, then performs semantic
        chunking and indexing in documents namespace.

        Args:
            file_path: Absolute file path
            content: File content

        Returns:
            WriteResult from parent write operation
        """
        # Bound the key up-front so both the filesystem write and the indexing
        # below key off the same bounded path (see bounded_storage_key). The
        # ``/large_tool_results/`` prefix is preserved, so the routing checks below
        # still work.
        file_path = bounded_storage_key(file_path)

        # Call parent to write file to filesystem namespace
        result = await super().awrite(file_path, content)

        # If write failed, don't attempt indexing
        if result.error:
            return result

        # Evicted tool results (/large_tool_results/) are NOT eagerly indexed.
        # They are transient conversation-scoped storage; semantic access is
        # provided lazily and on-demand via the `semantic_search_file` tool
        # (which calls `aensure_indexed`). Durable memory paths keep eager
        # indexing so `docstore_search` finds them. See core/CONTEXT.md (D5/D9).
        if file_path.startswith("/large_tool_results/"):
            logger.debug(f"Skipping eager indexing for transient tool result {file_path}")
            return result

        # Index the content asynchronously (don't fail write if indexing fails)
        try:
            await self._index_content(file_path, content)
            logger.info(f"Indexed {file_path} for semantic search")
        except Exception as e:
            # Log but don't fail the write operation
            logger.error(f"Failed to index {file_path}: {e}", exc_info=True)

        return result

    async def aensure_indexed(self, file_path: str, content: str) -> None:
        """Ensure a file's content is indexed for semantic search (JIT entry-point).

        Public, on-demand counterpart to the eager indexing performed by
        :meth:`awrite`. Used by the ``semantic_search_file`` tool to lazily
        chunk + embed a single in-hand file (typically an evicted
        ``/large_tool_results/`` blob) the first time it is searched.

        Idempotent via the content-hash caching in :meth:`_index_content`:
        re-indexing is skipped when the file's content is unchanged and
        re-vectorised only when the content hash differs.

        Args:
            file_path: Absolute file path.
            content: Current file content to index.
        """
        await self._index_content(file_path, content)

    async def _index_content(self, file_path: str, content: str) -> None:
        """Index file content for semantic search.

        Performs semantic chunking with contextualization and stores
        chunks in the documents namespace with embeddings.

        Args:
            file_path: Path of the file
            content: File content to index
        """
        # Bound the key the same way the filesystem write does, so the file-index
        # entry and every ``{file_path}#chunk_{i}`` key stay under the btree limit
        # (a raw Gemini ``__thought__`` id makes the chunk keys even longer than the
        # filesystem key). Idempotent: when called from ``awrite`` the path is
        # already bounded; when called from ``aensure_indexed`` (the lazy
        # semantic_search_file path) it is the raw agent-supplied path.
        file_path = bounded_storage_key(file_path)

        # Get user_id and user_sub from runtime metadata or tags
        config = get_config()
        metadata_dict = config.get("metadata", {})
        user_id = metadata_dict.get("user_id")
        user_sub = metadata_dict.get("user_sub")
        conversation_id = metadata_dict.get("conversation_id")

        # Fallback: try to extract user_sub from tags if not in metadata
        if not user_sub:
            tags = config.get("tags", [])
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("user_sub:"):
                    user_sub = tag.split(":", 1)[1]
                    logger.debug(f"[COST TRACKING] Extracted user_sub from tag: {user_sub}")
                    break

        # Fallback: try to extract conversation_id from tags if not in metadata
        if not conversation_id:
            tags = config.get("tags", [])
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("conversation:"):
                    conversation_id = tag.split(":", 1)[1]
                    logger.debug(f"[COST TRACKING] Extracted conversation_id from tag: {conversation_id}")
                    break

        if not user_id:
            logger.warning(f"No user_id in runtime config, cannot index {file_path}")
            return

        # Set user_sub and conversation_id for cost tracking
        # This enables CostTrackingBedrockEmbeddings to log costs with proper attribution
        if user_sub:
            set_request_user_sub(user_sub)
            logger.info(f"[COST TRACKING] Set user_sub={user_sub[:8]}... for indexing {file_path}")
        else:
            logger.warning(
                f"[COST TRACKING] No user_sub in metadata or tags for {file_path}, "
                f"embeddings won't be tracked. Config keys: {list(config.keys())}"
            )

        if conversation_id:
            set_request_conversation_id(conversation_id)
            logger.info(f"[COST TRACKING] Set conversation_id={conversation_id[:8]}... for indexing {file_path}")
        else:
            logger.debug(
                f"[COST TRACKING] No conversation_id for {file_path}, embedding costs won't be conversation-attributed"
            )

        # Start cost batching to aggregate all embedding calls from this indexing operation
        # This prevents log flooding from semantic chunking (which makes many embedding calls)
        start_cost_batching()

        try:
            # Detect if this is a large tool result
            is_large_tool_result = file_path.startswith("/large_tool_results/")
            # Attachments are ephemeral, conversation-scoped files indexed lazily
            # (JIT) by ``semantic_search_file``. They share the conversation's
            # ``tool_results`` chunk namespace so the search finds them.
            is_attachment = file_path.startswith("/attachments/")

            # Determine namespace based on file path:
            # - /large_tool_results/ and /attachments/: (conversation_id, "tool_results") - conversation-scoped
            # - /memories/: (user_id, "documents") - user-scoped
            # - /channel_memories/: (assistant_id, "documents") - channel-scoped
            if is_large_tool_result or is_attachment:
                conversation_id_ns = metadata_dict.get("conversation_id")
                if not conversation_id_ns:
                    logger.error(
                        f"Cannot index {file_path}: conversation_id missing from metadata. "
                        f"Conversation-scoped files require conversation context for proper scoping."
                    )
                    return
                namespace = (conversation_id_ns, "tool_results")
                logger.debug(f"Using conversation-scoped namespace for {file_path}: {namespace}")
            elif file_path.startswith("/channel_memories/"):
                # Channel files: assistant-scoped namespace
                assistant_id = metadata_dict.get("assistant_id")
                if not assistant_id:
                    logger.error(
                        f"Cannot index channel file {file_path}: assistant_id missing from metadata. "
                        f"Channel files require assistant context for proper scoping."
                    )
                    return
                namespace = (assistant_id, "documents")
                logger.debug(f"Using channel-scoped namespace for channel file: {namespace}")
            else:
                # User files (/memories/): user-scoped namespace
                namespace = (user_id, "documents")
                logger.debug(f"Using user-scoped namespace for personal file: {namespace}")

            # Content-hash caching: skip re-indexing when content is unchanged,
            # re-vectorise when it differs. This makes both eager (awrite) and
            # lazy (aensure_indexed) indexing idempotent and cheap to call
            # repeatedly on the same in-hand blob. See core/CONTEXT.md (D9).
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            existing_index = await self.documents_store.aget(namespace=namespace, key=file_path)

            if existing_index is not None and existing_index.value.get("content_hash") == content_hash:
                logger.info(
                    f"File '{file_path}' already indexed with matching content hash "
                    f"({existing_index.value.get('total_chunks', 0)} chunks). Skipping re-indexing."
                )
                return

            # Content changed (or never hashed): drop stale chunks before re-indexing
            # so a shorter new version doesn't leave orphaned higher-index chunks.
            if existing_index is not None:
                stale_chunk_keys = existing_index.value.get("chunk_keys", [])
                if stale_chunk_keys:
                    await asyncio.gather(
                        *[self.documents_store.adelete(namespace=namespace, key=key) for key in stale_chunk_keys]
                    )
                    logger.info(
                        f"Content changed for '{file_path}'; deleted {len(stale_chunk_keys)} stale chunks "
                        f"before re-indexing."
                    )

            # Detect and skip formatted docstore_search results
            # These are evicted tool results that contain already-formatted search output
            # like "Found 5 relevant chunks:\n\n[Result 1] /path (chunk X/Y)\nContext: ..."
            # Indexing these creates "meta-documents" that reference other chunks without
            # adding semantic value, polluting search results
            if is_large_tool_result and content.startswith("Found ") and "relevant chunks:" in content[:100]:
                logger.info(
                    f"Skipping indexing of '{file_path}' - appears to be formatted docstore_search output. "
                    f"These results reference already-indexed content and shouldn't be re-indexed."
                )
                return

            # Prepare metadata
            if is_large_tool_result:
                # Extract tool_call_id from path
                tool_call_id = file_path.split("/")[-1]
                metadata = {
                    "file_path": file_path,
                    "source": "tool_result",
                    "tool_call_id": tool_call_id,
                    "original_size": len(content),
                    "evicted_at": datetime.now(tz=timezone.utc).isoformat(),
                }
            else:
                metadata = {
                    "file_path": file_path,
                    "source": "filesystem",
                }

            # Create model for contextualization (prefers the cheap low chat tier).
            # require_default_model() turns an unconfigured fleet default into a clear error
            # instead of create_model(None) — get_default_indexing_model() can be None now that
            # there's no env/hardcoded default fallback.
            model = create_model(self._model_name or get_default_indexing_model() or require_default_model())

            # Perform semantic chunking with contextualization
            chunks = await chunk_with_context(content, metadata, model, cost_logger=self._cost_logger)

            logger.info(f"Generated {len(chunks)} semantic chunks for '{file_path}'")

            # Store the file index metadata (without embedding) as a lookup
            file_index_value = {
                "file_path": file_path,
                "content_hash": content_hash,
                "total_chunks": len(chunks),
                "chunk_keys": [f"{file_path}#chunk_{i}" for i in range(len(chunks))],
                "metadata": metadata,
            }
            await self.documents_store.aput(
                namespace=namespace,
                key=file_path,
                value=file_index_value,
            )

            # Store individual chunks with embeddings for semantic search.
            # Following the Anthropic contextual embeddings cookbook: we embed the
            # context description prepended to the chunk text. This anchors the vector
            # in the chunk's specific terminology while enriching it with semantic
            # framing from Claude — better recall than description-only vectors.
            # Capped at TITAN_EMBED_MAX_CHARS (50k) to stay within Bedrock's limit;
            # chunks are ≤40k chars by design so concatenation is always safe.
            #
            # Use a semaphore to cap concurrent Bedrock embedding calls so we don't
            # blow through the Titan Embeddings V2 TPS quota on large documents.
            _EMBED_CONCURRENCY = 5
            sem = asyncio.Semaphore(_EMBED_CONCURRENCY)

            async def _aput_chunk(i: int, chunk_text: str, context_description: str) -> None:
                key = f"{file_path}#chunk_{i}"
                contextualized_content = (f"{context_description}\n\n{chunk_text}")[:TITAN_EMBED_MAX_CHARS]
                value = {
                    "file_path": file_path,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "content": chunk_text,
                    "context_description": context_description,
                    "contextualized_content": contextualized_content,
                    "metadata": metadata,
                }
                async with sem:
                    await self.documents_store.aput(
                        namespace=namespace,
                        key=key,
                        value=value,
                        index=["contextualized_content"],
                    )

            await asyncio.gather(
                *[
                    _aput_chunk(i, chunk_text, context_description)
                    for i, (chunk_text, context_description) in enumerate(chunks)
                ]
            )

            logger.info(f"Indexed {len(chunks)} chunks for '{file_path}' in namespace {namespace}")

        finally:
            # Flush accumulated embedding costs as a single log entry
            await flush_cost_batch(self._cost_logger)

            # Clean up context variables
            if user_sub:
                set_request_user_sub(None)
                logger.debug(f"[COST TRACKING] Cleared user_sub context after indexing {file_path}")
            if conversation_id:
                set_request_conversation_id(None)
                logger.debug(f"[COST TRACKING] Cleared conversation_id context after indexing {file_path}")
