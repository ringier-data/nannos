"""Semantic chunking service with contextual descriptions using Claude + prompt caching.

This module implements semantic document chunking with:
1. Sentence-level text splitting using NLTK
2. Embedding-based boundary detection (Titan Embeddings V2)
3. Document summary generation (interleaved sampling, ≤50k chars) for use as LLM context
4. Batched contextualization with Claude using prompt caching for cost optimization
5. Returns chunks with context descriptions for improved retrieval

Optimized for contracts, papers, and technical documentation (including very large documents).
"""

import logging
import math
import re
from typing import Any, Optional

import nltk
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage
from ringier_a2a_sdk.cost_tracking import CostLogger, CostTrackingCallback

from agent_common.core.cost_tracking_embeddings import CostTrackingBedrockEmbeddings

logger = logging.getLogger(__name__)


# Ensure NLTK punkt tokenizer is available.
# Modern NLTK (3.9+) uses punkt_tab; fall back to legacy punkt for older installs.
def _ensure_nltk_punkt() -> None:
    for resource in ("tokenizers/punkt_tab", "tokenizers/punkt"):
        try:
            nltk.data.find(resource)
            return  # found — nothing to download
        except LookupError:
            pass
    # Neither found — download punkt_tab (preferred) and fall back to punkt
    try:
        logger.info("Downloading NLTK punkt_tab tokenizer...")
        nltk.download("punkt_tab", quiet=True)
    except Exception:
        logger.info("Downloading NLTK punkt tokenizer (fallback)...")
        nltk.download("punkt", quiet=True)


_ensure_nltk_punkt()

# Chunking parameters
# DEFAULT_CHUNK_SIZE_CHARS is calculated based on:
#   - FilesystemMiddleware eviction threshold: 20,000 tokens × 4 chars/token = 80,000 chars
#   - Default docstore_search top_k: 5 chunks
#   - Target: 80,000 / 5 = 16,000 chars per chunk (so 5 chunks don't trigger re-eviction)
#   - We use 8,000 as target so 2× hard break = 16,000 max chunk size
DEFAULT_CHUNK_SIZE_CHARS = 8_000  # Target chunk size in characters (hard break at 2×)
TITAN_EMBED_MAX_CHARS = 50_000  # Hard limit for Titan Embeddings V2 input
SEMANTIC_SIMILARITY_THRESHOLD = 0.6  # Cosine similarity threshold for boundary detection
SLIDING_WINDOW_SENTENCES = 3  # Number of sentences per window for embedding computation

# Context batching: max chunk text per Claude batch.
# Claude 3 Haiku has a 200k token context window.  We reserve ~20k tokens for
# the document summary (≤50k chars) + prompt template + expected output,
# leaving ~180k tokens for chunks.  Real-world documents (contracts, code, etc.)
# average ~3 chars/token — NOT the commonly cited 4 chars/token — so we use
# 300k chars ≈ 100k tokens as a safe per-batch ceiling.  This is intentionally
# conservative: an overly large batch caused a 214k-token prompt in production
# when the original 700k-char limit assumed 4 chars/token.
CONTEXT_BATCH_MAX_CHARS = 300_000


def _compute_cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))

    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0

    return dot_product / (magnitude1 * magnitude2)


