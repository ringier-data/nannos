"""A2A Part ↔ LangChain content conversion utilities.

Provides a single function :func:`a2a_parts_to_content` that converts A2A
``Part`` objects into either a plain-text string (``text_only=True``) or a
list of typed LangChain ``ContentBlock`` dicts (``text_only=False``).

This is the canonical conversion used by the steering middleware, the SDK
executor, and the agent-common client runnable.  Do **not** duplicate this
logic elsewhere.
"""

import base64
import json
from collections.abc import Sequence
from typing import Any, Literal, overload

from a2a.types import Part
from google.protobuf.json_format import MessageToDict
from langchain_core.messages import (
    AudioContentBlock,
    ContentBlock,
    FileContentBlock,
    ImageContentBlock,
    NonStandardContentBlock,
    TextContentBlock,
    VideoContentBlock,
)

# The Part "content" oneof: exactly one of these fields is populated. Annotating the
# WhichOneof result with this alias gives the IDE a precise type (protobuf ships no
# stubs, so the raw return is inferred as Any) and enables branch exhaustiveness.
PartKind = Literal["text", "raw", "url", "data"]


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
        text_only: When ``True``, extract only textual content (parts with a
            ``text`` field, plus JSON-serialised ``data`` parts) and return a
            plain string.  When ``False`` (default), perform full multi-modal
            conversion including file parts (``url``/``raw``) and return a list
            of typed LangChain ``ContentBlock`` dicts.

    Returns:
        When ``text_only=True``: concatenated text string (empty if no text).
        When ``text_only=False``: list of ``ContentBlock`` dicts (may be empty).
    """
    if not parts:
        return "" if text_only else []

    if text_only:
        texts: list[str] = []
        for part in parts:
            kind: PartKind | None = part.WhichOneof("content")
            if kind == "text":
                texts.append(part.text)
            elif kind == "data":
                texts.append(json.dumps(MessageToDict(part.data)))
        return "\n".join(texts) if texts else ""

    # ── Full multi-modal conversion ──────────────────────────────────
    # In A2A v1.0+ a Part is a flat protobuf message whose populated content
    # field (text / raw / url / data) is a oneof named "content"; file metadata
    # is carried alongside in ``media_type`` and ``filename``.
    blocks: list[ContentBlock] = []
    for part in parts:
        kind: PartKind | None = part.WhichOneof("content")

        if kind == "text":
            blocks.append(TextContentBlock(type="text", text=part.text))
            continue

        if kind in ("url", "raw"):
            source_kwargs: dict[str, Any] = {}
            if kind == "url":
                source_kwargs["url"] = part.url
            else:
                # ``raw`` holds the file bytes directly; LangChain expects base64 text.
                source_kwargs["base64"] = base64.b64encode(part.raw).decode()

            mime_type = part.media_type

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

        if kind == "data":
            # TODO: we assume is always application/json, but could be any structured data —
            #       consider allowing explicit media_type for data parts
            data = MessageToDict(part.data)
            blocks.append(
                NonStandardContentBlock(
                    type="non_standard",
                    value={
                        "media_type": "application/json",
                        "data": data,
                    },
                )
            )
            continue

    return blocks
