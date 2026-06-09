"""Document store tools for semantic search over persisted filesystem files.

These tools integrate with the IndexingStoreBackend to provide semantic search
capabilities. Files written to long-term storage (/memories/*) are automatically
indexed by the IndexingStoreBackend.

Provides tools:
1. docstore_search: Search durable memory (indexed /memories/ and /channel_memories/ files) by similarity
2. semantic_search_file: Index + semantic-search WITHIN a single large in-hand file (e.g. evicted tool result)
3. read_personal_file: Read files from a user's personal workspace (HITL-guarded, channel-only)
4. docstore_export: Export files (ephemeral or persisted) to S3 with presigned URLs

Privacy controls:
- read_personal_file is guarded by HumanInTheLoopMiddleware (always requires approval)
- docstore_search with include_personal=True is conditionally guarded by
  ConditionalHumanInTheLoopMiddleware (requires approval only when the flag is set)

All tools use AsyncPostgresStore with pgvector for semantic search.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.config import get_config
from langgraph.store.postgres.aio import AsyncPostgresStore
from langsmith import traceable
from object_storage import IObjectStorageService
from ringier_a2a_sdk.cost_tracking import CostLogger

from agent_common.backends.attachments_store import get_current_attachments_backend
from agent_common.backends.indexing_store import IndexingStoreBackend


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
) -> str:
    """Implementation of reading a personal file.

    Permission is handled by HumanInTheLoopMiddleware (HITL guards) before
    this tool is executed. By the time we reach here, the user has already
    approved the file access.

    Args:
        file_path: Path to the personal file
        target_user_id: User ID whose personal file to read
        store: AsyncPostgresStore instance

    Returns:
        File content or error message
    """
    # Defense-in-depth: the tool is only injected into the model's toolset in
    # channel context by ConversationContextToolsMiddleware, so it should never be callable outside a channel.
    # This check stays as a backstop in case the tool is reached through another path.
    if not _is_cross_namespace_access():
        raise ToolException(
            "read_personal_file is only available in Slack channel context. "
            "You are NOT in a channel. Do NOT call this tool again. "
            "Use docstore_search or read_file instead to access files."
        )

    # Get personal namespace
    personal_namespace = _get_personal_namespace(target_user_id)
    if not personal_namespace:
        return f"Error: Could not determine personal namespace for user {target_user_id}"

    # Read the file (permission already granted via HITL middleware)
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
) -> list[dict[str, Any]]:
    """Implementation of semantic search over indexed filesystem files.

    Searches files that have been indexed by IndexingStoreBackend when
    written to long-term storage (/memories/* or /channel_memories/*). Searches user-scoped
    or channel-scoped namespace depending on context.

    Permission for include_personal=True in channel context is handled by
    ConditionalHumanInTheLoopMiddleware (HITL guards) before this tool is
    executed. By the time we reach here, the user has already approved.

    Returns documents in LangChain retriever format for proper LangSmith tracing.

    Args:
        query: Search query
        top_k: Number of results
        user_id: User ID for namespacing
        store: AsyncPostgresStore instance
        include_personal: If True and in channel context, search personal documents too

    Returns:
        List of documents in LangChain format with page_content, type, and metadata
    """
    logger.info(
        f"Semantic search for indexed files, user {user_id}: '{query}' (top_k={top_k}, include_personal={include_personal})"
    )

    # Conversation tool results are NOT searched here — those are evicted /large_tool_results/ blobs
    # that can be indexed and searched on-demand via `semantic_search_file`.
    all_results = []

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


def _resolve_namespaces_for_path(
    file_path: str,
    user_id: str,
    metadata: dict[str, Any],
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """Resolve (filesystem, chunk) namespaces for an in-hand file path.

    Mirrors the routing in ``IndexingStoreBackend._index_content`` so that
    ``semantic_search_file`` reads the file from the same filesystem namespace
    it was written to and searches the same chunk namespace it is indexed into.

    Args:
        file_path: Absolute path of the in-hand file.
        user_id: Current user id (for /memories/ default route).
        metadata: Runtime config metadata (conversation_id, assistant_id).

    Returns:
        ``(filesystem_namespace, chunk_namespace)`` or ``None`` when the
        required scoping id is missing from metadata.
    """
    if file_path.startswith("/large_tool_results/"):
        conversation_id = metadata.get("conversation_id")
        if not conversation_id:
            return None
        return (conversation_id, "filesystem"), (conversation_id, "tool_results")
    if file_path.startswith("/attachments/"):
        # Attachments are conversation-scoped, ephemeral files. Their content is
        # not stored in PostgreSQL (it lives in the in-memory attachments
        # backend), so the filesystem namespace is unused — content is read via
        # the attachments backend in ``_semantic_search_file_impl``. JIT-indexed
        # chunks share the conversation's ``tool_results`` namespace.
        conversation_id = metadata.get("conversation_id")
        if not conversation_id:
            return None
        return (conversation_id, "filesystem"), (conversation_id, "tool_results")
    if file_path.startswith("/channel_memories/"):
        assistant_id = metadata.get("assistant_id")
        if not assistant_id:
            return None
        return (assistant_id, "filesystem"), (assistant_id, "documents")
    # Default: personal /memories/ files
    return (user_id, "filesystem"), (user_id, "documents")


async def _semantic_search_file_impl(
    file_path: str,
    query: str,
    top_k: int,
    user_id: str,
    store: AsyncPostgresStore,
    model_name: str | None = None,
    cost_logger: CostLogger | None = None,
) -> list[dict[str, Any]]:
    """Index a single in-hand file on demand and semantic-search within it.

    This is the lazy (JIT) counterpart to the eager indexing done by
    ``IndexingStoreBackend.awrite``. Evicted ``/large_tool_results/`` blobs are
    intentionally NOT eagerly indexed (see core/CONTEXT.md D5/D9); this tool
    reads the in-hand file, ensures it is chunked + embedded (idempotent via
    content-hash caching), then runs a semantic search scoped to just that file.

    Args:
        file_path: Path of the in-hand file (e.g. an evicted tool result).
        query: Natural language search query.
        top_k: Number of matching chunks to return.
        user_id: User id for /memories/ routing.
        store: AsyncPostgresStore instance.
        model_name: Model to use for indexing/chunking. When ``None``,
            ``IndexingStoreBackend`` selects the cheapest available model via
            ``get_default_indexing_model()``.
        cost_logger: Optional CostLogger for indexing cost attribution.

    Returns:
        List of documents in LangChain retriever format (page_content, type, metadata).
    """
    config = get_config()
    metadata = config.get("metadata", {}) if config else {}

    namespaces = _resolve_namespaces_for_path(file_path, user_id, metadata)
    if namespaces is None:
        raise ToolException(f"Cannot scope '{file_path}' for semantic search: missing conversation/assistant context.")
    filesystem_namespace, chunk_namespace = namespaces

    # Read the in-hand file content. Attachments live in the ephemeral in-memory
    # attachments backend (not PostgreSQL), so they are read from the per-turn
    # backend registered in the context variable. Everything else is read from
    # its filesystem namespace in the store.
    if file_path.startswith("/attachments/"):
        attachments_backend = get_current_attachments_backend()
        content = await attachments_backend.aread_text(file_path) if attachments_backend else None
        if content is None:
            raise ToolException(
                f"File not found: '{file_path}'. Use ls/read_file to confirm the path before searching it."
            )
    else:
        item = await store.aget(namespace=filesystem_namespace, key=file_path)
        if item is None:
            raise ToolException(
                f"File not found: '{file_path}'. Use ls/read_file to confirm the path before searching it."
            )
        value = item.value
        content_lines = value.get("content", [])
        content = "\n".join(content_lines) if isinstance(content_lines, list) else str(content_lines)
    if not content.strip():
        return []

    # JIT index (idempotent via content-hash caching in _index_content)
    backend = IndexingStoreBackend(store=store, model_name=model_name, cost_logger=cost_logger)
    await backend.aensure_indexed(file_path, content)

    # Search the chunk namespace, filtered to this specific file. Over-fetch so
    # filtering by file_path still yields up to top_k results when the namespace
    # contains chunks from other files.
    raw_results = await store.asearch(chunk_namespace, query=query, limit=max(top_k * 4, top_k))
    file_results = [r for r in raw_results if r.value.get("file_path") == file_path][:top_k]

    documents = []
    for result in file_results:
        rvalue = result.value
        documents.append(
            {
                "page_content": rvalue.get("content", ""),
                "type": "Document",
                "metadata": {
                    "file_path": rvalue.get("file_path", file_path),
                    "chunk_index": rvalue.get("chunk_index", 0),
                    "total_chunks": rvalue.get("total_chunks", 1),
                    "context_description": rvalue.get("context_description", ""),
                    "score": result.score if hasattr(result, "score") else None,
                },
            }
        )

    logger.info(f"semantic_search_file returned {len(documents)} chunks for '{file_path}'")
    return documents


async def _export_file_impl(
    file_path: str,
    user_id: str,
    store: AsyncPostgresStore,
    storage: IObjectStorageService,
    s3_bucket: str,
) -> str:
    """Export a file from filesystem to object storage.

    Reads persisted files from store and exports to storage backend. Works independently of document indexing.
    Handles both personal (/memories/) and channel (/channel_memories/) files.

    Args:
        file_path: Path of the file to export (should start with /memories/ or /channel_memories/)
        user_id: User ID for storage key generation
        store: AsyncPostgresStore instance for reading files
        storage: Object storage service for uploads
        s3_bucket: Bucket name for result storage

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

    await storage.upload(
        bucket=s3_bucket,
        key=s3_key,
        content=file_content.encode("utf-8"),
        content_type=content_type,
    )

    logger.info(f"Exported '{file_path}' to {s3_uri}")

    # Generate presigned URL (24 hours)
    presigned_url = await storage.generate_presigned_url(s3_uri, expiration_seconds=86400)

    return f"Exported file '{file_path}' to S3.\n\nDownload link (expires in 24 hours):\n{presigned_url}"


def create_document_store_tools(
    store: AsyncPostgresStore,
    storage: IObjectStorageService,
    s3_bucket: str,
    user_id: str,
    model_name: str | None = None,
    cost_logger: CostLogger | None = None,
) -> list[BaseTool]:
    """Create document store tools for semantic search, personal file access, and export.

    Documents are stored in user-scoped namespace (user_id, "documents") to enable
    cross-assistant document search, while filesystem remains assistant-scoped.

    Includes privacy-first cross-namespace access with interrupt-based permissions.

    Args:
        store: AsyncPostgresStore instance
        storage: Object storage service for file exports
        s3_bucket: Bucket for storing exported files
        user_id: User ID for document namespacing and storage keys
        model_name: Model to use for on-demand indexing in ``semantic_search_file``.
            When ``None``, the cheapest available provider model is selected
            automatically via ``get_default_indexing_model()``.
        cost_logger: Optional CostLogger for attributing on-demand indexing costs

    Returns:
        List of document store tools (docstore_search, semantic_search_file,
        read_personal_file, docstore_export)
    """

    @traceable(run_type="retriever")
    async def _search_documents_retriever(
        query: str,
        top_k: int = 5,
        include_personal: bool = False,
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
    ) -> str:
        """Search files you've written to long-term storage using semantic similarity.

        This searches indexed versions of files you've written using write_file or edit_file.
        Files are automatically indexed when written to long-term storage for semantic search.

        Use this when you need to:
        - Find relevant information across multiple files you've created
        - Search by meaning, not just filename
        - Get context from past work for answering questions

        In Slack channels, set include_personal=True to search the user's personal documents
        (requires approval via HITL middleware).

        Returns formatted context chunks that you can use to synthesize answers.

        Args:
            query: Natural language search query describing what you're looking for
            top_k: Number of relevant chunks to retrieve (default 5)
            include_personal: Search personal documents (channel context only, requires approval)

        Returns:
            Formatted context with relevant content from indexed files
        """
        documents = await _search_documents_retriever(
            query=query,
            top_k=top_k,
            include_personal=include_personal,
        )
        return _format_documents_for_llm(documents)

    async def semantic_search_file(
        file_path: str,
        query: str,
        top_k: int = 5,
    ) -> str:
        """Semantically search WITHIN a single large file you already have in hand.

        Use this for one big blob (typically a large/evicted tool result under
        /large_tool_results/) when you need to find the relevant passages by
        meaning rather than an exact string. The file is indexed on first use
        (cached afterwards) and the search is scoped to just that file.

        Choose the right tool:
        - Exact / known substring in a file -> use grep
        - Read a whole (small) file -> use read_file
        - Fuzzy search inside ONE large in-hand blob -> use this tool
        - Find a durable memory across your notes -> use docstore_search

        Args:
            file_path: Path of the in-hand file (e.g. /large_tool_results/<id>)
            query: Natural language description of what you're looking for
            top_k: Number of relevant chunks to retrieve (default 5)

        Returns:
            Formatted context chunks from within that file
        """
        documents = await _semantic_search_file_impl(
            file_path=file_path,
            query=query,
            top_k=top_k,
            user_id=user_id,
            store=store,
            model_name=model_name,
            cost_logger=cost_logger,
        )
        return _format_documents_for_llm(documents)

    async def read_personal_file(
        file_path: str,
    ) -> str:
        """Read a file from a user's personal workspace.

        Only available in Slack channel context. Approval is handled by
        HumanInTheLoopMiddleware before this tool executes.

        Use this when:
        - User mentions their personal files/notes in a channel
        - You need to reference user's private work in channel discussion
        - User explicitly asks to share their personal file content

        Args:
            file_path: Path to the personal file (e.g., "/my-notes.md")

        Returns:
            File content or error message
        """
        return await _read_personal_file_impl(
            file_path=file_path,
            target_user_id=user_id,
            store=store,
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
            storage=storage,
            s3_bucket=s3_bucket,
        )

    return [
        StructuredTool.from_function(
            coroutine=search_documents_rag,
            name="docstore_search",
            description=(
                "Search your durable memory (files written to long-term storage) using semantic similarity. "
                "This searches indexed versions of files created with write_file or edit_file. "
                "Files are automatically indexed when written to /memories/ (personal) or /channel_memories/ (shared). "
                "In personal context: searches your /memories/ files. "
                "In Slack channels: searches /channel_memories/ files shared with the channel. Use include_personal=True to also search your personal /memories/ files (requires permission). "
                "Use when: you need to find information across multiple durable notes/files by meaning (not just filename). "
                "Does NOT search large in-hand tool results \u2014 for that use semantic_search_file. "
                "Returns formatted context chunks for synthesizing answers."
            ),
        ),
        StructuredTool.from_function(
            coroutine=semantic_search_file,
            name="semantic_search_file",
            handle_tool_error=True,
            description=(
                "Semantically search WITHIN a single large file you already have in hand "
                "(typically a large or evicted tool result under /large_tool_results/). "
                "Indexes that one file on demand (cached afterwards) and finds the most relevant passages by meaning. "
                "Use when: a tool returned a big blob and you need to find specific information inside it by meaning, not exact string. "
                "For exact substrings use grep; to read a small file use read_file; "
                "to search durable memory across notes use docstore_search. "
                "Returns formatted context chunks from within that file."
            ),
        ),
        StructuredTool.from_function(
            coroutine=read_personal_file,
            name="read_personal_file",
            handle_tool_error=True,
            description=(
                "Read a file from user's personal workspace (Slack channel context only). "
                "Requires user approval (HITL-guarded). "
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
