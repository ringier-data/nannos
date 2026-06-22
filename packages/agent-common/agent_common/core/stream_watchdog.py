"""Client-side streaming watchdog (the gated 3-C escalation).

The spike proved the gateway silently ignores `stream_timeout` on Bedrock streaming
(#23375), so proxy timeouts are not sufficient. This wraps an async stream and
enforces a first-token timeout and an inter-chunk (idle) timeout on the client,
cancelling and raising `StreamStallError` if the model hangs mid-stream — the exact
failure that caused the original incident.

Going async (the gateway/ChatOpenAI path) is what makes this possible; the old
synchronous boto3 path could not separate these timeouts.

GRANULARITY CAVEAT: applied around `graph.astream(...)`, this measures the gap
between *graph stream parts*, not between individual LLM tokens. Legitimate gaps
(a long tool call, an A2A sub-agent hop) occur between parts, so the inter-chunk
budget here is a COARSE idle backstop (default 60s) — generous enough not to trip
on normal multi-step execution while still catching a fully hung stream (the 5-min
incident). A true per-token ~5s inter-chunk guard would require wrapping the LLM
stream inside the agent graph, which isn't cleanly exposed; revisit if needed.
"""

import asyncio
import logging
import os
from typing import AsyncIterator, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StreamStallError(TimeoutError):
    """Raised when a streamed response stalls past the configured timeout."""


def first_token_timeout() -> float:
    return float(os.getenv("LLM_FIRST_TOKEN_TIMEOUT", "30"))


def inter_chunk_timeout() -> float:
    # Coarse graph-level idle backstop (see GRANULARITY CAVEAT), not per-token.
    return float(os.getenv("LLM_INTER_CHUNK_TIMEOUT", "60"))


async def watch_stream(
    stream: AsyncIterator[T],
    *,
    first_timeout: float | None = None,
    chunk_timeout: float | None = None,
    label: str = "llm-stream",
) -> AsyncIterator[T]:
    """Yield from `stream`, enforcing first-token and inter-chunk timeouts.

    Raises StreamStallError if no first chunk arrives within `first_timeout`, or
    if the gap between consecutive chunks exceeds `chunk_timeout`.
    """
    ft = first_token_timeout() if first_timeout is None else first_timeout
    ct = inter_chunk_timeout() if chunk_timeout is None else chunk_timeout

    it = stream.__aiter__()
    waiting_for_first = True
    while True:
        budget = ft if waiting_for_first else ct
        try:
            chunk = await asyncio.wait_for(it.__anext__(), timeout=budget)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as e:
            phase = "first-token" if waiting_for_first else "inter-chunk"
            logger.warning("[watchdog] %s %s timeout after %.1fs — aborting stream", label, phase, budget)
            # Best-effort: close the underlying generator so the connection is released.
            aclose = getattr(it, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass
            raise StreamStallError(f"{label}: {phase} timeout after {budget:.1f}s") from e
        waiting_for_first = False
        yield chunk
