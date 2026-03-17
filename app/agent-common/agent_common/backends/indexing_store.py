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
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from deepagents.backends.protocol import WriteResult
from deepagents.backends.store import StoreBackend
from langchain.tools import ToolRuntime
from langgraph.store.postgres.aio import AsyncPostgresStore
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.cost_tracking.logger import (
    flush_cost_batch,
    set_request_conversation_id,
    set_request_user_sub,
    start_cost_batching,
)

from agent_common.core.model_factory import create_model
from agent_common.core.semantic_chunking import TITAN_EMBED_MAX_CHARS, chunk_with_context

logger = logging.getLogger(__name__)


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
        bedrock_region: AWS region for Bedrock model creation
        cost_logger: Optional CostLogger for reporting indexing costs
        namespace_factory: Optional callable for determining write namespace
    """

    def __init__(
        self,
        runtime: ToolRuntime,
        bedrock_region: str | None = None,
        cost_logger: Optional[CostLogger] = None,
        namespace_factory: Optional[Any] = None,
    ):
        """Initialize IndexingStoreBackend.

        Args:
            runtime: ToolRuntime with store and config
            bedrock_region: AWS region for Bedrock model creation
            cost_logger: Optional CostLogger for reporting LLM usage costs
            namespace_factory: Optional callable that takes BackendContext and returns
                namespace tuple. Used to scope file storage (read/write operations).
                If None, uses legacy assistant_id-based scoping.
        """
        # Pass namespace factory to parent StoreBackend
        # This ensures read operations (grep, read_file, etc.) are scoped correctly
        super().__init__(runtime, namespace=namespace_factory)
        self._bedrock_region = bedrock_region
        self.documents_store: AsyncPostgresStore = runtime.store  # type: ignore[assignment]
        self._cost_logger = cost_logger

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
        # Call parent to write file to filesystem namespace
        result = await super().awrite(file_path, content)

        # If write failed, don't attempt indexing
        if result.error:
            return result

        # Index the content asynchronously (don't fail write if indexing fails)
        try:
            await self._index_content(file_path, content)
            logger.info(f"Indexed {file_path} for semantic search")
        except Exception as e:
            # Log but don't fail the write operation
            logger.error(f"Failed to index {file_path}: {e}", exc_info=True)

        return result

    async def _index_content(self, file_path: str, content: str) -> None:
        """Index file content for semantic search.

        Performs semantic chunking with contextualization and stores
        chunks in the documents namespace with embeddings.

        Args:
            file_path: Path of the file
            content: File content to index
        """
        # Get user_id and user_sub from runtime metadata or tags
        metadata_dict = self.runtime.config.get("metadata", {})
        user_id = metadata_dict.get("user_id")
        user_sub = metadata_dict.get("user_sub")
        conversation_id = metadata_dict.get("conversation_id")

        # Fallback: try to extract user_sub from tags if not in metadata
        if not user_sub:
            tags = self.runtime.config.get("tags", [])
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("user_sub:"):
                    user_sub = tag.split(":", 1)[1]
                    logger.debug(f"[COST TRACKING] Extracted user_sub from tag: {user_sub}")
                    break

        # Fallback: try to extract conversation_id from tags if not in metadata
        if not conversation_id:
            tags = self.runtime.config.get("tags", [])
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
                f"embeddings won't be tracked. Config keys: {list(self.runtime.config.keys())}"
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

            # Determine namespace based on file path:
            # - /large_tool_results/: (conversation_id, "tool_results") - conversation-scoped
            # - /memories/: (user_id, "documents") - user-scoped
            # - /channel_memories/: (assistant_id, "documents") - channel-scoped
            if is_large_tool_result:
                conversation_id_ns = metadata_dict.get("conversation_id")
                if not conversation_id_ns:
                    logger.error(
                        f"Cannot index tool result {file_path}: conversation_id missing from metadata. "
                        f"Tool results require conversation context for proper scoping."
                    )
                    return
                namespace = (conversation_id_ns, "tool_results")
                logger.debug(f"Using conversation-scoped namespace for tool result: {namespace}")
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

            # Check if this file is already indexed to avoid re-chunking
            # This happens when docstore_search returns large results that get evicted
            existing_index = await self.documents_store.aget(namespace=namespace, key=file_path)

            if existing_index is not None:
                logger.info(
                    f"File '{file_path}' is already indexed with {existing_index.value.get('total_chunks', 0)} chunks. "
                    f"Skipping re-indexing to avoid redundant chunking."
                )
                return

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

            # Create model for contextualization (use haiku-4-5 for cost efficiency)
            model = create_model("claude-haiku-4-5", bedrock_region=self._bedrock_region)

            # Perform semantic chunking with contextualization
            chunks = await chunk_with_context(content, metadata, model, cost_logger=self._cost_logger)  # type: ignore[arg-type]

            logger.info(f"Generated {len(chunks)} semantic chunks for '{file_path}'")

            # Store the file index metadata (without embedding) as a lookup
            file_index_value = {
                "file_path": file_path,
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
