"""Minimal chat helper for console-backend's own LLM calls via the Model Gateway.

console-backend's utility LLM calls (watch-param generation, catalog summarization)
used raw boto3 `invoke_model`. They now go through the gateway (ADR-0001) like all
other LLM traffic — one OpenAI-compatible call with the app's virtual key. Kept
dependency-light (httpx only; no langchain in console-backend).
"""

import json
import logging
import os

import httpx

from ..config import config

logger = logging.getLogger(__name__)


async def gateway_chat(
    prompt: str,
    *,
    model: str,
    max_tokens: int = 1024,
    metadata: dict | None = None,
    timeout: float = 60.0,
) -> str:
    """Single-turn completion through the gateway; returns the assistant text.

    `metadata` (e.g. {"user_sub": ...}) rides on x-litellm-spend-logs-metadata so the
    proxy attributes the cost (ADR-0002). Without a user_sub the proxy logs nothing.
    """
    headers = {
        "Authorization": f"Bearer {os.getenv('LLM_GATEWAY_API_KEY', '')}",
        "Content-Type": "application/json",
    }
    if metadata:
        headers["x-litellm-spend-logs-metadata"] = json.dumps({k: v for k, v in metadata.items() if v is not None})

    url = f"{config.model_gateway.url.rstrip('/')}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers=headers,
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