def _create_document_summary(sentences: list[str], max_chars: int = TITAN_EMBED_MAX_CHARS) -> str:
    """Create a ≤max_chars representative summary of a document by sampling sentences.

    Strategy:
    - Head  (first ~1/3 of budget): all sentences in order, no sampling.
    - Tail  (last  ~1/3 of budget): all sentences in order, no sampling.
    - Middle (remaining ~1/3):    evenly-sampled sentences if they exceed budget,
                                   otherwise all included verbatim.

    Head and tail are kept dense because they most often contain the most important
    context (abstract, introduction, conclusions, bibliography).

    Args:
        sentences: List of sentences from the document.
        max_chars: Hard character limit for the returned summary.

    Returns:
        A string of ≤ max_chars characters representing the document.
    """
    full_text = " ".join(sentences)
    if len(full_text) <= max_chars:
        return full_text

    head_budget = max_chars // 3
    tail_budget = max_chars // 3
    middle_budget = max_chars - head_budget - tail_budget  # remaining ~1/3

    # --- Head ---
    head_sentences: list[str] = []
    head_chars = 0
    head_end_idx = 0
    for idx, sent in enumerate(sentences):
        needed = len(sent) + (1 if head_sentences else 0)  # +1 for space separator
        if head_chars + needed > head_budget:
            break
        head_sentences.append(sent)
        head_chars += needed
        head_end_idx = idx + 1

    # --- Tail ---
    tail_sentences: list[str] = []
    tail_chars = 0
    tail_start_idx = len(sentences)
    for sent in reversed(sentences[head_end_idx:]):
        needed = len(sent) + (1 if tail_sentences else 0)
        if tail_chars + needed > tail_budget:
            break
        tail_sentences.insert(0, sent)
        tail_chars += needed
        tail_start_idx -= 1

    # --- Middle ---
    middle_sentences = sentences[head_end_idx:tail_start_idx]
    middle_total_chars = sum(len(s) for s in middle_sentences) + max(0, len(middle_sentences) - 1)

    if middle_total_chars <= middle_budget:
        sampled_middle = middle_sentences
        middle_marker = ""
    else:
        # Sample every N-th sentence so the sampled content fits in the budget
        n = math.ceil(middle_total_chars / middle_budget)
        sampled_middle = middle_sentences[::n]
        middle_marker = f"[...{len(middle_sentences)} middle sentences, every {n}th shown...]"

    # Assemble
    parts: list[str] = []
    if head_sentences:
        parts.append(" ".join(head_sentences))
    if middle_marker:
        parts.append(middle_marker)
    if sampled_middle:
        parts.append(" ".join(sampled_middle))
    if tail_sentences:
        parts.append("[...end middle...]")
        parts.append(" ".join(tail_sentences))

    summary = " ".join(parts)
    return summary[:max_chars]


