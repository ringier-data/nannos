"""Content builder for converting A2A message parts to LLM input.

This module handles the conversion of A2A message parts into:
1. **Text content** for the orchestrator LLM - files are described as references
   (type, name) WITHOUT raw URIs, so the LLM can route but never hallucinate URLs.
2. **Typed ContentBlocks** (FileContentBlock, ImageContentBlock, etc.) carrying the
   exact original URIs. These are stored on GraphRuntimeContext.pending_file_blocks
   and injected deterministically into every sub-agent dispatch, bypassing the LLM.

This follows the principle that the orchestrator is a routing/delegation agent
that makes decisions about how to handle files, not a multimodal processor.
"""

import asyncio
import logging
import mimetypes

from a2a.types import FilePart, Part, TextPart
from agent_common.core.object_storage import get_object_storage_service
from langchain_core.messages import (
    AudioContentBlock,
    ContentBlock,
    FileContentBlock,
    ImageContentBlock,
    VideoContentBlock,
)

logger = logging.getLogger(__name__)


def _guess_mime_type(uri: str, name: str | None) -> str | None:
    """Guess MIME type from filename or URI path."""
    # Try from the provided name first
    if name:
        guessed, _ = mimetypes.guess_type(name)
        if guessed:
            return guessed

    # Try from the URI path
    if "/" in uri:
        path = uri.rsplit("/", 1)[-1]
        guessed, _ = mimetypes.guess_type(path)
        if guessed:
            return guessed

    return None


def _describe_file(uri: str, mime_type: str | None, name: str | None) -> str:
    """Create a text description of a file for the orchestrator.

    The description deliberately OMITS the raw URI to prevent the orchestrator LLM
    from attempting to reproduce long pre-signed URLs (which leads to hallucination
    and corrupted security tokens). The actual URIs are carried separately via
    typed ContentBlocks on GraphRuntimeContext.pending_file_blocks.

    Args:
        uri: The URI of the file (used only for S3 URI indicator, not included verbatim)
        mime_type: Optional MIME type
        name: Optional file name

    Returns:
        Text description that the orchestrator can use to decide what to do
    """
    parts = []

    display_name = name or uri.split("/")[-1]  # Use name if available, otherwise last part of URI

    # File type indicator
    if mime_type:
        # Normalize video/webm to audio/webm if the file appears to be audio-only
        # (mimetypes.guess_type default to video/webm for .webm)
        display_mime = mime_type
        if mime_type == "video/webm" and name and "recording" in name.lower():
            # Likely an audio recording, not a video
            display_mime = "audio/webm"

        if display_mime.startswith("image/"):
            parts.append(f"[Image file attached] {display_name} ({display_mime})")
        elif display_mime == "application/pdf":
            parts.append(f"[PDF document attached] {display_name} ({display_mime})")
        elif display_mime.startswith("text/"):
            parts.append(f"[Text file attached] {display_name} ({display_mime})")
        elif display_mime.startswith("audio/"):
            parts.append(f"[Audio file attached] {display_name} ({display_mime})")
        elif display_mime.startswith("video/"):
            parts.append(f"[Video file attached] {display_name} ({display_mime})")
        else:
            parts.append(f"[File attached] {display_name} ({display_mime})")
    else:
        parts.append(f"[File attached] {display_name}")
    return " | ".join(parts)


