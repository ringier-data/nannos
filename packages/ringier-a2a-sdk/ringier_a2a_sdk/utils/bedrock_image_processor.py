"""Multi-modal URL → inline-base64 preprocessing.

Some transports cannot ingest binary content by URL and require it inline as
base64. This module downloads from (pre-signed) URLs and rebuilds the block
inline. Two consumers, with different scopes:

**Bedrock Converse** (``preprocess_content_blocks_for_bedrock`` /
``preprocess_messages_for_bedrock``) — images, documents and videos all have to
be inline. While a model may *advertise* a content category as supported (e.g.
Claude on Bedrock supports ``["text", "image", "file"]``), Bedrock only accepts
that content through inline base64 — never a URL.

**OpenAI Chat Completions** (``preprocess_file_blocks_for_chat_completions``) —
``file`` blocks only. ``langchain_core``'s OpenAI block translator maps an image
block's URL to ``image_url`` happily, but *raises* on a ``file`` block carrying a
``url`` ("OpenAI Chat Completions does not support file URLs"): the spec only has
``file_data`` (inline base64) and ``file_id``. That raise happens client-side, so
no amount of downstream normalization (our LiteLLM gateway included) can rescue
it — the block must be inlined before the request payload is built.

This utility is used by:
- LangGraphBedrockAgent._preprocess_input_messages() in langgraph_bedrock.py
- LocalA2ARunnable._apply_provider_transforms() in agent_common
- Orchestrator when preparing inputs for sub-agents
"""

import base64 as b64
import logging
import mimetypes
import re
from typing import Any

import httpx
from langchain_core.messages import (
    ContentBlock,
    FileContentBlock,
    HumanMessage,
    ImageContentBlock,
    TextContentBlock,
    VideoContentBlock,
)

logger = logging.getLogger(__name__)

# Block types that carry binary data Bedrock can only ingest as inline base64,
# mapped to the typed ContentBlock constructor used to build the inlined block.
# Audio is intentionally excluded: Bedrock Converse does not accept audio, so
# audio blocks are filtered/converted to text upstream by input-mode validation.
_BINARY_BLOCK_CONSTRUCTORS = {
    "image": ImageContentBlock,
    "file": FileContentBlock,
    "video": VideoContentBlock,
}
_BINARY_BLOCK_TYPES = tuple(_BINARY_BLOCK_CONSTRUCTORS)

# Default MIME types per block type when a block omits one. Documents have no
# safe default (the format is required and unguessable), so they are left out.
_DEFAULT_MIME_BY_TYPE = {
    "image": "image/png",
}

# Bedrock document names are constrained to alphanumerics, whitespace, hyphens,
# parentheses and square brackets (no dots/extensions). Used to sanitize the
# filename we forward as the document name.
_DOC_NAME_DISALLOWED = re.compile(r"[^a-zA-Z0-9\s\-\(\)\[\]]+")

# Ceiling on what we are willing to inline. base64 inflates by ~4/3 and the whole
# thing then rides in the request body (and into the checkpoint), so a large
# attachment is expensive long before any provider rejects it — every provider
# caps request size well below what a presigned URL can hand us. Past the ceiling
# we degrade to a text description rather than blow up the turn.
_MAX_INLINE_BYTES = 20 * 1024 * 1024


def _filename_from_url(url: str) -> str:
    """Extract a human-readable filename from a (possibly pre-signed) URL."""
    return url.split("/")[-1].split("?")[0] if url else "unknown"


def _resolve_mime_type(block: dict, block_type: str, url: str, filename: str) -> str | None:
    """Resolve a MIME type for a block, inferring from filename/URL when absent.

    Returns None when no MIME type can be determined and there is no safe
    default for the block type (e.g. documents/videos), signalling the caller
    to fall back to a text description rather than send an un-formattable block.
    """
    mime_type = block.get("mime_type") or block.get("mimeType")
    if mime_type:
        return mime_type
    guessed, _ = mimetypes.guess_type(filename or url.split("?")[0])
    if guessed:
        return guessed
    return _DEFAULT_MIME_BY_TYPE.get(block_type)