async def chunk_with_context(
    content: str,
    metadata: dict[str, Any],
    model: ChatBedrockConverse,
    embeddings_model: CostTrackingBedrockEmbeddings | None = None,
    chunk_size_chars: int = DEFAULT_CHUNK_SIZE_CHARS,
    cost_logger: Optional[CostLogger] = None,
) -> list[tuple[str, str]]:
    """Chunk text with semantic boundaries and generate contextual descriptions.

    Args:
        content: The document text to chunk
        metadata: Metadata about the document (file_path, etc.)
        model: ChatBedrockConverse model for generating context descriptions
        embeddings_model: CostTrackingBedrockEmbeddings model for boundary detection (optional)
        chunk_size_chars: Target chunk size in characters
        cost_logger: Optional CostLogger for reporting LLM usage costs

    Returns:
        List of tuples: (chunk_text, context_description)
    """
    logger.info(f"Starting semantic chunking for document with {len(content)} chars")

    # Initialize embeddings model if not provided
    if embeddings_model is None:
        embeddings_model = CostTrackingBedrockEmbeddings(
            model_id="amazon.titan-embed-text-v2:0",
            region_name=model.region_name,
            cost_logger=cost_logger,
        )

    # Step 1: Split into sentences
    try:
        sentences = nltk.sent_tokenize(content)
    except Exception as e:
        logger.warning(f"NLTK tokenization failed: {e}. Falling back to simple split.")
        sentences = re.split(r"[.!?]+", content)
        sentences = [s.strip() for s in sentences if s.strip()]

    logger.info(f"Split document into {len(sentences)} sentences")

    if len(sentences) == 0:
        logger.warning("No sentences found in document")
        return []

    # Step 2: Build document summary (≤TITAN_EMBED_MAX_CHARS) for use as LLM context
    document_summary = _create_document_summary(sentences)
    logger.info(f"Document summary: {len(document_summary)} chars (original: {len(content)} chars)")

    # Step 3: Group sentences into chunks based on semantic boundaries (char-based)
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_char_count = 0

    # Compute embeddings for sliding windows to detect semantic boundaries.
    # Individual sentence windows are short (≪50k chars) so always within Titan limit.
    # Collect all window texts first, then embed in one batched call so the SDK can
    # issue requests concurrently (asyncio.gather) instead of sequentially.
    window_texts = [
        " ".join(sentences[i : i + SLIDING_WINDOW_SENTENCES])
        for i in range(0, len(sentences), SLIDING_WINDOW_SENTENCES)
    ]
    window_texts = [t for t in window_texts if t.strip()]
    window_embeddings: list[list[float]] = await embeddings_model.aembed_documents(window_texts)

    logger.info(f"Computed {len(window_embeddings)} window embeddings for boundary detection")

    # Build chunks respecting semantic boundaries
    next_window_idx = 0
    window_idx = 0
    for i, sentence in enumerate(sentences):
        current_chunk.append(sentence)
        current_char_count += len(sentence) + (1 if len(current_chunk) > 1 else 0)  # +1 for space

        # Check if we should create a chunk boundary
        should_break = False

        # Always break if chunk is getting too large (2× target — still well under Titan 50k limit)
        if current_char_count >= chunk_size_chars * 2:
            should_break = True
        # Check semantic boundary if we're near target size
        elif current_char_count >= chunk_size_chars:
            next_window_idx = (i + 1) // SLIDING_WINDOW_SENTENCES
            if window_idx < len(window_embeddings) - 1 and next_window_idx < len(window_embeddings):
                similarity = _compute_cosine_similarity(
                    window_embeddings[window_idx], window_embeddings[next_window_idx]
                )
                if similarity < SEMANTIC_SIMILARITY_THRESHOLD:
                    should_break = True
                    logger.debug(f"Semantic boundary at sentence {i} (similarity: {similarity:.3f})")

        if should_break and current_chunk:
            chunks.append(" ".join(current_chunk))
            current_chunk = []
            current_char_count = 0
            window_idx = next_window_idx if next_window_idx < len(window_embeddings) else window_idx

    # Add remaining sentences as final chunk
    if current_chunk:
        chunks.append(" ".join(current_chunk))

    logger.info(f"Created {len(chunks)} semantic chunks")

    # Validate chunk sizes - ensure no chunk exceeds safe embedding limit
    # With context description prepended, we need chunks to be well under TITAN_EMBED_MAX_CHARS
    # Use 40k as safe threshold (leaving 10k headroom for context descriptions)
    MAX_SAFE_CHUNK_SIZE = 40_000
    oversized_chunks = [i for i, chunk in enumerate(chunks) if len(chunk) > MAX_SAFE_CHUNK_SIZE]

    if oversized_chunks:
        logger.warning(
            f"Found {len(oversized_chunks)} chunks exceeding {MAX_SAFE_CHUNK_SIZE} chars. "
            f"This should not happen with DEFAULT_CHUNK_SIZE_CHARS={DEFAULT_CHUNK_SIZE_CHARS}. "
            f"Oversized chunk indices: {oversized_chunks[:5]}..."
        )
        # Split oversized chunks naively to prevent embedding failures
        validated_chunks = []
        for i, chunk in enumerate(chunks):
            if len(chunk) <= MAX_SAFE_CHUNK_SIZE:
                validated_chunks.append(chunk)
            else:
                # Split oversized chunk into smaller pieces
                logger.warning(f"Splitting oversized chunk {i} ({len(chunk)} chars)")
                words = chunk.split()
                current_piece = []
                current_len = 0
                for word in words:
                    word_len = len(word) + 1  # +1 for space
                    if current_len + word_len > MAX_SAFE_CHUNK_SIZE and current_piece:
                        validated_chunks.append(" ".join(current_piece))
                        current_piece = []
                        current_len = 0
                    current_piece.append(word)
                    current_len += word_len
                if current_piece:
                    validated_chunks.append(" ".join(current_piece))
        chunks = validated_chunks
        logger.info(f"After validation: {len(chunks)} chunks (split {len(oversized_chunks)} oversized chunks)")

    # Step 4: Generate contextual descriptions in batches (each batch ≤ CONTEXT_BATCH_MAX_CHARS)
    chunk_descriptions = await _generate_chunk_contexts_batched(document_summary, chunks, metadata, model, cost_logger)

    # Combine results
    results: list[tuple[str, str]] = list(zip(chunks, chunk_descriptions))
    logger.info(f"Chunking complete: {len(results)} chunks with context descriptions")
    return results


