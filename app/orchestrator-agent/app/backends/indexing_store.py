"""IndexingStoreBackend: StoreBackend with automatic semantic indexing.

This backend extends StoreBackend to automatically index files written to
long-term storage (/memories/*) for semantic search. Files are stored in
the filesystem namespace and indexed chunks are stored in the documents
namespace for cross-assistant search.

Handles both:
1. Normal write_file/edit_file operations
2. Large tool result evictions from FilesystemMiddleware
"""

import logging
from datetime import datetime, timezone
from typing import Any

from deepagents.backends.protocol import WriteResult
from deepagents.backends.store import StoreBackend
from langchain.tools import ToolRuntime
from langgraph.store.postgres.aio import AsyncPostgresStore

from ..core.model_factory import create_model
from ..core.semantic_chunking import chunk_with_context

logger = logging.getLogger(__name__)


class IndexingStoreBackend(StoreBackend):
    """StoreBackend with automatic semantic indexing of written files.

    Extends StoreBackend to automatically index file content when written.
    Uses semantic chunking with Claude for contextualization and stores
    chunks in a separate documents namespace for semantic search.

    Architecture:
    - Files stored in: (assistant_id, "filesystem") or (user_id, "filesystem")
    - Indexed chunks stored in: (user_id, "documents")
    - Large tool results detected via path prefix: /large_tool_results/*

    Args:
        runtime: ToolRuntime providing store access and metadata
        agent_settings: AgentSettings for creating Claude model
    """

    def __init__(
        self,
        runtime: ToolRuntime,
        agent_settings: Any,
    ):
        """Initialize IndexingStoreBackend.

        Args:
            runtime: ToolRuntime with store and config
            agent_settings: AgentSettings for model creation
        """
        super().__init__(runtime)
        self.agent_settings = agent_settings
        self.documents_store: AsyncPostgresStore = runtime.store  # type: ignore[assignment]

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
        # Get user_id from runtime metadata
        metadata_dict = self.runtime.config.get("metadata", {})
        user_id = metadata_dict.get("user_id")

        if not user_id:
            logger.warning(f"No user_id in runtime config, cannot index {file_path}")
            return

        # Detect if this is a large tool result
        is_large_tool_result = file_path.startswith("/large_tool_results/")

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

        # Create model for contextualization
        model = create_model("claude-sonnet-4.5", self.agent_settings, thinking=False)

        # Perform semantic chunking with contextualization
        chunks = await chunk_with_context(content, metadata, model)  # type: ignore[arg-type]

        logger.info(f"Generated {len(chunks)} semantic chunks for '{file_path}'")

        # Store chunks in documents namespace (user-scoped for cross-assistant search)
        namespace = (user_id, "documents")

        # Filesystem namespace for user-scoped copy (for export tool)
        filesystem_namespace_user = (user_id, "filesystem")

        # Store the file in user-scoped filesystem namespace
        # Get the file data from the store (it was just written by parent)
        assistant_id = metadata_dict.get("assistant_id")
        if assistant_id:
            filesystem_namespace_read = (assistant_id, "filesystem")
            item = await self._get_store().aget(namespace=filesystem_namespace_read, key=file_path)
            if item:
                # Copy to user namespace
                await self._get_store().aput(
                    namespace=filesystem_namespace_user,
                    key=file_path,
                    value=item.value,
                )
                logger.debug(f"Copied {file_path} to user namespace {filesystem_namespace_user}")

        # Store the file index metadata (without embedding) as a lookup
        file_index_value = {
            "file_path": file_path,
            "total_chunks": len(chunks),
            "chunk_keys": [f"{file_path}#chunk_{i}" for i in range(len(chunks))],
            "filesystem_namespace": filesystem_namespace_user,
            "metadata": metadata,
        }
        await self.documents_store.aput(
            namespace=namespace,
            key=file_path,
            value=file_index_value,
        )

        # Store individual chunks with embeddings for semantic search
        for i, (chunk_text, context_description, chunk_embedding) in enumerate(chunks):
            key = f"{file_path}#chunk_{i}"
            value = {
                "file_path": file_path,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "content": chunk_text,
                "context_description": context_description,
                "metadata": metadata,
            }

            # Store with embedding for semantic search
            # Type ignore: LangGraph type stubs incorrectly require list[str] but accepts list[float]
            await self.documents_store.aput(
                namespace=namespace,
                key=key,
                value=value,
                index=chunk_embedding,  # type: ignore[arg-type]
            )

        logger.info(f"Indexed {len(chunks)} chunks for '{file_path}' in namespace {namespace}")
