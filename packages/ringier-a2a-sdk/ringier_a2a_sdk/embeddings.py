"""Gemini Embedding 2 adapter implementing LangChain's Embeddings interface.

Model: gemini-embedding-2-preview  (Vertex AI)
Supports unified multimodal embeddings: text, image, or text+image in one call.
Uses Matryoshka Representation Learning for adjustable output dimensions.

Gemini Embedding 2 does NOT support the task_type config parameter.
Instead, task instructions are embedded as text prefixes:
  - Indexing:  "title: none | text: {content}"
  - Querying:  "task: search result | query: {text}"
See: https://ai.google.dev/gemini-api/docs/embeddings
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import Executor
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai import types
from langchain_core.embeddings import Embeddings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_ID = "gemini-embedding-2-preview"
_DEFAULT_DIMENSION = 1024
_MAX_CONCURRENT = 20

# Gemini Embedding 2 is only available in us-central1
_GCP_EMBEDDING_LOCATION = "us-central1"


def _build_genai_client() -> genai.Client:
    """Build a Gemini client authenticated via Vertex AI service account.

    Reads GCP_KEY (service account JSON), GCP_PROJECT_ID from environment.
    """
    gcp_key = os.getenv("GCP_KEY")
    gcp_project = os.getenv("GCP_PROJECT_ID")

    if not gcp_key:
        raise RuntimeError("GCP_KEY is not set — add the service account JSON blob to .env")
    if not gcp_project:
        raise RuntimeError("GCP_PROJECT_ID is not set — add it to .env")

    from google.oauth2 import service_account as _sa

    credentials = _sa.Credentials.from_service_account_info(
        json.loads(gcp_key),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )

    return genai.Client(
        vertexai=True,
        credentials=credentials,
        project=gcp_project,
        location=_GCP_EMBEDDING_LOCATION,
    )


# Module-level singleton client (created once on first use)
_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = _build_genai_client()
    return _client


class GeminiEmbeddings(Embeddings):
    """LangChain Embeddings wrapping Gemini Embedding 2 on Vertex AI.

    For asymmetric retrieval, create two instances:
      - role="document" for indexing (formats text as "title: none | text: ...")
      - role="query" for searching (formats text as "task: search result | query: ...")

    Also supports multimodal text+image embedding via embed_with_image().

    The cost_logger can be any object with a ``log_cost_async()`` method
    (e.g. ``ringier_a2a_sdk.CostLogger`` or agent-console backend's ``InternalCostLogger``).
    """

    def __init__(
        self,
        role: str = "document",
        dimension: int = _DEFAULT_DIMENSION,
        model_id: str = _DEFAULT_MODEL_ID,
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
        self._cost_logger = cost_logger
        self._user_sub = user_sub
        self._catalog_id = catalog_id
        self._executor = executor

    def _format_text(self, text: str) -> str:
        """Apply Gemini Embedding 2 task prefix for asymmetric retrieval."""
        if self.role == "query":
            return f"task: search result | query: {text}"
        return f"title: none | text: {text}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts synchronously (used by VectorStore.add_documents)."""
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
                return await loop.run_in_executor(self._executor, self._invoke, t, None)

        return await asyncio.gather(*[_embed_one(t) for t in texts])

    async def aembed_query(self, text: str) -> list[float]:
        """Embed a single query text asynchronously."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._invoke, text, None)

    def embed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Embed text + image together (multimodal). Used by the sync pipeline."""
        return self._invoke(text=text, image_bytes=image_bytes)

    async def aembed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Async version of embed_with_image."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._invoke, text, image_bytes)

    def _invoke(self, text: str, image_bytes: bytes | None = None) -> list[float]:
        """Call Gemini embed_content and return the embedding vector."""
        client = _get_client()

        formatted = self._format_text(text)
        parts: list[types.Part] = [types.Part(text=formatted)]
        if image_bytes:
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))

        result = client.models.embed_content(
            model=self.model_id,
            contents=[types.Content(parts=parts)],
            config=types.EmbedContentConfig(
                output_dimensionality=self.dimension,
            ),
        )

        self._log_cost(formatted, image_bytes)

        return list(result.embeddings[0].values)

    def _log_cost(self, formatted_text: str, image_bytes: bytes | None = None) -> None:
        """Log embedding cost if a cost logger is configured."""
        if not self._cost_logger:
            return

        user_sub = self._user_sub
        if not user_sub:
            from ringier_a2a_sdk.cost_tracking.logger import get_request_user_sub

            user_sub = get_request_user_sub()
        if not user_sub:
            return

        billing: dict[str, int] = {"input_text_tokens": len(formatted_text) // 4}
        if image_bytes:
            billing["input_images"] = 1

        self._cost_logger.log_cost_async(
            user_sub=user_sub,
            billing_unit_breakdown=billing,
            provider="google",
            model_name=self.model_id,
            catalog_id=self._catalog_id,
        )
