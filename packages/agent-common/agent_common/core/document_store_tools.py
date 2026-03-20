"""Document store tools for semantic search over persisted filesystem files.

These tools integrate with the IndexingStoreBackend to provide semantic search
capabilities. Files written to long-term storage (/memories/*) are automatically
indexed by the IndexingStoreBackend.

Provides two tools:
1. docstore_search: Search indexed files using semantic similarity
2. docstore_export: Export files (ephemeral or persisted) to S3 with presigned URLs

All tools use AsyncPostgresStore with pgvector for semantic search.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from langchain.agents.middleware.types import AgentState
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_config
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import interrupt
from langsmith import traceable
from typing_extensions import NotRequired

from agent_common.core.s3_service import S3Service


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


class FilesystemState(dict):
    """State schema for accessing ephemeral files."""

    files: dict[str, Any]  # FileData structures


logger = logging.getLogger(__name__)


def _get_filesystem_namespace() -> tuple[str] | tuple[str, str]:
    """Get the namespace for filesystem storage (channel or personal workspace).

    Returns a tuple for organizing files in the store. If an assistant_id is available
    in the config metadata, returns a 2-tuple of (assistant_id, "filesystem") to provide
    per-assistant isolation. Otherwise, returns a 1-tuple of ("filesystem",) for shared storage.

    This follows the same logic as FilesystemMiddleware._get_namespace().

    Returns:
        Namespace tuple for store operations, either `(assistant_id, "filesystem")` or `("filesystem",)`.
    """
    namespace = "filesystem"
    config = get_config()
    if config is None:
        return (namespace,)
    assistant_id = config.get("metadata", {}).get("assistant_id")
    if assistant_id is None:
        return (namespace,)
    return (assistant_id, "filesystem")


def _get_personal_namespace(user_id: str | None = None) -> tuple[str, str] | None:
    """Get the personal workspace namespace for a specific user.

    Personal workspace is user-scoped storage, separate from channel workspaces.
    Each user has their own isolated (user_id, "filesystem") namespace.

    Args:
        user_id: User ID to get namespace for. If None, uses current user from config.

    Returns:
        (user_id, "filesystem") if user_id is available, None otherwise.
    """
    if user_id is None:
        config = get_config()
        if config is None:
            return None
        user_id = config.get("metadata", {}).get("user_id")

    if user_id is None:
        return None

    return (user_id, "filesystem")


def _is_cross_namespace_access() -> bool:
    """Check if current context would require cross-namespace file access.

    Cross-namespace access occurs when:
    - User is in a Slack channel (scope="channel")
    - And they want to access their personal files (different namespace)

    This requires explicit permission via interrupt() for privacy.

    Returns:
        True if accessing personal files from channel context, False otherwise.
    """
    config = get_config()
    if config is None:
        return False

    metadata = config.get("metadata", {})
    scope = metadata.get("scope")

    # If we're in a channel, accessing personal files is cross-namespace
    return scope == "channel"


async def _read_personal_file_impl(
    file_path: str,
    target_user_id: str,
    store: AsyncPostgresStore,
    state: DocumentStoreState,
) -> str:
    """Implementation of reading a personal file with permission checking.

    Checks if cross-namespace access is needed, verifies permissions in state,
    requests permission via interrupt if not granted, then reads the file.

    Args:
        file_path: Path to the personal file
        target_user_id: User ID whose personal file to read
        store: AsyncPostgresStore instance
        state: Graph state with permission tracking

    Returns:
        File content or error message
    """
    # Check if this is cross-namespace access
    if not _is_cross_namespace_access():
        return "Error: read_personal_file is only for accessing personal files from channel context."

    # Get personal namespace
    personal_namespace = _get_personal_namespace(target_user_id)
    if not personal_namespace:
        return f"Error: Could not determine personal namespace for user {target_user_id}"

    # Check if permission already granted for this file
    granted_files = state.get("personal_file_read_permissions", set())
    if file_path not in granted_files:
        # Request permission via interrupt
        config = get_config()
        user_name = config.get("metadata", {}).get("user_name") if config else None
        mention = f"@{user_name}" if user_name else "the user"

        permission_request = {
            "type": "file_permission_request",
            "message": f"Allow access to {mention}'s personal file: {file_path}?",
            "file_path": file_path,
            "user_id": target_user_id,
        }

        response = interrupt(permission_request)

        if response != "approve":
            return f"Access denied to personal file: {file_path}"

        # Permission granted, update state
        granted_files.add(file_path)
        state["personal_file_read_permissions"] = granted_files

    # Read the file
    try:
        item = await store.aget(namespace=personal_namespace, key=file_path)
        if not item:
            return f"File not found in personal workspace: {file_path}"

        value = item.value
        content_lines = value.get("content", [])
        if not content_lines:
            return f"File is empty: {file_path}"

        content = "\n".join(content_lines)
        logger.info(f"Read personal file with permission: {file_path}")
        return content

    except Exception as e:
        logger.error(f"Failed to read personal file {file_path}: {e}")
        return f"Error reading file: {str(e)}"


async def _search_documents_rag_impl(
    query: str,
    top_k: int,
    user_id: str,
    store: AsyncPostgresStore,
    include_personal: bool = False,
    state: DocumentStoreState | None = None,
) -> list[dict[str, Any]]:
    """Implementation of semantic search over indexed filesystem files.

    Searches files that have been indexed by IndexingStoreBackend when
    written to long-term storage (/memories/* or /channel_memories/*). Searches user-scoped
    or channel-scoped namespace depending on context.

    Returns documents in LangChain retriever format for proper LangSmith tracing.

    Args:
        query: Search query
        top_k: Number of results
        user_id: User ID for namespacing
        store: AsyncPostgresStore instance
        include_personal: If True and in channel context, search personal documents too
        state: Graph state for permission tracking (required if include_personal=True)

    Returns:
        List of documents in LangChain format with page_content, type, and metadata
    """
    # Check personal search permission if requested in channel context
    if include_personal and _is_cross_namespace_access():
        if state is None:
            logger.error("State required for personal search permission checking")
            return []

        # Check if permission already granted
        has_permission = state.get("personal_search_permission", False)
        if not has_permission:
            # Request permission via interrupt
            config = get_config()
            user_name = config.get("metadata", {}).get("user_name") if config else None
            mention = f"@{user_name}" if user_name else "the user"

            permission_request = {
                "type": "search_permission_request",
                "message": f"Allow semantic search across {mention}'s personal documents?",
                "user_id": user_id,
            }

            response = interrupt(permission_request)

            if response != "approve":
                logger.info("Access denied to personal documents")
                return []

            # Permission granted, update state
            state["personal_search_permission"] = True

    logger.info(
        f"Semantic search for indexed files, user {user_id}: '{query}' (top_k={top_k}, include_personal={include_personal})"
    )

    # Hybrid search: current conversation tool results + persistent user/channel documents
    all_results = []

    # Search conversation-scoped tool results
    config = get_config()
    conversation_id = config.get("metadata", {}).get("conversation_id") if config else None
    if conversation_id:
        tool_results_namespace = (conversation_id, "tool_results")
        tool_results = await store.asearch(
            tool_results_namespace,
            query=query,
            limit=top_k,
        )
        all_results.extend(tool_results)
        logger.debug(f"Found {len(tool_results)} tool result chunks in conversation {conversation_id}")

    # Determine which document namespaces to search based on context
    config = get_config()
    metadata = config.get("metadata", {}) if config else {}
    scope = metadata.get("scope", "personal")
    assistant_id = metadata.get("assistant_id")

    if scope == "channel" and assistant_id:
        # Channel context: search channel documents (assistant-scoped)
        channel_docs_namespace = (assistant_id, "documents")
        channel_docs = await store.asearch(
            channel_docs_namespace,
            query=query,
            limit=top_k,
        )
        all_results.extend(channel_docs)
        logger.debug(f"Found {len(channel_docs)} channel document chunks")

        # Also search personal documents if requested and permitted
        if include_personal:
            user_docs_namespace = (user_id, "documents")
            user_docs = await store.asearch(
                user_docs_namespace,
                query=query,
                limit=top_k,
            )
            all_results.extend(user_docs)
            logger.debug(f"Found {len(user_docs)} personal document chunks")
    else:
        # Personal context: search user documents only
        user_docs_namespace = (user_id, "documents")
        user_docs = await store.asearch(
            user_docs_namespace,
            query=query,
            limit=top_k,
        )
        all_results.extend(user_docs)
        logger.debug(f"Found {len(user_docs)} user document chunks")

    if not all_results:
        return []

    # Sort combined results by score (descending) and take top_k
    all_results.sort(key=lambda x: x.score if hasattr(x, "score") and x.score else 0, reverse=True)
    results = all_results[:top_k]

    # Format results in LangChain retriever format
    documents = []
    for item in results:
        value = item.value
        file_path = value.get("file_path", "unknown")
        chunk_index = value.get("chunk_index", 0)
        total_chunks = value.get("total_chunks", 1)
        content = value.get("content", "")
        context_desc = value.get("context_description", "")

        documents.append(
            {
                "page_content": content,
                "type": "Document",
                "metadata": {
                    "file_path": file_path,
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                    "context_description": context_desc,
                    "score": item.score if hasattr(item, "score") else None,
                },
            }
        )

    logger.info(f"RAG search returned {len(documents)} chunks")
    return documents


async def _export_file_impl(
    file_path: str,
    user_id: str,
    store: AsyncPostgresStore,
    s3_service: S3Service,
    s3_bucket: str,
) -> str:
    """Export a file from filesystem to S3.

    Reads persisted files from store and exports to S3. Works independently of document indexing.
    Handles both personal (/memories/) and channel (/channel_memories/) files.

    Args:
        file_path: Path of the file to export (should start with /memories/ or /channel_memories/)
        user_id: User ID for S3 key generation
        store: AsyncPostgresStore instance for reading files
        s3_service: S3 service for uploads
        s3_bucket: S3 bucket name for result storage

    Returns:
        Presigned URL for downloaded file
    """
    logger.info(f"Exporting file '{file_path}' for user {user_id}")

    # Determine namespace and file key based on path prefix
    if file_path.startswith("/memories/"):
        # Personal storage: user-scoped namespace
        namespace = (user_id, "filesystem")
        file_key = file_path[len("/memories/") - 1 :]  # Keep leading slash
    elif file_path.startswith("/channel_memories/"):
        # Channel storage: assistant-scoped namespace
        config = get_config()
        assistant_id = config.get("metadata", {}).get("assistant_id") if config else None
        if not assistant_id:
            return "Error: /channel_memories/ only available in channel contexts"
        namespace = (assistant_id, "filesystem")
        file_key = file_path[len("/channel_memories/") - 1 :]  # Keep leading slash
    else:
        return (
            f"Error: Can only export persisted files (starting with /memories/ or /channel_memories/). "
            f"File '{file_path}' is ephemeral and cannot be exported. "
            f"Use write_file to persist it first."
        )

    try:
        file_item = await store.aget(namespace, file_key)

        if not file_item:
            return (
                f"Error: File '{file_path}' not found in filesystem storage. Make sure the file exists using ls tool."
            )

        # Extract file content from FileData structure in store
        value = file_item.value
        content_lines = value.get("content", [])

        if not content_lines:
            return f"Error: File '{file_path}' is empty"

        # Join content lines (same format as FilesystemMiddleware)
        file_content = "\n".join(content_lines)

    except Exception as e:
        logger.error(f"Failed to read persisted file '{file_path}' from filesystem: {e}")
        return f"Error: Could not read file '{file_path}': {str(e)}"

    # Determine content type from file extension
    content_type = "text/plain"
    if file_path.endswith(".md"):
        content_type = "text/markdown"
    elif file_path.endswith(".json"):
        content_type = "application/json"
    elif file_path.endswith(".py"):
        content_type = "text/x-python"
    elif file_path.endswith((".txt", ".log")):
        content_type = "text/plain"

    # Upload to S3
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_name = file_path.split("/")[-1]  # Get basename
    s3_key = f"docstore/exports/{user_id}/{timestamp}_{file_name}"
    s3_uri = f"s3://{s3_bucket}/{s3_key}"

    await s3_service.upload_content(
        content=file_content.encode("utf-8"),
        bucket=s3_bucket,
        key=s3_key,
        content_type=content_type,
    )

    logger.info(f"Exported '{file_path}' to {s3_uri}")

    # Generate presigned URL (24 hours)
    presigned_url = await s3_service.generate_presigned_url(s3_uri, expiration=86400)

    return f"Exported file '{file_path}' to S3.\n\nDownload link (expires in 24 hours):\n{presigned_url}"


def create_document_store_tools(
    store: AsyncPostgresStore,
    s3_service: S3Service,
    s3_bucket: str,
    user_id: str,
) -> list[BaseTool]:
    """Create document store tools for semantic search, personal file access, and export.

    Documents are stored in user-scoped namespace (user_id, "documents") to enable
    cross-assistant document search, while filesystem remains assistant-scoped.

    Includes privacy-first cross-namespace access with interrupt-based permissions.

    Args:
        store: AsyncPostgresStore instance
        s3_service: S3 service for file exports
        s3_bucket: S3 bucket for storing exported files
        user_id: User ID for document namespacing and S3 keys

    Returns:
        List of document store tools (search, read_personal_file, export)
    """

    @traceable(run_type="retriever")
    async def _search_documents_retriever(
        query: str,
        top_k: int = 5,
        include_personal: bool = False,
        state: DocumentStoreState | None = None,
    ) -> list[dict[str, Any]]:
        """Retriever function that returns documents in LangChain format for tracing.

        This function is traced as a retriever step in LangSmith, providing
        proper visualization of retrieved documents.
        """
        return await _search_documents_rag_impl(
            query=query,
            top_k=top_k,
            user_id=user_id,
            store=store,
            include_personal=include_personal,
            state=state,
        )

    def _format_documents_for_llm(documents: list[dict[str, Any]]) -> str:
        """Format retrieved documents as a string for LLM consumption."""
        if not documents:
            return "No relevant documents found."

        context_parts = []
        for i, doc in enumerate(documents, 1):
            metadata = doc["metadata"]
            content = doc["page_content"]
            file_path = metadata.get("file_path", "unknown")
            chunk_index = metadata.get("chunk_index", 0)
            total_chunks = metadata.get("total_chunks", 1)
            context_desc = metadata.get("context_description", "")

            context_parts.append(
                f"[Result {i}] {file_path} (chunk {chunk_index + 1}/{total_chunks})\n"
                f"Context: {context_desc}\n"
                f"Content:\n{content}\n"
            )

        formatted_context = "\n---\n\n".join(context_parts)
        return f"Found {len(documents)} relevant chunks:\n\n{formatted_context}"

    async def search_documents_rag(
        query: str,
        top_k: int = 5,
        include_personal: bool = False,
        runtime=None,
    ) -> str:
        """Search files you've written to long-term storage using semantic similarity.

        This searches indexed versions of files you've written using write_file or edit_file.
        Files are automatically indexed when written to long-term storage for semantic search.

        Use this when you need to:
        - Find relevant information across multiple files you've created
        - Search by meaning, not just filename
        - Get context from past work for answering questions

        In Slack channels, set include_personal=True to search the user's personal documents
        (requires explicit permission via interrupt).

        Returns formatted context chunks that you can use to synthesize answers.

        Args:
            query: Natural language search query describing what you're looking for
            top_k: Number of relevant chunks to retrieve (default 5)
            include_personal: Search personal documents (channel context only, requires permission)
            runtime: Tool runtime (automatically injected, required if include_personal=True)

        Returns:
            Formatted context with relevant content from indexed files
        """
        state = runtime.state if runtime else None
        documents = await _search_documents_retriever(
            query=query,
            top_k=top_k,
            include_personal=include_personal,
            state=state,
        )
        return _format_documents_for_llm(documents)

    async def read_personal_file(
        file_path: str,
        runtime,
    ) -> str:
        """Read a file from a user's personal workspace with permission.

        Only available in Slack channel context. Requires explicit user permission
        via interrupt before accessing personal files for privacy.

        Use this when:
        - User mentions their personal files/notes in a channel
        - You need to reference user's private work in channel discussion
        - User explicitly asks to share their personal file content

        Permission is conversation-scoped (per Slack thread) for privacy.

        Args:
            file_path: Path to the personal file (e.g., "/my-notes.md")
            runtime: Tool runtime (automatically injected) for state access and permission tracking

        Returns:
            File content if permission granted, error message if denied
        """
        # Get state with type safety
        state: DocumentStoreState = runtime.state if runtime.state is not None else {}  # type: ignore[assignment]
        return await _read_personal_file_impl(
            file_path=file_path,
            target_user_id=user_id,
            store=store,
            state=state,
        )

    async def docstore_export(
        file_path: str,
    ) -> str:
        """Export a file from the filesystem to S3 for download.

        Exports files directly from filesystem storage (both ephemeral and persisted files)
        to S3 with a presigned download URL. Works with any file visible via ls tool,
        independent of whether the file has been indexed for semantic search.

        This tool builds upon the default LangGraph filesystem storage functionality,
        reading files from the same namespace where write_file stores them.

        Use this when the user wants to:
        - Download any file from the workspace (ephemeral or persisted)
        - Share a file outside the conversation
        - Get a permanent download link

        Args:
            file_path: Path of the file to export (from ls output, e.g., /myfile.txt or /memories/notes.md)

        Returns:
            Presigned S3 URL (24h expiration) to download the file
        """
        return await _export_file_impl(
            file_path=file_path,
            user_id=user_id,
            store=store,
            s3_service=s3_service,
            s3_bucket=s3_bucket,
        )

    return [
        StructuredTool.from_function(
            coroutine=search_documents_rag,
            name="docstore_search",
            description=(
                "Search files you've written to long-term storage using semantic similarity. "
                "This searches indexed versions of files created with write_file or edit_file, plus large tool results from this conversation. "
                "Files are automatically indexed when written to /memories/ (personal) or /channel_memories/ (shared). "
                "In personal context: searches your /memories/ files. "
                "In Slack channels: searches /channel_memories/ files shared with the channel. Use include_personal=True to also search your personal /memories/ files (requires permission). "
                "Use when: you need to find information across multiple files by meaning (not just filename), "
                "retrieve context from past work or previous tool results to answer questions. "
                "Returns formatted context chunks for synthesizing answers."
            ),
        ),
        StructuredTool.from_function(
            coroutine=read_personal_file,
            name="read_personal_file",
            description=(
                "Read a file from user's personal workspace (Slack channel context only). "
                "Requires explicit user permission via interrupt for privacy. "
                "Permission is conversation-scoped (per Slack thread). "
                "Use when: user mentions their personal files in a channel, "
                "you need to reference user's private work in discussion, "
                "or user explicitly asks to share personal file content."
            ),
        ),
        StructuredTool.from_function(
            coroutine=docstore_export,
            name="docstore_export",
            description=(
                "Export a persisted file from /memories/ (personal) or /channel_memories/ (shared) to S3 for download. "
                "Only works with files in long-term storage (starting with /memories/ or /channel_memories/). "
                "Ephemeral files must be persisted with write_file first. "
                "Use when: user wants to download/share a persisted file from the workspace. "
                "Returns a presigned S3 URL (24h expiration) to download the file."
            ),
        ),
    ]
