"""Document store middleware for automatic semantic indexing of filesystem writes.

This middleware automatically indexes files written to long-term storage,
making them searchable via semantic search without requiring explicit indexing.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from typing_extensions import NotRequired

from ..core.model_factory import create_model
from ..core.semantic_chunking import chunk_with_context

logger = logging.getLogger(__name__)


class DocumentStoreState(AgentState):
    """Extended agent state with cross-namespace permission tracking.

    Tracks granular, conversation-scoped permissions for privacy-first
    cross-namespace access (e.g., Slack channel accessing personal files).

    Permission Design:
    - File-level: Set of approved file paths for read_personal_file tool
    - Search-level: Boolean flag for search_documents_rag with include_personal=True
    - Conversation-scoped: Permissions reset per thread_id (Slack thread)
    - Interrupt-based: Uses LangGraph interrupt() for explicit user consent
    """

    personal_file_read_permissions: NotRequired[set[str]]
    """Set of approved personal file paths for current conversation.
    
    Granted via read_personal_file tool interrupt → user approval.
    Format: {"path/to/file.py", "path/to/other.md", ...}
    Resets per conversation (thread_id isolation).
    """

    personal_search_permission: NotRequired[bool]
    """Permission to search personal documents in current conversation.
    
    Granted via search_documents_rag(include_personal=True) interrupt → user approval.
    When True, semantic search includes (user_id, "documents") namespace.
    Defaults to False, resets per conversation.
    """


class DocumentStoreMiddleware(AgentMiddleware[DocumentStoreState, Any]):
    """Middleware for automatic semantic indexing of filesystem writes.

    This middleware intercepts write_file and edit_file tool calls and automatically
    indexes the written content for semantic search. Files are indexed in the
    documents namespace: (user_id, "documents").

    The middleware only indexes files written to long-term storage (when filesystem
    middleware has long_term_memory=True).

    Large Tool Result Indexing:
    ---------------------
    When FilesystemMiddleware evicts large tool results (>80KB) to
    /large_tool_results/{tool_call_id}, this middleware automatically detects
    and indexes them for RAG retrieval. This enables the model to search and
    retrieve relevant chunks from large tool outputs without passing the full
    content in context.

    Flow:
    1. Tool executes → returns large result (>80KB)
    2. FilesystemMiddleware.awrap_tool_call():
       - Detects large size
       - Creates file at /large_tool_results/{tool_call_id}
       - Returns Command with files update + summary message
    3. DocumentStoreMiddleware.awrap_tool_call():
       - Detects /large_tool_results/* file creation in Command
       - Extracts content and applies semantic chunking
       - Stores chunks with metadata (source=tool_result, tool_call_id, etc.)
    4. Model receives compact summary
    5. Model can retrieve specific chunks via docstore_search if needed

    Namespace design:
    - Filesystem stores files in: (assistant_id, "filesystem") - per-assistant scope
    - Document indexing stores chunks in: (user_id, "documents") - per-user scope

    This allows documents to be searchable across all assistants for a user,
    while filesystem remains isolated per assistant.

    Args:
        agent_settings: AgentSettings for creating Claude model for contextualization
        user_id: User ID for document namespacing
    """

    def __init__(self, agent_settings: Any) -> None:
        """Initialize the document store middleware.

        Args:
            agent_settings: AgentSettings for model creation
        """
        self.agent_settings = agent_settings

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Wrap tool calls to automatically index filesystem writes.

        Intercepts write_file and edit_file calls. After the tool executes successfully,
        reads the file from the filesystem namespace and indexes it in the documents namespace.

        Also detects when FilesystemMiddleware evicts large tool results to
        /large_tool_results/{tool_call_id} and automatically indexes them for RAG.

        Args:
            request: The tool call request with tool_call, state, and runtime
            handler: Function to execute the tool

        Returns:
            The tool call result (ToolMessage or Command)
        """
        # Execute the tool first
        result = await handler(request)

        # Extract tool name for write_file/edit_file detection
        tool_name = request.tool_call.get("name", "")

        # Check if FilesystemMiddleware evicted a large tool result
        # This happens when result is a Command with files update containing /large_tool_results/*
        if isinstance(result, Command) and result.update:
            files_update = result.update.get("files", {})
            for file_path, file_data in files_update.items():
                if file_path.startswith("/large_tool_results/"):
                    # Extract tool_call_id from path
                    tool_call_id = file_path.split("/")[-1]
                    logger.info(f"Detected large tool result eviction: {file_path} (tool_call_id: {tool_call_id})")

                    # Index this evicted tool result
                    await self._index_evicted_tool_result(
                        file_path=file_path,
                        file_data=file_data,
                        tool_call_id=tool_call_id,
                        runtime=request.runtime,
                    )

        # Only intercept write_file and edit_file for normal file indexing
        if tool_name not in ["write_file", "edit_file"]:
            return result

        # Only index if this is a write/edit to long-term storage and we have a store
        if not request.runtime or not request.runtime.store:
            return result

        # Check if the tool call was successful (ToolMessage without error)
        if isinstance(result, ToolMessage):
            # Check for error in content or status
            if result.status == "error":
                logger.debug(f"Skipping indexing for failed {tool_name} call")
                return result
        else:
            # If it's a Command, we don't index
            return result

        # Extract file path from tool call
        args = request.tool_call.get("args", {})
        file_path = args.get("file_path")

        if not file_path:
            logger.warning(f"No file_path in {tool_name} call, skipping indexing")
            return result

        # Read the file from where FilesystemMiddleware stores it
        # FilesystemMiddleware uses (assistant_id, "filesystem")
        metadata = request.runtime.config.get("metadata", {})
        assistant_id = metadata.get("assistant_id")
        user_id = metadata.get("user_id")

        if not assistant_id or not user_id:
            logger.warning(f"Missing assistant_id or user_id in runtime config, skipping indexing for {file_path}")
            return result

        filesystem_namespace_read = (assistant_id, "filesystem")
        # For document system, we'll store a reference to user-scoped location
        filesystem_namespace_user = (user_id, "filesystem")

        try:
            # Read the file from where FilesystemMiddleware stored it
            item = await request.runtime.store.aget(namespace=filesystem_namespace_read, key=file_path)
            if not item:
                logger.warning(f"File {file_path} not found in {filesystem_namespace_read} after {tool_name}")
                return result

            # Extract content
            value = item.value
            content_lines = value.get("content", [])
            if not content_lines:
                logger.warning(f"Empty content for {file_path}")
                return result

            content = "\n".join(content_lines)

            # Copy the file to user-scoped namespace for secure access
            # This ensures user-level isolation for document system
            await request.runtime.store.aput(
                namespace=filesystem_namespace_user,
                key=file_path,
                value=value,  # Same FileData structure
            )
            logger.debug(f"Copied {file_path} to user namespace {filesystem_namespace_user}")

        except Exception as e:
            logger.error(f"Failed to process {file_path}: {e}")
            return result

        # Index the content asynchronously (don't block on it)
        try:
            await self._index_file(file_path, content, request.runtime)
            logger.info(f"Indexed {file_path} for semantic search")
        except Exception as e:
            # Log but don't fail the tool call if indexing fails
            logger.error(f"Failed to index {file_path}: {e}", exc_info=True)

        return result

    async def _index_file(self, file_path: str, content: str, runtime: Any) -> None:
        """Index a file for semantic search.

        Args:
            file_path: Path of the file
            content: File content
            runtime: LangGraph runtime (ToolRuntime) with store access
        """
        if not runtime or not runtime.store:
            logger.warning("No store available in runtime, cannot index file")
            return

        # Create model for contextualization
        # Note: create_model with "claude-sonnet-4.5" always returns ChatBedrockConverse
        model = create_model("claude-sonnet-4.5", self.agent_settings, thinking=False)

        # Prepare metadata
        metadata = {
            "file_path": file_path,
            "source": "filesystem",
        }

        # Perform semantic chunking with contextualization
        # Type assertion: create_model("claude-sonnet-4.5") always returns ChatBedrockConverse
        chunks = await chunk_with_context(content, metadata, model)  # type: ignore[arg-type]

        logger.info(f"Generated {len(chunks)} semantic chunks for '{file_path}'")

        # Get user_id from runtime metadata for namespace
        user_id = runtime.config.get("metadata", {}).get("user_id")
        if not user_id:
            logger.warning(f"No user_id in runtime config, cannot index {file_path}")
            return

        # Store chunks in documents namespace (user-scoped for cross-assistant search)
        namespace = (user_id, "documents")

        # Filesystem namespace for user-scoped copy
        filesystem_namespace_user = (user_id, "filesystem")

        # First, store the file index metadata (without embedding) as a lookup for export
        file_index_value = {
            "file_path": file_path,
            "total_chunks": len(chunks),
            "chunk_keys": [f"{file_path}#chunk_{i}" for i in range(len(chunks))],
            "filesystem_namespace": filesystem_namespace_user,  # Points to user-scoped copy
            "metadata": metadata,
        }
        await runtime.store.aput(
            namespace=namespace,
            key=file_path,  # Use file_path as key for direct lookup
            value=file_index_value,
        )

        # Then store individual chunks with embeddings for semantic search
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
            # The embedding vector from chunk_embedding is passed via index parameter
            await runtime.store.aput(
                namespace=namespace,
                key=key,
                value=value,
                index=chunk_embedding,  # Pass embedding for similarity search
            )

        logger.info(f"Indexed {len(chunks)} chunks for '{file_path}' in namespace {namespace}")

    async def _index_evicted_tool_result(
        self,
        file_path: str,
        file_data: dict[str, Any],
        tool_call_id: str,
        runtime: Any,
    ) -> None:
        """Index an evicted tool result for semantic search.

        When FilesystemMiddleware evicts a large tool result (>80KB) to
        /large_tool_results/{tool_call_id}, this method automatically indexes
        it for RAG retrieval.

        Args:
            file_path: Path of the evicted file (e.g., /large_tool_results/{tool_call_id})
            file_data: FileData structure with content
            tool_call_id: Tool call ID extracted from path
            runtime: LangGraph runtime (ToolRuntime) with store access
        """
        if not runtime or not runtime.store:
            logger.warning("No store available in runtime, cannot index evicted tool result")
            return

        # Extract content from FileData structure
        content_lines = file_data.get("content", [])
        if not content_lines:
            logger.warning(f"Empty content for evicted tool result: {file_path}")
            return

        content = "\n".join(content_lines)
        original_size = len(content)

        logger.info(f"Indexing evicted tool result: {file_path} (size: {original_size} bytes)")

        # Get user_id from runtime metadata
        user_id = runtime.config.get("metadata", {}).get("user_id")
        if not user_id:
            logger.warning(f"No user_id in runtime config, cannot index {file_path}")
            return

        # Create model for contextualization
        model = create_model("claude-sonnet-4.5", self.agent_settings, thinking=False)

        # Prepare metadata with tool result information
        from datetime import datetime, timezone

        metadata = {
            "file_path": file_path,
            "source": "tool_result",
            "tool_call_id": tool_call_id,
            "original_size": original_size,
            "evicted_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        # Perform semantic chunking with contextualization
        chunks = await chunk_with_context(content, metadata, model)  # type: ignore[arg-type]

        logger.info(f"Generated {len(chunks)} semantic chunks for tool result '{file_path}'")

        # Store chunks in documents namespace (user-scoped for cross-assistant search)
        namespace = (user_id, "documents")

        # Filesystem namespace for user-scoped storage
        filesystem_namespace_user = (user_id, "filesystem")

        # Store the file in user-scoped filesystem namespace
        await runtime.store.aput(
            namespace=filesystem_namespace_user,
            key=file_path,
            value=file_data,
        )
        logger.debug(f"Stored evicted tool result in user namespace: {filesystem_namespace_user}")

        # Store the file index metadata (without embedding) as a lookup for export
        file_index_value = {
            "file_path": file_path,
            "total_chunks": len(chunks),
            "chunk_keys": [f"{file_path}#chunk_{i}" for i in range(len(chunks))],
            "filesystem_namespace": filesystem_namespace_user,
            "metadata": metadata,
        }
        await runtime.store.aput(
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
            await runtime.store.aput(
                namespace=namespace,
                key=key,
                value=value,
                index=chunk_embedding,
            )

        logger.info(f"Indexed {len(chunks)} chunks for tool result '{file_path}' in namespace {namespace}")
