"""Content builder for converting A2A message parts to LLM input.

This module handles the conversion of A2A message parts into text content
for the orchestrator. Files are NOT directly passed to the LLM - instead,
they are described as references so the orchestrator can decide whether to:
1. Read the file content using tools (to understand and decide next steps)
2. Generate a presigned URL and dispatch to sub-agents without reading

This follows the principle that the orchestrator is a routing/delegation agent
that makes decisions about how to handle files, not a multimodal processor.
"""

import logging
import mimetypes

from a2a.types import FilePart, Part, TextPart

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

    Args:
        uri: The URI of the file
        mime_type: Optional MIME type
        name: Optional file name

    Returns:
        Text description that the orchestrator can use to decide what to do
    """
    parts = []

    # File type indicator
    if mime_type:
        # Normalize video/webm to audio/webm if the file appears to be audio-only
        # (mimetypes.guess_type default to video/webm for .webm)
        display_mime = mime_type
        if mime_type == "video/webm" and name and "recording" in name.lower():
            # Likely an audio recording, not a video
            display_mime = "audio/webm"

        if display_mime.startswith("image/"):
            parts.append("[Image file attached]")
        elif display_mime == "application/pdf":
            parts.append("[PDF document attached]")
        elif display_mime.startswith("text/"):
            parts.append("[Text file attached]")
        elif display_mime.startswith("audio/"):
            parts.append(f"[Audio file attached: {display_mime}]")
        elif display_mime.startswith("video/"):
            parts.append(f"[Video file attached: {display_mime}]")
        else:
            parts.append(f"[File attached: {display_mime}]")
    else:
        parts.append("[File attached]")

    # File name
    if name:
        parts.append(f"Name: {name}")

    parts.append(f"URI: {uri}")

    return " | ".join(parts)


def _process_file_part(part: Part) -> str | None:
    """Process a FilePart and return its text description.

    Args:
        part: A2A Part containing a FilePart

    Returns:
        Text description of the file, or None if not processable
    """
    if not isinstance(part.root, FilePart):
        return None

    file_data = part.root.file

    # Check if it has a URI (FileWithUri)
    if not hasattr(file_data, "uri"):
        logger.warning("FilePart does not have a URI, skipping")
        return None

    uri: str = file_data.uri  # type: ignore[union-attr]

    # # We only handle S3 URIs
    # if not uri.startswith("s3://"):
    #     logger.warning(f"FilePart URI is not an S3 URI: {uri}, skipping")
    #     return None

    mime_type = getattr(file_data, "mimeType", None)
    name = getattr(file_data, "name", None)

    # Try to guess MIME type if not provided
    if not mime_type:
        mime_type = _guess_mime_type(uri, name)

    return _describe_file(uri, mime_type, name)


def build_text_content(
    parts: list[Part],
    user_prefix: str | None = None,
) -> str:
    """Build text content from A2A message parts.

    Files are converted to text descriptions rather than being passed directly
    to the LLM. This allows the orchestrator to decide whether to:
    - Read and understand the file (using read_file tool)
    - Generate a presigned URL for sub-agents (using generate_presigned_url tool)
    - Skip the file entirely

    Args:
        parts: List of A2A Part objects (may contain TextPart, FilePart, etc.)
        user_prefix: Optional user prefix for multi-user attribution (e.g., Slack)
                     Format: "UserName <@SlackHandle>" - will be prepended as "[prefix]:"

    Returns:
        Combined text content with file descriptions
    """
    content_parts: list[str] = []

    # Add user prefix if provided (for Slack multi-user attribution)
    if user_prefix:
        content_parts.append(f"[{user_prefix}]:")

    for part in parts:
        if isinstance(part.root, TextPart):
            content_parts.append(part.root.text)
        elif isinstance(part.root, FilePart):
            description = _process_file_part(part)
            if description:
                content_parts.append(description)
        else:
            # Other part types (DataPart, etc.) - log and skip for now
            logger.debug(f"Skipping unsupported part type: {type(part.root)}")

    return "\n".join(content_parts)
