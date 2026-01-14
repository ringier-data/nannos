"""Semantic chunking service with contextual descriptions using Claude + prompt caching.

This module implements semantic document chunking with:
1. Sentence-level text splitting using NLTK
2. Embedding-based boundary detection (Titan Embeddings V2)
3. Batch contextualization with Claude using prompt caching for cost optimization
4. Returns chunks with context descriptions for improved retrieval

Optimized for contracts, papers, and technical documentation (medium-sized documents).
"""

import logging
import re
from typing import Any

import nltk
from langchain_aws import BedrockEmbeddings, ChatBedrockConverse
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# Ensure NLTK punkt tokenizer is available
try:
    nltk.data.find("tokenizers/punkt")
except LookupError:
    logger.info("Downloading NLTK punkt tokenizer...")
    nltk.download("punkt", quiet=True)

# Chunking parameters
DEFAULT_CHUNK_SIZE_WORDS = 500  # Target chunk size in words
SEMANTIC_SIMILARITY_THRESHOLD = 0.6  # Cosine similarity threshold for boundary detection
SLIDING_WINDOW_SENTENCES = 3  # Number of sentences per window for embedding computation


def _count_words(text: str) -> int:
    """Count words in text."""
    return len(text.split())


def _compute_cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    import math

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))

    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0

    return dot_product / (magnitude1 * magnitude2)


