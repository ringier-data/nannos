"""A2A Part ↔ LangChain content conversion utilities.

Provides a single function :func:`a2a_parts_to_content` that converts A2A
``Part`` objects into either a plain-text string (``text_only=True``) or a
list of typed LangChain ``ContentBlock`` dicts (``text_only=False``).

This is the canonical conversion used by the steering middleware, the SDK
executor, and the agent-common client runnable.  Do **not** duplicate this
logic elsewhere.
"""

import json
from collections.abc import Sequence
from typing import Any, Literal, overload

from a2a.types import DataPart, FilePart, FileWithBytes, FileWithUri, Part, TextPart
from langchain_core.messages import (
    AudioContentBlock,
    ContentBlock,
    FileContentBlock,
    ImageContentBlock,
    TextContentBlock,
    VideoContentBlock,
)


@overload
def a2a_parts_to_content(parts: Sequence[Part], *, text_only: Literal[True]) -> str: ...


@overload
def a2a_parts_to_content(parts: Sequence[Part], *, text_only: Literal[False] = ...) -> list[ContentBlock]: ...


def a2a_parts_to_content(
    parts: Sequence[Part],
    *,
    text_only: bool = False,
) -> str | list[ContentBlock]:
    """Convert A2A Parts to LangChain content.

    Args:
        parts: Sequence of A2A Part objects.
        text_only: When ``True``, extract only textual content (``TextPart``
            and JSON-serialised ``DataPart``) and return a plain string.
            When ``False`` (default), perform full multi-modal conversion
            including ``FilePart`` and return a list of typed LangChain
            ``ContentBlock`` dicts.

    Returns:
        When ``text_only=True``: concatenated text string (empty if no text).
        When ``text_only=False``: list of ``ContentBlock`` dicts (may be empty).
    """
    if not parts:
        return "" if text_only else []

    if text_only:
        texts: list[str] = []
        for part in parts:
            inner = part.root if hasattr(part, "root") else part
            if isinstance(inner, TextPart):
                texts.append(inner.text)
            elif isinstance(inner, DataPart):
                texts.append(json.dumps(inner.data))
        return "\n".join(texts) if texts else ""

    # ── Full multi-modal conversion ──────────────────────────────────
    blocks: list[ContentBlock] = []
    for part in parts:
        inner = part.root if hasattr(part, "root") else part

        if isinstance(inner, TextPart):
            blocks.append(TextContentBlock(type="text", text=inner.text))
            continue

        if isinstance(inner, FilePart):
            source_kwargs: dict[str, Any] = {}
            if isinstance(inner.file, FileWithUri):
                source_kwargs["url"] = inner.file.uri
            elif isinstance(inner.file, FileWithBytes):
                source_kwargs["base64"] = inner.file.bytes
            else:
                continue  # Unrecognised file variant

            mime_type = inner.file.mime_type

            # Map to the most specific LangChain content-block type
            if mime_type and mime_type.startswith("image/"):
                blocks.append(ImageContentBlock(type="image", **source_kwargs, mime_type=mime_type))
            elif mime_type and mime_type.startswith("audio/"):
                blocks.append(AudioContentBlock(type="audio", **source_kwargs, mime_type=mime_type))
            elif mime_type and mime_type.startswith("video/"):
                blocks.append(VideoContentBlock(type="video", **source_kwargs, mime_type=mime_type))
            else:
                blocks.append(
                    FileContentBlock(
                        type="file",
                        **source_kwargs,
                        mime_type=mime_type if mime_type else "application/octet-stream",
                    )
                )
            continue

        if isinstance(inner, DataPart):
            blocks.append(TextContentBlock(type="text", text=json.dumps(inner.data)))
            continue

    return blocks
