"""Gemini Embedding 2 adapter (LangChain Embeddings) routed through the Model Gateway.

All embedding traffic goes through the LiteLLM proxy `/embeddings` endpoint — no direct
Vertex SDK or GCP credentials in app pods. Gemini Embedding 2 supports unified
multimodal embeddings (text, image, or text+image fused into a single vector) and
Matryoshka adjustable output dimensions.

Gemini Embedding 2 has no task_type parameter; asymmetric retrieval uses text prefixes:
  - Indexing  (role="document"): "title: none | text: {content}"
  - Querying  (role="query"):    "task: search result | query: {text}"

Vertex fuses every element of the request's input list into ONE embedding, so text+image
go in a single flat list → one combined vector (one model call per item).

Cost is captured proxy-side: we stamp per-request spend-logs metadata (user_sub,
catalog_id) that the proxy's CustomLogger reads — no in-app cost logging.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from concurrent.futures import Executor
from typing import Any

import httpx
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)

# Gateway alias for the embedding model (registered on the proxy; overridable per env).
_DEFAULT_MODEL_ALIAS = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2")
_DEFAULT_DIMENSION = 1024
_MAX_CONCURRENT = 20
_SPEND_LOGS_HEADER = "x-litellm-spend-logs-metadata"


def _gateway_base() -> str:
    base = os.getenv("LLM_GATEWAY_URL")
    if not base:
        raise RuntimeError("LLM_GATEWAY_URL is not set — the Model Gateway is required for embeddings")
    return base.rstrip("/")


class GeminiEmbeddings(Embeddings):
    """LangChain Embeddings for Gemini Embedding 2, served via the Model Gateway.

    For asymmetric retrieval create two instances:
      - role="document" for indexing (formats text as "title: none | text: ...")
      - role="query" for searching (formats text as "task: search result | query: ...")

    Also supports multimodal text+image embedding via embed_with_image().

    `cost_logger` is accepted for backwards compatibility but no longer used — cost is
    captured proxy-side from the per-request spend-logs metadata.
    """

    def __init__(
        self,
        role: str = "document",
        dimension: int = _DEFAULT_DIMENSION,
        model_id: str = _DEFAULT_MODEL_ALIAS,
        cost_logger: Any | None = None,
        user_sub: str | None = None,
        catalog_id: str | None = None,
        executor: Executor | None = None,
    ) -> None:
        if role not in ("document", "query"):
            raise ValueError(f"role must be 'document' or 'query', got {role!r}")
        self.role = role
        self.dimension = dimension
        self.model_id = model_id
        self._user_sub = user_sub
        self._catalog_id = catalog_id
        self._executor = executor

    def _format_text(self, text: str) -> str:
        """Apply Gemini Embedding 2 task prefix for asymmetric retrieval."""
        if self.role == "query":
            return f"task: search result | query: {text}"
        return f"title: none | text: {text}"

    def _spend_metadata(self) -> dict[str, str]:
        """Attribution for proxy-side cost capture (user_sub explicit or from ContextVar)."""
        user_sub = self._user_sub
        if not user_sub:
            try:
                from ringier_a2a_sdk.cost_tracking.logger import get_request_user_sub

                user_sub = get_request_user_sub()
            except Exception:
                user_sub = None
        meta: dict[str, str] = {}
        if user_sub:
            meta["user_sub"] = user_sub
        if self._catalog_id:
            meta["catalog_id"] = self._catalog_id
        return meta

    def _invoke(self, text: str, image_bytes: bytes | None = None, mime_type: str = "image/png") -> list[float]:
        """POST one item (text, or text+image fused) to the gateway and return its vector."""
        inputs: list[str] = [self._format_text(text)]
        if image_bytes:
            inputs.append(f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode())

        headers = {
            "Authorization": f"Bearer {os.getenv('LLM_GATEWAY_API_KEY', 'sk-nannos-gateway')}",
            "Content-Type": "application/json",
        }
        meta = self._spend_metadata()
        if meta:
            headers[_SPEND_LOGS_HEADER] = json.dumps(meta)

        body = {"model": self.model_id, "input": inputs, "dimensions": self.dimension}
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(f"{_gateway_base()}/embeddings", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()["data"]
        return list(data[0]["embedding"])

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts (one model call each — Vertex fuses a list into one vector)."""
        return [self._invoke(text=t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query text."""
        return self._invoke(text=text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts concurrently with bounded parallelism."""
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        loop = asyncio.get_running_loop()

        async def _embed_one(t: str) -> list[float]:
            async with semaphore:
                return await loop.run_in_executor(self._executor, self._invoke, t)

        return await asyncio.gather(*[_embed_one(t) for t in texts])

    async def aembed_query(self, text: str) -> list[float]:
        """Embed a single query text asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._invoke, text)

    def embed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Embed text + image together (multimodal). Used by the sync pipeline."""
        return self._invoke(text=text, image_bytes=image_bytes)

    async def aembed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Async version of embed_with_image."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._invoke, text, image_bytes)
