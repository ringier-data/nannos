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

Cost is captured proxy-side: we stamp per-request spend-logs metadata via the shared
agent-common attribution helper (the full set — user_sub, conversation_id, sub_agent_id,
scheduled_job_id, catalog_id) that the proxy's CustomLogger reads — no in-app cost logging.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
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

    def _attribution_header(self) -> dict[str, str]:
        """Spend-logs header for proxy-side cost capture (ADR-0002).

        Uses the canonical agent-common attribution (the full field set: user_sub,
        conversation_id, sub_agent_id, scheduled_job_id, catalog_id) via the shared
        header builder, with explicit constructor overrides. Falls back to the SDK
        request user_sub only when the canonical var is unset (an SDK-only boundary), so
        catalog_search inside the orchestrator / sub-agents / scheduled jobs is attributed
        with all its dimensions instead of dropping everything but user_sub+catalog_id.
        """
        from agent_common.core.attribution import attribution_header, current_user_sub

        overrides: dict[str, Any] = {"catalog_id": self._catalog_id}
        if self._user_sub:
            overrides["user_sub"] = self._user_sub
        elif current_user_sub.get() is None:
            try:
                from ringier_a2a_sdk.cost_tracking.logger import get_request_user_sub

                overrides["user_sub"] = get_request_user_sub()
            except Exception:
                pass
        return attribution_header(**overrides)

    def _invoke(self, text: str, image_bytes: bytes | None = None, mime_type: str = "image/png") -> list[float]:
        """POST one item (text, or text+image fused) to the gateway and return its vector."""
        inputs: list[str] = [self._format_text(text)]
        if image_bytes:
            inputs.append(f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode())

        headers = {
            "Authorization": f"Bearer {os.getenv('LLM_GATEWAY_API_KEY', 'sk-nannos-gateway')}",
            "Content-Type": "application/json",
        }
        headers.update(self._attribution_header())

        body = {"model": self.model_id, "input": inputs, "dimensions": self.dimension}
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(f"{_gateway_base()}/embeddings", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()["data"]
        # Vertex multimodal fuses every input-list element into ONE vector, so we expect a
        # single embedding and read data[0]. If the gateway ever returns one vector per
        # element (the default OpenAI /embeddings contract), silently taking data[0] would
        # drop the image — fail loudly instead (#8).
        if len(data) != 1:
            raise RuntimeError(
                f"Expected a single fused embedding from {self.model_id} for a "
                f"{len(inputs)}-element input, got {len(data)} — the gateway is returning "
                "per-element vectors, not a fused one; multimodal embeddings would be wrong."
            )
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
                # copy the calling context so _attribution_header() still sees the attribution
                # ContextVars inside the executor thread — a raw run_in_executor would lose
                # them and the proxy would drop the embedding's cost.
                ctx = contextvars.copy_context()
                return await loop.run_in_executor(self._executor, ctx.run, self._invoke, t)

        return await asyncio.gather(*[_embed_one(t) for t in texts])

    async def aembed_query(self, text: str) -> list[float]:
        """Embed a single query text asynchronously."""
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()  # preserve attribution ContextVar across the executor hop
        return await loop.run_in_executor(self._executor, ctx.run, self._invoke, text)

    def embed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Embed text + image together (multimodal). Used by the sync pipeline."""
        return self._invoke(text=text, image_bytes=image_bytes)

    async def aembed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Async version of embed_with_image."""
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()  # preserve attribution ContextVar across the executor hop
        return await loop.run_in_executor(self._executor, ctx.run, self._invoke, text, image_bytes)
