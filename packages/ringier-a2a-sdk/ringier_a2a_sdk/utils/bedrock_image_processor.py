"""Bedrock-specific image URL preprocessing.

Bedrock's Converse API requires images as inline base64 data, not URLs.
This module provides utilities to download images from pre-signed URLs and
convert them to base64 for Bedrock consumption.

This utility is used by:
- LangGraphBedrockAgent._preprocess_input_messages() in langgraph_bedrock.py
- LocalA2ARunnable subclasses that use Bedrock models
- Orchestrator when preparing inputs for Bedrock agents
"""

import base64 as b64
import logging
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, ImageContentBlock, TextContentBlock

logger = logging.getLogger(__name__)


async def preprocess_messages_for_bedrock(messages: list[HumanMessage]) -> list[HumanMessage]:
    """Convert URL-based images to inline base64 for Bedrock Converse API.

    Bedrock's Converse API requires images as inline base64 data, not URLs.
    This function downloads images from pre-signed S3 URLs and converts them to base64
    before passing to the LLM.

    Args:
        messages: List of LangChain HumanMessage objects (may contain image blocks with URLs)

    Returns:
        List of HumanMessage objects with URL-based images converted to inline base64

    Note:
        - Images that are already base64-encoded are left unchanged
        - Images that fail to download are converted to text descriptions
        - The function includes URL information in text blocks so the LLM can reference URLs
    """
    processed = []
    for msg in messages:
        # HumanMessage.content can be either str or list[ContentBlock]
        content = msg.content
        if not isinstance(content, list):
            processed.append(msg)
            continue

        # Check if any URL-based images need conversion
        needs_conversion = any(
            isinstance(b, dict) and b.get("type") == "image" and "url" in b and "base64" not in b for b in content
        )
        if not needs_conversion:
            processed.append(msg)
            continue

        # Convert URL-based images to base64
        new_blocks = []

        for block in content:
            if isinstance(block, dict) and block.get("type") == "image" and "url" in block and "base64" not in block:
                url = block["url"]
                mime_type = block.get("mime_type", "image/png")
                filename = url.split("/")[-1].split("?")[0] if url else "unknown"

                try:
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        resp = await client.get(url, timeout=60.0)
                        resp.raise_for_status()
                        b64_data = b64.b64encode(resp.content).decode("utf-8")

                    # Bedrock only sees the base64 pixels; include URL as text
                    # so the LLM can reference it in tool call arguments
                    new_blocks.append(
                        TextContentBlock(
                            type="text",
                            text=f"[Attached image: {filename}, URL: {url}]",
                        )
                    )
                    new_blocks.append(
                        ImageContentBlock(
                            type="image",
                            base64=b64_data,
                            mime_type=mime_type,
                        )
                    )
                    logger.info(f"Converted URL image to inline base64 ({len(b64_data)} chars)")

                except Exception:
                    logger.warning("Failed to download image from URL, converting to text description", exc_info=True)
                    new_blocks.append(
                        TextContentBlock(
                            type="text",
                            text=f"[Image: {filename} ({mime_type}), URL: {url}] (could not load from URL)",
                        )
                    )
            else:
                new_blocks.append(block)

        processed.append(HumanMessage(content=new_blocks))

    return processed


async def preprocess_content_blocks_for_bedrock(content: list[Any]) -> list[Any]:
    """Convert URL-based images in content blocks to inline base64 for Bedrock.

    Lower-level utility that works directly with content blocks instead of messages.
    Useful for orchestrator and middleware that build messages dynamically.

    Args:
        content: List of content blocks (dicts with 'type' key)

    Returns:
        List of content blocks with URL-based images converted to inline base64
    """
    # Check if any URL-based images need conversion
    needs_conversion = any(
        isinstance(b, dict) and b.get("type") == "image" and "url" in b and "base64" not in b for b in content
    )

    if not needs_conversion:
        return content

    # Convert URL-based images to base64
    new_blocks = []

    for block in content:
        if isinstance(block, dict) and block.get("type") == "image" and "url" in block and "base64" not in block:
            url = block["url"]
            mime_type = block.get("mime_type", "image/png")
            filename = url.split("/")[-1].split("?")[0] if url else "unknown"

            try:
                async_client = httpx.AsyncClient(follow_redirects=True)
                resp = await async_client.get(url, timeout=60.0)
                resp.raise_for_status()
                b64_data = b64.b64encode(resp.content).decode("utf-8")

                # Bedrock only sees the base64 pixels; include URL as text for reference
                new_blocks.append(
                    {
                        "type": "text",
                        "text": f"[Attached image: {filename}, URL: {url}]",
                    }
                )
                new_blocks.append(
                    {
                        "type": "image",
                        "base64": b64_data,
                        "mime_type": mime_type,
                    }
                )
                logger.info(f"Converted URL image to inline base64 ({len(b64_data)} chars)")

            except Exception:
                logger.warning("Failed to download image from URL, converting to text description", exc_info=True)
                new_blocks.append(
                    {
                        "type": "text",
                        "text": f"[Image: {filename} ({mime_type}), URL: {url}] (could not load from URL)",
                    }
                )
        else:
            new_blocks.append(block)

    return new_blocks
