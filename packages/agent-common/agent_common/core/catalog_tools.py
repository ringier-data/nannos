"""Catalog search tool for semantic search over indexed document pages/slides.

Searches across user's accessible catalogs using S3 Vectors via
the LangChain VectorStore interface. Returns results with metadata
including extraction pointers (source_ref) for the pitch-deck agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import boto3
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool, StructuredTool
from langsmith import traceable
from ringier_a2a_sdk.embeddings import GatewayEmbeddings

logger = logging.getLogger(__name__)

_AWS_REGION = os.environ.get("AWS_REGION", "eu-central-1")


# Module-level singletons (created once, reused across calls); rebuilt if the configured
# default embedding alias changes (model_factory's defaults cache refreshes ~every 60s).
_query_embeddings: Embeddings | None = None
_query_embeddings_alias: str | None = None


def _get_query_embeddings(
    cost_logger: Any | None = None,
) -> Embeddings:
    """Lazy-init query embedding singleton (gateway-backed, query role).

    Resolves the SAME default embedding alias the indexing side uses
    (CatalogSyncPipeline.resolve_embedding_readiness / get_default_embedding_model), so query
    vectors are produced by the model the index was built with. A hardcoded alias here would
    silently mismatch the index once an admin changes the default → meaningless similarity
    (or a gateway 400 on a retired alias). When no default is configured the catalog feature
    is disabled, so this raises rather than guessing a model — callers reach it only via the
    catalog_search tool, which is gated off in that case.

    If cost_logger is provided on the first call, cost tracking is enabled.
    user_sub is read from ContextVar at invoke time.
    """
    global _query_embeddings, _query_embeddings_alias
    from agent_common.core.model_factory import get_default_embedding_model, get_model_provider

    alias = get_default_embedding_model(multimodal=True)
    if not alias:
        raise RuntimeError(
            "Catalog search requires a default embedding model, but none is configured "
            "(Admin → Model Gateway). The catalog_search tool should have been disabled."
        )
    if _query_embeddings is None or alias != _query_embeddings_alias:
        # Provider family selects the request profile (must match the indexing side so query
        # vectors are shaped like the index's). Read from the cached gateway registry.
        _query_embeddings = GatewayEmbeddings(
            role="query", model_id=alias, provider=get_model_provider(alias), cost_logger=cost_logger
        )
        _query_embeddings_alias = alias
    return _query_embeddings


def _create_vector_store(
    catalog_id: str,
    vector_bucket_name: str,
    cost_logger: Any | None = None,
) -> Any:
    """Create a read-only AmazonS3Vectors for querying a catalog index."""
    from langchain_aws.vectorstores.s3_vectors import AmazonS3Vectors

    query_emb = _get_query_embeddings(cost_logger=cost_logger)
    return AmazonS3Vectors(
        vector_bucket_name=vector_bucket_name,
        index_name=f"catalog-{catalog_id}",
        embedding=query_emb,
        query_embedding=query_emb,
        distance_metric="cosine",
        create_index_if_not_exist=False,
    )


# MIME type for native Google Slides. Used as a filter when callers need
# results that can be programmatically reconstructed via the Google Slides
# API (binary .pptx and PDFs cannot be turned back into editable decks).
GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"


async def search_catalogs(
    query: str,
    catalog_ids: list[str],
    top_k: int,
    vector_bucket_name: str,
    vector_store_backend: str,
    thumbnails_s3_bucket: str,
    cost_logger: Any | None = None,
    slides_only: bool = False,
) -> list[dict[str, Any]]:
    """Search across multiple catalogs and merge results by score.

    Returns a list of dicts with 'page_content', 'metadata', and 'score' keys,
    sorted by descending score, capped at top_k.

    When ``slides_only=True`` the search is restricted to pages whose source
    file is a native Google Slides deck. ``mime_type`` is indexed as
    filterable metadata at sync time, so the filter is pushed down to S3
    Vectors rather than being applied client-side.
    """
    all_results: list[tuple[Document, float]] = []

    search_filter: dict[str, Any] | None = None
    if slides_only:
        search_filter = {"mime_type": GOOGLE_SLIDES_MIME}

    async def _search_one(catalog_id: str) -> list[tuple[Document, float]]:
        vs = _create_vector_store(catalog_id, vector_bucket_name, cost_logger=cost_logger)
        if search_filter is not None:
            return await vs.asimilarity_search_with_score(query, top_k, filter=search_filter)
        return await vs.asimilarity_search_with_score(query, top_k)

    # Search all catalogs concurrently
    tasks = [_search_one(cid) for cid in catalog_ids]
    results_per_catalog = await asyncio.gather(*tasks, return_exceptions=True)

    for cid, result in zip(catalog_ids, results_per_catalog):
        if isinstance(result, Exception):
            logger.warning("Search failed for catalog %s: %s", cid, result)
            continue
        all_results.extend(result)

    # Sort by distance ascending (cosine distance: lower = more similar)
    all_results.sort(key=lambda x: x[1])
    top_results = all_results[:top_k]

    # Build thumbnail presigned URLs
    formatted: list[dict[str, Any]] = []
    for doc, score in top_results:
        meta = dict(doc.metadata)
        thumbnail_key = meta.get("thumbnail_s3_key", "")
        if thumbnail_key:
            meta["thumbnail_url"] = _build_thumbnail_url(thumbnails_s3_bucket, thumbnail_key)
        else:
            meta["thumbnail_url"] = ""

        formatted.append(
            {
                "page_content": doc.page_content,
                "metadata": meta,
                "score": score,
            }
        )

    return formatted


def _build_thumbnail_url(bucket: str, key: str) -> str:
    """Build a presigned S3 URL for a thumbnail (1 hour expiry)."""
    s3_client = boto3.client("s3", region_name=_AWS_REGION)
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=3600,
    )


def create_catalog_search_tool(
    accessible_catalog_ids: list[str],
    thumbnails_s3_bucket: str,
    vector_bucket_name: str,
    vector_store_backend: str = "s3_vectors",
    cost_logger: Any | None = None,
) -> BaseTool | None:
    """Create a catalog search tool for the given accessible catalogs.

    Args:
        accessible_catalog_ids: List of catalog IDs the user can access.
        thumbnails_s3_bucket: S3 bucket name for thumbnails (for presigned URLs).
        vector_bucket_name: S3 Vectors bucket name.
        vector_store_backend: Vector store backend type.

    Returns:
        BaseTool instance, or None if the user has no accessible catalogs or no default
        embedding model is configured (the catalog feature is disabled in that case).
    """
    if not accessible_catalog_ids:
        return None

    # Catalog search embeds the query through the configured default model. With none set,
    # the feature is disabled fleet-wide (matches indexing being blocked and the console UI),
    # so don't offer the tool rather than embedding with a guessed model.
    from agent_common.core.model_factory import is_embeddings_configured

    if not is_embeddings_configured(multimodal=True):
        logger.info("catalog_search tool disabled: no default embedding model configured")
        return None

    @traceable(run_type="retriever")
    async def _catalog_search_retriever(
        query: str,
        catalog_ids: list[str],
        top_k: int,
        slides_only: bool,
    ) -> list[dict[str, Any]]:
        """Retriever function traced in LangSmith."""
        return await search_catalogs(
            query=query,
            catalog_ids=catalog_ids,
            top_k=top_k,
            vector_bucket_name=vector_bucket_name,
            vector_store_backend=vector_store_backend,
            thumbnails_s3_bucket=thumbnails_s3_bucket,
            cost_logger=cost_logger,
            slides_only=slides_only,
        )

    async def catalog_search(
        query: str,
        top_k: int = 10,
        slides_only: bool = False,
    ) -> str:
        """Search across your document catalogs for relevant slides, pages, and content.

        Use this tool to find specific slides, pages, or content from indexed
        document repositories (Google Drive, presentations, PDFs).

        Each result includes:
        - File name and folder path
        - Document summary
        - Page/slide number, title, and content
        - Relevance score
        - Thumbnail URL (for visual preview)
        - Source reference (for extracting the original slide/page)

        Args:
            query: Natural language search query. Be specific about what content
                   you're looking for (e.g., "Q1 revenue breakdown by region",
                   "product roadmap timeline", "customer testimonials slide").
            top_k: Maximum number of results to return (default 10).
            slides_only: If True, restrict results to native Google Slides
                   decks only. Use this when you need to construct a new deck
                   programmatically via the Google Slides API, since binary
                   .pptx and PDF pages cannot be reconstructed as editable
                   slides. Default False (returns all matching content).

        Returns:
            Formatted search results with relevance scores and extraction references.
        """
        results = await _catalog_search_retriever(
            query=query,
            catalog_ids=accessible_catalog_ids,
            top_k=top_k,
            slides_only=slides_only,
        )

        if not results:
            return "No relevant pages found in your catalogs."

        parts = []
        for i, result in enumerate(results, 1):
            meta = result["metadata"]
            score = result.get("score", 0.0)
            source_ref = meta.get("source_ref", "{}")
            if isinstance(source_ref, str):
                source_ref_str = source_ref
            else:
                source_ref_str = json.dumps(source_ref)

            parts.append(
                f"Result {i} (score: {score:.2f}):\n"
                f'  File: "{meta.get("source_file_name", "Unknown")}" ({meta.get("folder_path", "")})\n'
                f"  Document: {meta.get('document_summary', 'N/A')}\n"
                f'  Page {meta.get("page_number", "?")} of {meta.get("page_count", "?")}: "{meta.get("title", "")}"\n'
                f"  Content: {result.get('page_content', '')[:300]}...\n"
                f"  Thumbnail: {meta.get('thumbnail_url', 'N/A')}\n"
                f"  Source ref: {source_ref_str}\n"
            )

        return f"Found {len(results)} relevant pages:\n\n" + "\n---\n\n".join(parts)

    return StructuredTool.from_function(
        coroutine=catalog_search,
        name="catalog_search",
        description=(
            "Search across your document catalogs for relevant slides, pages, and content. "
            "Use for finding specific presentations, slides, or document pages. "
            "Returns results with file info, content, thumbnails, and extraction references. "
            "Pass slides_only=true to restrict results to native Google Slides decks (required "
            "when reconstructing slides programmatically via the Google Slides API)."
        ),
    )
