"""Gateway embeddings adapter (LangChain Embeddings) routed through the Model Gateway.

All embedding traffic goes through the LiteLLM proxy `/embeddings` endpoint — no direct
Vertex/Bedrock SDK or cloud credentials in app pods. The adapter speaks to whatever embedding
alias the gateway resolves; how each request is shaped is decided by an ``EmbeddingProfile``
selected from the model's provider family / litellm model string (see ``profile_for``), so the
Gemini-2 assumptions that used to be hardcoded no longer leak onto other providers:

  * Asymmetric retrieval — Gemini Embedding 2 has no task_type param and encodes it as text
    prefixes (role="document": "title: none | text: …"; role="query": "task: search result |
    query: …"); Cohere uses an ``input_type`` request param; Titan/OpenAI are symmetric.
  * Output ``dimensions`` (Matryoshka) — requested only for models that accept the param; a
    model that rejects it is caught at registration (ModelGatewayService.test_model), not here.
  * Multimodal fusion — only Gemini Embedding 2 fuses text+image into one vector; every other
    model embeds text only and its image bytes are dropped (see _embed_with_image_or_degrade).

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
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.embeddings import Embeddings

from .utils.gateway import gateway_api_key, gateway_base_url
from .utils.http_pool import LazyClient

logger = logging.getLogger(__name__)

# Default output dimension when a caller doesn't pass one explicitly. Read from the same
# EMBEDDING_DIMENSION env that model_factory.get_embedding_dimension() uses, so the catalog
# paths (which construct GatewayEmbeddings without a dimension arg) and the pgvector/agent-memory
# stores (create_embeddings, which passes get_embedding_dimension()) never diverge — one source
# of truth for the index dimension. Falls back to 1024 when unset.
_DEFAULT_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
_MAX_CONCURRENT = 20

# Model aliases already warned about for lacking text+image fusion. The per-instance latch
# (_image_fusion_unsupported) skips the wasted fused call for the rest of a sync; this set
# keeps the log to one line per alias for the life of the process, across sync jobs/instances.
_FUSION_WARNED: set[str] = set()


@dataclass(frozen=True)
class EmbeddingProfile:
    """How a gateway embedding model wants its requests shaped.

    Selected by ``profile_for`` from the model's provider family / litellm model string — the
    public alias is admin-chosen and not a reliable capability signal. Only a few knobs differ
    between providers, and only two affect correctness rather than just retrieval quality:

      * ``text_prefixes`` (#1) — Gemini's text-prefix encoding of asymmetric retrieval; applying
        it to any other model just pollutes the embedded text.
      * ``input_type`` — Cohere expresses the same asymmetry as a request param instead.
      * ``send_dimensions`` (#2) — Matryoshka/output-dimension support is provider-specific; a
        model that rejects the param must fail at registration, not mid-sync.
      * ``supports_fusion`` (#3) — only Gemini Embedding 2 fuses text+image into one vector.
    """

    name: str
    text_prefixes: dict[str, str] | None = None  # role -> template containing "{text}"
    input_type: dict[str, str] | None = None  # role -> request input_type value
    send_dimensions: bool = True
    supports_fusion: bool = False

    def format_text(self, role: str, text: str) -> str:
        """Apply the provider's asymmetric-retrieval text prefix, or pass text through."""
        if self.text_prefixes:
            return self.text_prefixes.get(role, "{text}").format(text=text)
        return text


# Gemini Embedding 2 (Vertex / AI Studio): asymmetric retrieval via text prefixes (no task_type
# param), Matryoshka output dimensions, and the unique fused-input-list → single-vector contract.
_GEMINI = EmbeddingProfile(
    name="gemini",
    text_prefixes={
        "query": "task: search result | query: {text}",
        "document": "title: none | text: {text}",
    },
    send_dimensions=True,
    supports_fusion=True,
)
# Cohere embed v3/v4: asymmetric retrieval via the input_type request param; text-only. v3 is
# fixed-dimension and rejects the `dimensions` param, so we don't send it (revisit for v4).
_COHERE = EmbeddingProfile(
    name="cohere",
    input_type={"document": "search_document", "query": "search_query"},
    send_dimensions=False,
    supports_fusion=False,
)
# Everything else (Bedrock Titan/Nova, OpenAI text-embedding-*, Azure, unknown): symmetric,
# text-only, output dimension requested. A model that can't honour the requested dimension is
# rejected at registration (ModelGatewayService.test_model), not silently degraded here.
_GENERIC = EmbeddingProfile(name="generic", send_dimensions=True, supports_fusion=False)


def profile_for(litellm_model: str | None = None, provider: str | None = None) -> EmbeddingProfile:
    """Pick the request profile for a gateway embedding model.

    Prefers the precise ``litellm_params.model`` string when the caller has it (console-backend
    holds the master key on the registration path); otherwise the ``litellm_provider`` family is
    enough to answer the only correctness-critical question — Gemini-family (prefixes + fusion)
    vs everything else — and is reachable from the alias via the virtual-key ``/model/info``
    (``model_info.litellm_provider``), so the worker needs no master key. Unknown on both → the
    conservative generic profile.

    For *embeddings* the provider family is not the footgun it is for chat: Vertex hosts Claude
    too, but not as an embedding model, so a ``vertex_ai``/``gemini`` embedding deployment is
    Gemini-family and gets the full Gemini profile. ``supports_fusion`` is "attempt the fused
    call"; a rare non-fusing Vertex model (e.g. text-embedding-005) then fails the fused call
    once and the runtime latch degrades it to text-only — the same safe path as before. The
    System Status caveat (``supports_image_fusion``) passes only ``litellm_model`` (no provider),
    so it never takes this branch and still reports such a model as text-only.

    CONVERGENCE — LiteLLM already exposes ``input_type`` as the unified asymmetric-retrieval knob
    and translates it per provider (Vertex ``input_type``→``task_type`` RETRIEVAL_QUERY/
    RETRIEVAL_DOCUMENT; Cohere v3 ``search_query``/``search_document``; NVIDIA ``query``/
    ``passage``). The *values* are provider-specific (there is no universal enum and no
    capabilities API to discover them), so the app keeps a thin per-provider {role→value} map —
    that's exactly what ``input_type`` here is, and it's INHERENT, not throwaway. Cohere/NVIDIA/new
    providers should ride ``input_type``.

    Gemini still uses ``text_prefixes`` for a STALE reason: the original integration assumed
    gemini-embedding lacked ``task_type``. The LiteLLM Vertex-embedding docs contradict this —
    ``gemini-embedding-2`` honours ``task_type`` via the unified ``input_type`` (RETRIEVAL_QUERY/
    RETRIEVAL_DOCUMENT), and ``dimensions`` maps to Vertex ``output_dimensionality``. So the
    prefixes are likely NOT triggering asymmetric retrieval at all — they are prepended as literal
    text (noise). Converging Gemini onto ``input_type`` is the right fix but is NOT a drop-in:
    (a) verify the behaviour against the actual gateway/alias first, and (b) it's a re-index event
    — stored vectors were built with the prefix transform, so the doc side must be re-embedded for
    similarity to hold. ``send_dimensions`` (pinned-index requirement) and ``supports_fusion``
    (defensive runtime latch) are independent of all this.
    """
    m = (litellm_model or "").lower()
    p = (provider or "").lower()
    if "gemini-embedding" in m or p in ("gemini", "vertex_ai"):
        return _GEMINI  # prefixes + dimensions + attempt fusion
    if "cohere" in m or "embed-english" in m or "embed-multilingual" in m or p == "cohere":
        return _COHERE
    return _GENERIC


def supports_image_fusion(litellm_model: str | None) -> bool:
    """Whether this client can fuse text+image into one vector for the given gateway model.

    Only Gemini Embedding 2 (Vertex) honours the fused-input-list → single-vector contract the
    adapter speaks; every other embedding model (Bedrock Nova/Titan, …) embeds text only and its
    image bytes are dropped (see _embed_with_image_or_degrade). ``litellm_model`` is the gateway's
    ``litellm_params.model`` (e.g. ``vertex_ai/gemini-embedding-2``), not the public alias.

    Thin wrapper over ``profile_for`` so the System Status note and what the sync actually does
    can never disagree — both derive fusion from the same profile.
    """
    return profile_for(litellm_model).supports_fusion


_HTTP_TIMEOUT = 60.0  # generous: multimodal/fused image embeds are slower than text
# One process-wide connection pool reused across every _invoke (and every GatewayEmbeddings
# instance), instead of a fresh TCP+TLS handshake per embedding call — indexing a large
# catalog otherwise pays one handshake per chunk. httpx.Client is safe to share across
# threads, so the executor-backed async paths reuse it too. LazyClient handles the
# thread-safe lazy init shared with cost_tracking.attribution.
_client: LazyClient[httpx.Client] = LazyClient(lambda: httpx.Client(timeout=_HTTP_TIMEOUT))


class GatewayEmbeddings(Embeddings):
    """LangChain Embeddings served via the Model Gateway, shaped per-provider.

    The request profile (asymmetric-retrieval mechanism, output ``dimensions``, multimodal
    fusion) is chosen by ``profile_for`` from ``litellm_model``/``provider`` — pass whichever
    the caller can resolve; with neither it falls back to the conservative generic profile.

    For asymmetric retrieval create two instances:
      - role="document" for indexing
      - role="query" for searching

    Also supports multimodal text+image embedding via embed_with_image() (degrades to text-only
    when the resolved model can't fuse).

    `cost_logger` is accepted for backwards compatibility but no longer used — cost is
    captured proxy-side from the per-request spend-logs metadata.
    """

    def __init__(
        self,
        role: str = "document",
        dimension: int = _DEFAULT_DIMENSION,
        *,
        model_id: str,
        litellm_model: str | None = None,
        provider: str | None = None,
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
        # The provider profile decides prefixes vs input_type, whether to send `dimensions`, and
        # whether text+image fusion is even attempted (see profile_for / _embed_with_image_or_degrade).
        self._profile = profile_for(litellm_model, provider)
        self._user_sub = user_sub
        self._catalog_id = catalog_id
        self._executor = executor
        # Latches True the first time a text+image call proves the model can't fuse, so the
        # rest of this instance's image-bearing docs skip straight to text-only (see
        # _embed_with_image_or_degrade).
        self._image_fusion_unsupported = False

    def _format_text(self, text: str) -> str:
        """Apply the provider's asymmetric-retrieval text prefix for this instance's role."""
        return self._profile.format_text(self.role, text)

    def _attribution_header(self) -> dict[str, str]:
        """Spend-logs header for proxy-side cost capture.

        Uses the canonical attribution helper (the full field set: user_sub,
        conversation_id, sub_agent_id, scheduled_job_id, catalog_id) via the shared
        header builder, with explicit constructor overrides. Falls back to the SDK
        request user_sub only when the canonical var is unset (an SDK-only boundary), so
        catalog_search inside the orchestrator / sub-agents / scheduled jobs is attributed
        with all its dimensions instead of dropping everything but user_sub+catalog_id.
        """
        from ringier_a2a_sdk.cost_tracking.attribution import attribution_header, current_user_sub

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
            "Authorization": f"Bearer {gateway_api_key()}",
            "Content-Type": "application/json",
        }
        headers.update(self._attribution_header())

        body: dict[str, Any] = {"model": self.model_id, "input": inputs}
        # Send `dimensions` only for providers that accept the Matryoshka param — a model that
        # rejects it (e.g. Cohere v3, Titan v1) is caught at registration, not here (#2).
        if self._profile.send_dimensions:
            body["dimensions"] = self.dimension
        # Cohere encodes asymmetric retrieval as a request param rather than a text prefix.
        if self._profile.input_type and (input_type := self._profile.input_type.get(self.role)):
            body["input_type"] = input_type
        resp = _client.get().post(f"{gateway_base_url()}/embeddings", json=body, headers=headers)
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
        """Embed multiple texts (one model call each — Vertex fuses a list into one vector, so
        each text MUST be its own request and can't be batched into a single multi-input call).

        The calls are independent blocking POSTs, so fan them out across a bounded thread pool
        instead of issuing them serially: a batch costs ~ceil(N / _MAX_CONCURRENT) round-trips
        of wall-clock instead of N. The shared httpx.Client is thread-safe, and this mirrors the
        bounded concurrency aembed_documents already uses.
        """
        if len(texts) <= 1:
            return [self._invoke(text=t) for t in texts]
        # Snapshot the caller's context PER task (in this thread) so _attribution_header() still
        # sees the attribution ContextVars inside the worker threads — a bare thread would lose
        # them and the proxy would drop each embedding's cost. A Context can't be entered
        # concurrently, hence one copy per text.
        tasks = [(contextvars.copy_context(), t) for t in texts]

        def _run(task: tuple[contextvars.Context, str]) -> list[float]:
            ctx, text = task
            return ctx.run(self._invoke, text)

        if self._executor is not None:
            return list(self._executor.map(_run, tasks))
        with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT, len(texts))) as pool:
            return list(pool.map(_run, tasks))

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

    def _warn_fusion_unsupported(self, reason: str) -> None:
        """Log once per alias (process-wide) that this model can't fuse text+image."""
        if self.model_id in _FUSION_WARNED:
            return
        _FUSION_WARNED.add(self.model_id)
        logger.warning(
            "Embedding model %r does not support multimodal text+image fusion (%s); "
            "degrading image-bearing documents to text-only. Set a fusion-capable "
            "model (Gemini Embedding 2) as the multimodal_embedding default to embed images.",
            self.model_id,
            reason,
        )

    def _embed_with_image_or_degrade(self, text: str, image_bytes: bytes) -> list[float]:
        """Fused text+image embedding, degrading to text-only when the model can't fuse.

        Only Gemini Embedding 2 (Vertex) honours the fused-input-list → single-vector contract
        this client speaks; any other model (e.g. Bedrock Nova/Titan) would otherwise fail every
        image-bearing doc. When the resolved profile already knows the model is text-only we skip
        the doomed fused call entirely; otherwise we attempt it and, on the first capability error,
        latch to text-only for the rest of this instance. Either way the image then contributes
        nothing to the vector, but indexing proceeds.
        """
        if self._image_fusion_unsupported:
            return self._invoke(text=text)
        if not self._profile.supports_fusion:
            # Known text-only provider (or provider-only signal): don't waste a fused round-trip.
            self._image_fusion_unsupported = True
            self._warn_fusion_unsupported(f"profile={self._profile.name}")
            return self._invoke(text=text)
        try:
            return self._invoke(text=text, image_bytes=image_bytes)
        except (RuntimeError, httpx.HTTPStatusError) as e:
            # RuntimeError → gateway returned per-element vectors (no fusion).
            # 400/422 → the provider rejected the fused image input shape.
            # Anything else (timeout, 5xx) is transient — re-raise rather than mask it as a
            # capability gap, so a real outage isn't silently downgraded for the whole sync.
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code not in (400, 422):
                raise
            self._image_fusion_unsupported = True
            self._warn_fusion_unsupported(type(e).__name__)
            return self._invoke(text=text)

    def embed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Embed text + image together (multimodal). Used by the sync pipeline.

        Degrades to text-only when the configured model can't fuse text+image
        (see _embed_with_image_or_degrade)."""
        return self._embed_with_image_or_degrade(text, image_bytes)

    async def aembed_with_image(self, text: str, image_bytes: bytes) -> list[float]:
        """Async version of embed_with_image (same text-only degradation)."""
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()  # preserve attribution ContextVar across the executor hop
        return await loop.run_in_executor(self._executor, ctx.run, self._embed_with_image_or_degrade, text, image_bytes)