async def chunk_with_context(
    content: str,
    metadata: dict[str, Any],
    model: ChatBedrockConverse,
    embeddings_model: BedrockEmbeddings | None = None,
    chunk_size_words: int = DEFAULT_CHUNK_SIZE_WORDS,
) -> list[tuple[str, str, list[float]]]:
    """Chunk text with semantic boundaries and generate contextual descriptions.

    Args:
        content: The document text to chunk
        metadata: Metadata about the document (file_path, etc.)
        model: ChatBedrockConverse model for generating context descriptions
        embeddings_model: BedrockEmbeddings model for boundary detection (optional)
        chunk_size_words: Target chunk size in words

    Returns:
        List of tuples: (chunk_text, context_description, chunk_embedding)
    """
    logger.info(f"Starting semantic chunking for document with {_count_words(content)} words")

    # Initialize embeddings model if not provided
    if embeddings_model is None:
        embeddings_model = BedrockEmbeddings(
            model_id="amazon.titan-embed-text-v2:0",
            region_name=model.region_name,
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

    # Step 2: Group sentences into chunks based on semantic boundaries
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_word_count = 0

    # Compute embeddings for sliding windows to detect semantic boundaries
    window_embeddings: list[list[float]] = []
    for i in range(0, len(sentences), SLIDING_WINDOW_SENTENCES):
        window_text = " ".join(sentences[i : i + SLIDING_WINDOW_SENTENCES])
        if window_text.strip():
            embedding = await embeddings_model.aembed_query(window_text)
            window_embeddings.append(embedding)

    logger.info(f"Computed {len(window_embeddings)} window embeddings for boundary detection")

    # Build chunks respecting semantic boundaries
    window_idx = 0
    for i, sentence in enumerate(sentences):
        sentence_word_count = _count_words(sentence)
        current_chunk.append(sentence)
        current_word_count += sentence_word_count

        # Check if we should create a chunk boundary
        should_break = False

        # Always break if chunk is getting too large (2x target size)
        if current_word_count >= chunk_size_words * 2:
            should_break = True
        # Check semantic boundary if we're near target size
        elif current_word_count >= chunk_size_words:
            # Check cosine similarity between current and next window
            next_window_idx = (i + 1) // SLIDING_WINDOW_SENTENCES
            if window_idx < len(window_embeddings) - 1 and next_window_idx < len(window_embeddings):
                similarity = _compute_cosine_similarity(
                    window_embeddings[window_idx], window_embeddings[next_window_idx]
                )
                # If similarity drops below threshold, it's a good boundary
                if similarity < SEMANTIC_SIMILARITY_THRESHOLD:
                    should_break = True
                    logger.debug(f"Semantic boundary detected at sentence {i} (similarity: {similarity:.3f})")

        if should_break and current_chunk:
            chunk_text = " ".join(current_chunk)
            chunks.append(chunk_text)
            current_chunk = []
            current_word_count = 0
            window_idx = next_window_idx if next_window_idx < len(window_embeddings) else window_idx

    # Add remaining sentences as final chunk
    if current_chunk:
        chunk_text = " ".join(current_chunk)
        chunks.append(chunk_text)

    logger.info(f"Created {len(chunks)} semantic chunks")

    # Step 3: Generate contextual descriptions for ALL chunks in ONE LLM call with prompt caching
    # This uses Claude's prompt caching to cache the full document context across the 5-minute cache window
    chunk_descriptions = await _generate_chunk_contexts_batch(content, chunks, metadata, model)

    # Step 4: Compute embeddings for each chunk
    chunk_embeddings: list[list[float]] = []
    for chunk in chunks:
        embedding = await embeddings_model.aembed_query(chunk)
        chunk_embeddings.append(embedding)

    logger.info(f"Computed embeddings for {len(chunk_embeddings)} chunks")

    # Combine results
    results: list[tuple[str, str, list[float]]] = []
    for chunk, description, embedding in zip(chunks, chunk_descriptions, chunk_embeddings):
        results.append((chunk, description, embedding))

    logger.info(f"Chunking complete: {len(results)} chunks with context and embeddings")
    return results


async def _generate_chunk_contexts_batch(
    full_document: str, chunks: list[str], metadata: dict[str, Any], model: ChatBedrockConverse
) -> list[str]:
    """Generate contextual descriptions for all chunks in a single LLM call with prompt caching.

    Uses Claude's ephemeral prompt caching to cache the full document context for 5 minutes,
    reducing cost by ~10x for repeated contextualizations.
    """
    if not chunks:
        return []

    # Build prompt with cached document context
    file_path = metadata.get("file_path", "document")
    doc_type = metadata.get("type", "document")

    # Format chunks with clear separators
    chunks_text = ""
    for i, chunk in enumerate(chunks, 1):
        chunks_text += f"\n\n--- Chunk {i} ---\n{chunk[:200]}..."  # Preview first 200 chars

    prompt = f"""You are analyzing a {doc_type} to generate brief contextual descriptions for document chunks.

FULL DOCUMENT CONTEXT (use this to understand the overall document):
{full_document}

---

Your task: Generate a brief 1-2 sentence contextual description for each chunk below. Each description should:
1. Summarize what the chunk discusses
2. Mention how it relates to the overall document
3. Include key entities, topics, or concepts
4. Be specific enough to help with semantic search

CHUNKS TO DESCRIBE:
{chunks_text}

Respond with ONLY a JSON array of descriptions, one per chunk:
["description for chunk 1", "description for chunk 2", ...]"""

    # Use Claude with prompt caching
    # The cache_control marker tells Claude to cache everything before this point
    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": prompt,
                "cache_control": {"type": "ephemeral"},  # Cache for 5 minutes
            }
        ]
    )

    logger.info(f"Generating contextual descriptions for {len(chunks)} chunks (with prompt caching)")

    try:
        response = await model.ainvoke([message])
        response_text = response.content

        # Parse JSON array from response
        import json

        # Try to extract JSON array from response
        if isinstance(response_text, str):
            # Look for JSON array in response
            json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
            if json_match:
                descriptions = json.loads(json_match.group(0))
                if len(descriptions) == len(chunks):
                    logger.info(f"Successfully generated {len(descriptions)} contextual descriptions")
                    return descriptions
                else:
                    logger.warning(f"Description count mismatch: got {len(descriptions)}, expected {len(chunks)}")

        # Fallback: generate generic descriptions
        logger.warning("Failed to parse chunk descriptions from LLM response, using generic descriptions")
        return [f"Section {i + 1} from {file_path}" for i in range(len(chunks))]

    except Exception as e:
        logger.error(f"Error generating chunk contexts: {e}", exc_info=True)
        # Fallback: return generic descriptions
        return [f"Section {i + 1} from {file_path}" for i in range(len(chunks))]
