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
from ringier_a2a_sdk.embeddings import GeminiEmbeddings

logger = logging.getLogger(__name__)

_AWS_REGION = os.environ.get("AWS_REGION", "eu-central-1")


# Module-level singletons (created once, reused across calls)
_query_embeddings: Embeddings | None = None


def _get_query_embeddings(
    cost_logger: Any | None = None,
) -> Embeddings:
    """Lazy-init query embedding singleton (Gemini Embedding 2, query role).

    If cost_logger is provided on the first call, cost tracking is enabled.
    user_sub is read from ContextVar at invoke time.
    """
    global _query_embeddings
    if _query_embeddings is None:
        _query_embeddings = GeminiEmbeddings(role="query", cost_logger=cost_logger)
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


async def search_catalogs(
    query: str,
    catalog_ids: list[str],
    top_k: int,
    vector_bucket_name: str,
    vector_store_backend: str,
    thumbnails_s3_bucket: str,
    cost_logger: Any | None = None,
) -> list[dict[str, Any]]:
    """Search across multiple catalogs and merge results by score.

    Returns a list of dicts with 'page_content', 'metadata', and 'score' keys,
    sorted by descending score, capped at top_k.
    """
    all_results: list[tuple[Document, float]] = []

    async def _search_one(catalog_id: str) -> list[tuple[Document, float]]:
        vs = _create_vector_store(catalog_id, vector_bucket_name, cost_logger=cost_logger)
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
        BaseTool instance, or None if user has no accessible catalogs.
    """
    if not accessible_catalog_ids:
        return None

    @traceable(run_type="retriever")
    async def _catalog_search_retriever(
        query: str,
        catalog_ids: list[str],
        top_k: int,
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
        )

    async def catalog_search(
        query: str,
        top_k: int = 10,
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

        Returns:
            Formatted search results with relevance scores and extraction references.
        """
        results = await _catalog_search_retriever(
            query=query,
            catalog_ids=accessible_catalog_ids,
            top_k=top_k,
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
            "Returns results with file info, content, thumbnails, and extraction references."
        ),
    )