def _sanitize_document_name(filename: str) -> str:
    """Sanitize a filename into a Bedrock-acceptable document name.

    Bedrock rejects document names containing dots/extensions or special
    characters. Strip the extension, replace disallowed runs with a space and
    collapse whitespace. Falls back to ``document`` when nothing remains.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    cleaned = _DOC_NAME_DISALLOWED.sub(" ", stem).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "document"


def _is_convertible(block: Any, block_types: tuple[str, ...]) -> bool:
    """True if *block* carries binary data via URL that must be inlined."""
    return (
        isinstance(block, dict)
        and block.get("type") in block_types
        and "url" in block
        and "base64" not in block
    )


def _needs_conversion(content: list[Any], block_types: tuple[str, ...] = _BINARY_BLOCK_TYPES) -> bool:
    """True if any block carries binary data via URL that must be inlined."""
    return any(_is_convertible(b, block_types) for b in content)


async def _convert_block(block: dict, *, sanitize_document_name: bool = True) -> list[ContentBlock]:
    """Convert a single URL-based binary block to inline base64 block(s).

    Args:
        block: The URL-based binary content block to inline.
        sanitize_document_name: Strip the extension and special characters from the
            forwarded filename, as Bedrock's document names require. OpenAI Chat
            Completions wants the opposite — it infers the file type from the
            filename's extension — so that path passes ``False``.

    Returns a list of replacement blocks:
    - On success: a text block surfacing the URL (so the LLM can still
      reference it in tool calls) followed by the base64 block.
    - On unresolved MIME type, oversized payload or download failure: a single
      text block describing the file so the turn degrades gracefully instead of
      crashing.
    """
    block_type = block["type"]
    url = block["url"]
    filename = _filename_from_url(url)

    mime_type = _resolve_mime_type(block, block_type, url, filename)
    if not mime_type:
        logger.warning(
            "Cannot determine MIME type for %s block '%s'; forwarding as text description",
            block_type,
            filename,
        )
        return [
            TextContentBlock(
                type="text", text=f"[Attached {block_type}: {filename}, URL: {url}] (unknown type, not loaded)"
            )
        ]

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=60.0)
            resp.raise_for_status()
            raw = resp.content
            if len(raw) > _MAX_INLINE_BYTES:
                logger.warning(
                    "%s block '%s' is %d bytes, over the %d-byte inline ceiling; "
                    "forwarding as text description",
                    block_type.capitalize(),
                    filename,
                    len(raw),
                    _MAX_INLINE_BYTES,
                )
                return [
                    TextContentBlock(
                        type="text",
                        text=(
                            f"[{block_type.capitalize()}: {filename} ({mime_type}), URL: {url}] "
                            f"(too large to attach: {len(raw) // (1024 * 1024)} MB)"
                        ),
                    )
                ]
            b64_data = b64.b64encode(raw).decode("utf-8")
    except Exception:
        logger.warning(
            "Failed to download %s from URL, converting to text description", block_type, exc_info=True
        )
        return [
            TextContentBlock(
                type="text",
                text=f"[{block_type.capitalize()}: {filename} ({mime_type}), URL: {url}] (could not load from URL)",
            )
        ]

    # Bedrock only sees the inlined bytes; surface the URL as text so the LLM
    # can still reference it (e.g. when echoing it into a tool call argument).
    constructor = _BINARY_BLOCK_CONSTRUCTORS[block_type]
    converted = constructor(type=block_type, base64=b64_data, mime_type=mime_type)
    if block_type == "file":
        # Both transports want a name on the document block. ContentBlock has no
        # dedicated name field, but the codebase convention (see
        # content_builder._process_file_part and attachments_store) is a top-level
        # `filename` key, which Bedrock Converse and langchain_core's OpenAI
        # translator both read. Keep that convention.
        raw_name = block.get("filename") or filename
        converted["filename"] = _sanitize_document_name(raw_name) if sanitize_document_name else raw_name  # type: ignore[typeddict-unknown-key]
    logger.info("Converted URL %s to inline base64 (%d chars)", block_type, len(b64_data))
    return [
        TextContentBlock(type="text", text=f"[Attached {block_type}: {filename}, URL: {url}]"),
        converted,
    ]


async def _convert_content_blocks(
    content: list[Any],
    *,
    block_types: tuple[str, ...] = _BINARY_BLOCK_TYPES,
    sanitize_document_name: bool = True,
) -> list[ContentBlock]:
    """Convert URL-based binary blocks in a content list to inline base64.

    Only blocks whose type is in *block_types* are touched; everything else
    (including binary blocks that are already inline) passes through untouched.
    """
    new_blocks: list[ContentBlock] = []
    for block in content:
        if _is_convertible(block, block_types):
            new_blocks.extend(await _convert_block(block, sanitize_document_name=sanitize_document_name))
        else:
            new_blocks.append(block)
    return new_blocks


async def preprocess_messages_for_bedrock(messages: list[HumanMessage]) -> list[HumanMessage]:
    """Convert URL-based binary content to inline base64 for Bedrock Converse API.

    Bedrock's Converse API requires images, documents and videos as inline
    base64 data, not URLs. This function downloads them from pre-signed URLs and
    converts them to base64 before passing to the LLM.

    Args:
        messages: List of LangChain HumanMessage objects (may contain image,
            file or video blocks with URLs)

    Returns:
        List of HumanMessage objects with URL-based binary content converted to
        inline base64

    Note:
        - Content already base64-encoded is left unchanged
        - Content that fails to download is converted to text descriptions
        - The function includes URL information in text blocks so the LLM can
          reference URLs
    """
    processed = []
    for msg in messages:
        # HumanMessage.content can be either str or list[ContentBlock]
        content = msg.content
        if not isinstance(content, list) or not _needs_conversion(content):
            processed.append(msg)
            continue
        processed.append(HumanMessage(content=await _convert_content_blocks(content)))

    return processed


async def preprocess_content_blocks_for_bedrock(content: list[Any]) -> list[Any]:
    """Convert URL-based binary content blocks to inline base64 for Bedrock.

    Lower-level utility that works directly with content blocks instead of
    messages. Useful for orchestrator and middleware that build messages
    dynamically. Handles image, file (document) and video blocks.

    Args:
        content: List of content blocks (dicts with 'type' key)

    Returns:
        List of content blocks with URL-based binary content converted to inline
        base64
    """
    if not _needs_conversion(content):
        return content
    return await _convert_content_blocks(content)


async def preprocess_file_blocks_for_chat_completions(content: list[Any]) -> list[Any]:
    """Inline URL-sourced ``file`` blocks as base64 for OpenAI Chat Completions.

    ``langchain_core`` refuses to serialize a ``file`` block that carries a ``url``
    into a Chat Completions payload — it raises ``ValueError: OpenAI Chat Completions
    does not support file URLs`` while building the request, before anything is sent.
    Inlining the bytes as ``file_data`` (which is what the base64 block becomes) is
    the only representation the API accepts short of a pre-uploaded ``file_id``.

    Scope is deliberately narrow: ``image`` blocks keep their URLs (the translator maps
    those to ``image_url``, and the gateway resolves them), and blocks already carrying
    ``base64`` are untouched. The filename keeps its extension — OpenAI uses it to
    identify the file type.

    Args:
        content: List of content blocks (dicts with a ``type`` key).

    Returns:
        The content blocks with URL-sourced file blocks inlined. Files that cannot be
        downloaded, whose MIME type cannot be resolved, or that exceed the inline size
        ceiling degrade to a text description instead of failing the turn.
    """
    if not _needs_conversion(content, ("file",)):
        return content
    return await _convert_content_blocks(content, block_types=("file",), sanitize_document_name=False)