async def _process_file_part(part: Part) -> tuple[str, ContentBlock] | None:
    """Process a FilePart and return its text description and typed ContentBlock.

    Returns a tuple of:
    - Text description for the orchestrator LLM (no raw URI)
    - ContentBlock (ImageContentBlock or FileContentBlock) carrying the exact URI

    Args:
        part: A2A Part containing a FilePart

    Returns:
        Tuple of (text_description, content_block), or None if not processable
    """
    if not isinstance(part.root, FilePart):
        return None

    file_data = part.root.file

    # Check if it has a URI (FileWithUri)
    if not hasattr(file_data, "uri"):
        logger.warning("FilePart does not have a URI, skipping")
        return None

    uri: str = file_data.uri  # type: ignore[union-attr]
    original_uri = uri  # Store original for logging
    # if uri is a s3 URI, we will generate a pre-signed URL for sub-agents to access the file
    if uri.startswith("s3://") or uri.startswith("file://"):
        try:
            storage_service = get_object_storage_service()
            # Generate presigned URL with 24 hour expiration (max allowed)
            uri = await storage_service.generate_presigned_url(uri, expiration_seconds=86400)
            logger.debug(f"Generated presigned URL for file: {original_uri}")
        except Exception as e:
            logger.warning(f"Failed to generate presigned URL for {original_uri}: {e}")
            # Continue with original URI if presigned URL generation fails

    mime_type = getattr(file_data, "mimeType", None)
    name = getattr(file_data, "name", None)

    # Try to guess MIME type if not provided
    if not mime_type:
        mime_type = _guess_mime_type(uri, name)

    # Build the text description (no raw URI)
    description = _describe_file(uri, mime_type, name)

    # Build the typed ContentBlock carrying the actual URI
    # Use the most specific block type for each media category:
    # - ImageContentBlock for images
    # - AudioContentBlock for audio
    # - VideoContentBlock for video
    # - FileContentBlock for everything else (PDFs, text, unknown)
    content_block: ContentBlock
    if mime_type and mime_type.startswith("image/"):
        content_block = ImageContentBlock(type="image", url=uri, mime_type=mime_type)
    elif mime_type and mime_type.startswith("audio/"):
        content_block = AudioContentBlock(type="audio", url=uri, mime_type=mime_type)
    elif mime_type and mime_type.startswith("video/"):
        content_block = VideoContentBlock(type="video", url=uri, mime_type=mime_type)
    else:
        block_kwargs: dict[str, str] = {"type": "file", "url": uri}
        if mime_type:
            block_kwargs["mime_type"] = mime_type
        content_block = FileContentBlock(**block_kwargs)

    return description, content_block


async def build_text_content(
    parts: list[Part],
    user_prefix: str | None = None,
) -> tuple[str, list[ContentBlock]]:
    """Build text content and file content blocks from A2A message parts.

    Files are converted to:
    1. Text descriptions (type, name, "auto-forwarded") for the orchestrator LLM.
       Raw URIs are deliberately OMITTED to prevent the LLM from hallucinating
       or corrupting long pre-signed URLs.
    2. Typed ContentBlocks (ImageContentBlock, FileContentBlock) carrying the
       exact original URIs for deterministic forwarding to sub-agents.

    Args:
        parts: List of A2A Part objects (may contain TextPart, FilePart, etc.)
        user_prefix: Optional user prefix for multi-user attribution (e.g., Slack)
                     Format: "UserName <@SlackHandle>" - will be prepended as "[prefix]:"

    Returns:
        Tuple of:
        - Combined text content with file descriptions (no raw URIs)
        - List of ContentBlocks carrying the actual file URIs for sub-agent dispatch
    """
    content_parts: list[str] = []
    file_blocks: list[ContentBlock] = []

    # Add user prefix if provided (for Slack multi-user attribution)
    if user_prefix:
        content_parts.append(f"[{user_prefix}]:")

    # Collect file parts with their indices for concurrent processing
    file_part_tasks: list[tuple[int, Part]] = []
    for idx, part in enumerate(parts):
        if isinstance(part.root, FilePart):
            file_part_tasks.append((idx, part))

    # Process all file parts concurrently if any exist
    file_results: dict[int, tuple[str, ContentBlock]] = {}
    if file_part_tasks:
        file_processing_coros = [_process_file_part(part) for _, part in file_part_tasks]
        results = await asyncio.gather(*file_processing_coros)
        for (idx, _), result in zip(file_part_tasks, results):
            if result:
                file_results[idx] = result

    # Build final content in order, using pre-computed file results
    for idx, part in enumerate(parts):
        if isinstance(part.root, TextPart):
            content_parts.append(part.root.text)
        elif isinstance(part.root, FilePart):
            if idx in file_results:
                description, content_block = file_results[idx]
                content_parts.append(description)
                file_blocks.append(content_block)
        else:
            # Other part types (DataPart, etc.) - log and skip for now
            logger.debug(f"Skipping unsupported part type: {type(part.root)}")

    return "\n".join(content_parts), file_blocks