async def _generate_chunk_contexts_batched(
    document_summary: str,
    chunks: list[str],
    metadata: dict[str, Any],
    model: ChatBedrockConverse,
    cost_logger: Optional[CostLogger] = None,
) -> list[str]:
    """Generate contextual descriptions for all chunks, processing in char-bounded batches.

    Each batch is bounded to CONTEXT_BATCH_MAX_CHARS of chunk text so that the full
    Claude prompt (summary + batch chunks + template) stays within the 200k token limit.
    The document summary is included in every batch call with prompt caching so it is
    only charged once per 5-minute cache window.

    Args:
        document_summary: ≤TITAN_EMBED_MAX_CHARS representative summary of the full document.
        chunks: Chunked text segments to describe.
        metadata: Document metadata (file_path, type, etc.).
        model: ChatBedrockConverse model for generation.
        cost_logger: Optional CostLogger for reporting LLM usage costs.

    Returns:
        List of description strings, one per chunk (same order, same length).
    """
    if not chunks:
        return []

    file_path = metadata.get("file_path", "document")
    doc_type = metadata.get("type", "document")

    # Split chunks into batches that each fit in CONTEXT_BATCH_MAX_CHARS
    batches: list[list[tuple[int, str]]] = []  # list of (original_index, chunk_text)
    current_batch: list[tuple[int, str]] = []
    current_batch_chars = 0
    for idx, chunk in enumerate(chunks):
        chunk_len = len(chunk)
        if current_batch and current_batch_chars + chunk_len > CONTEXT_BATCH_MAX_CHARS:
            batches.append(current_batch)
            current_batch = []
            current_batch_chars = 0
        current_batch.append((idx, chunk))
        current_batch_chars += chunk_len
    if current_batch:
        batches.append(current_batch)

    logger.info(f"Processing {len(chunks)} chunks in {len(batches)} batch(es) for context generation")

    # Build callbacks list for cost tracking if a CostLogger is available
    callbacks = [CostTrackingCallback(cost_logger)] if cost_logger else []

    # Build the cached system message once — identical across all batches so it
    # benefits from Claude's 5-minute prompt cache on every batch after the first.
    system_msg = SystemMessage(
        content=[
            {
                "type": "text",
                "text": (
                    f"You are analyzing a {doc_type} to generate brief contextual "
                    f"descriptions for document chunks.\n\n"
                    f"Here is a representative sample of the full document for context:\n"
                    f"<document>\n{document_summary}\n</document>"
                ),
                "cache_control": {"type": "ephemeral"},  # Cache this message for 5 minutes
            }
        ]
    )

    all_descriptions: dict[int, str] = {}

    for batch_num, batch in enumerate(batches, 1):
        batch_indices = [idx for idx, _ in batch]
        batch_chunks = [chunk for _, chunk in batch]

        chunks_text = ""
        for local_i, chunk in enumerate(batch_chunks, 1):
            chunks_text += f'\n<chunk index="{local_i}">\n{chunk}\n</chunk>'

        human_prompt = (
            f"Generate a brief 1-2 sentence contextual description for each chunk below. "
            f"Each description should:\n"
            f"1. Summarize what the chunk discusses\n"
            f"2. Mention how it relates to the overall document\n"
            f"3. Include key entities, topics, or concepts\n"
            f"4. Be specific enough to help with semantic search\n"
            f"{chunks_text}\n\n"
            f"Respond with ONLY a JSON array of descriptions, one per chunk "
            f"(exactly {len(batch_chunks)} items):\n"
            f'["description for chunk 1", "description for chunk 2", ...]'
        )
        human_msg = HumanMessage(content=human_prompt)

        logger.info(
            f"Batch {batch_num}/{len(batches)}: generating descriptions for "
            f"{len(batch_chunks)} chunks ({sum(len(c) for c in batch_chunks)} chars)"
        )

        try:
            response = await model.ainvoke(
                [system_msg, human_msg], config={"callbacks": callbacks} if callbacks else {}
            )
            response_text = response.content

            import json

            descriptions: list[str] | None = None
            if isinstance(response_text, str):
                json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group(0))
                    if len(parsed) == len(batch_chunks):
                        descriptions = parsed
                    else:
                        logger.warning(
                            f"Batch {batch_num}: description count mismatch "
                            f"(got {len(parsed)}, expected {len(batch_chunks)})"
                        )

            if descriptions is None:
                logger.warning(f"Batch {batch_num}: falling back to generic descriptions")
                descriptions = [f"Section {idx + 1} from {file_path}" for idx in batch_indices]

            for orig_idx, description in zip(batch_indices, descriptions):
                all_descriptions[orig_idx] = description

        except Exception as e:
            logger.error(f"Batch {batch_num}: error generating chunk contexts: {e}", exc_info=True)
            for orig_idx in batch_indices:
                all_descriptions[orig_idx] = f"Section {orig_idx + 1} from {file_path}"

    return [all_descriptions[i] for i in range(len(chunks))]
