"""Bedrock-specific multi-modal URL preprocessing.

Bedrock's Converse API requires binary content (images, documents, videos) as
inline base64 data, not URLs. This module provides utilities to download files
from pre-signed URLs and convert them to base64 for Bedrock consumption.

While a model may *advertise* a content category as supported (e.g. Claude on
Bedrock supports ``["text", "image", "file"]``), Bedrock only accepts that
content through inline base64 — never a URL. These helpers bridge that gap so
the advertised capability is actually honored when the model input is built.

This utility is used by:
- LangGraphBedrockAgent._preprocess_input_messages() in langgraph_bedrock.py
- LocalA2ARunnable subclasses that use Bedrock models
- Orchestrator when preparing inputs for Bedrock agents
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


def _needs_conversion(content: list[Any]) -> bool:
    """True if any block carries binary data via URL that must be inlined."""
    return any(
        isinstance(b, dict)
        and b.get("type") in _BINARY_BLOCK_TYPES
        and "url" in b
        and "base64" not in b
        for b in content
    )


async def _convert_block(block: dict) -> list[ContentBlock]:
    """Convert a single URL-based binary block to inline base64 block(s).

    Returns a list of replacement blocks:
    - On success: a text block surfacing the URL (so the LLM can still
      reference it in tool calls) followed by the base64 block.
    - On unresolved MIME type or download failure: a single text block
      describing the file so the turn degrades gracefully instead of crashing.
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
            b64_data = b64.b64encode(resp.content).decode("utf-8")
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
        # Bedrock requires a (sanitized) name on the document block. ContentBlock
        # has no dedicated name field, but the codebase convention (see
        # content_builder._process_file_part and attachments_store) is a top-level
        # `filename` key, which Bedrock Converse also reads. Keep that convention.
        converted["filename"] = _sanitize_document_name(block.get("filename") or filename)  # type: ignore[typeddict-unknown-key]
    logger.info("Converted URL %s to inline base64 (%d chars)", block_type, len(b64_data))
    return [
        TextContentBlock(type="text", text=f"[Attached {block_type}: {filename}, URL: {url}]"),
        converted,
    ]


async def _convert_content_blocks(content: list[Any]) -> list[ContentBlock]:
    """Convert all URL-based binary blocks in a content list to inline base64."""
    new_blocks: list[ContentBlock] = []
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") in _BINARY_BLOCK_TYPES
            and "url" in block
            and "base64" not in block
        ):
            new_blocks.extend(await _convert_block(block))
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
