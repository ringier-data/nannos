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
from typing import AsyncIterator, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class StreamStallError(TimeoutError):
    """Raised when a streamed response stalls past the configured timeout.

    ``phase`` says which budget tripped ("first-token" or "inter-chunk") and
    ``budget`` is the effective timeout in seconds that was exceeded, so callers
    can apply phase-specific recovery policy without parsing the message.
    """

    def __init__(self, message: str, *, phase: str, budget: float) -> None:
        super().__init__(message)
        self.phase = phase
        self.budget = budget


def _env_float(name: str, default: float, *, minimum: float) -> float:
    # A watchdog misconfiguration must degrade to "watchdog too lenient", never to
    # "every request fails": unparsable values fall back to the default and values
    # below `minimum` (0 would make asyncio.wait_for trip instantly) are clamped.
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("[watchdog] invalid %s=%r — using default %.1f", name, raw, default)
        return default
    if value < minimum:
        logger.warning("[watchdog] %s=%s below minimum — clamping to %.1f", name, raw, minimum)
        return minimum
    return value


def first_token_timeout() -> float:
    # TTFT scales with the *uncached* prompt size: a ~150k-token prompt on a cold
    # Bedrock cache takes 30s+ of prompt ingestion before the first token, which is
    # normal operation, not a hang. 90s distinguishes slow ingestion from a genuine
    # stall (the original incident hung for 5+ minutes).
    return _env_float("LLM_FIRST_TOKEN_TIMEOUT", 90.0, minimum=1.0)


def inter_chunk_timeout() -> float:
    # Coarse graph-level idle backstop (see GRANULARITY CAVEAT), not per-token.
    return _env_float("LLM_INTER_CHUNK_TIMEOUT", 60.0, minimum=1.0)


def stall_resume_budget_multiplier() -> float:
    # How much the tripped budget grows for the auto-resume pass. Clamped to >=1 so
    # a misconfigured value can never make the resume run under a SMALLER budget
    # than the pass that already stalled (which would lose the same race again).
    return _env_float("LLM_STALL_RESUME_BUDGET_MULTIPLIER", 3.0, minimum=1.0)


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
            raise StreamStallError(f"{label}: {phase} timeout after {budget:.1f}s", phase=phase, budget=budget) from e
        waiting_for_first = False
        yield chunk


async def watch_stream_with_resume(
    make_stream: Callable[[bool], AsyncIterator[T]],
    *,
    first_timeout: float | None = None,
    chunk_timeout: float | None = None,
    label: str = "llm-stream",
    recovery_part: T | None = None,
) -> AsyncIterator[T]:
    """`watch_stream` plus a single auto-resume from checkpoint after a stall.

    ``make_stream(resuming)`` must build a fresh stream each call: ``resuming=False``
    for the initial pass, ``resuming=True`` after a stall — where it should resume
    pending checkpoint work (e.g. ``graph.astream(None, config, ...)``).

    On a stall, only the budget that tripped (``StreamStallError.phase``) is scaled
    by LLM_STALL_RESUME_BUDGET_MULTIPLIER for the resume pass — retrying it against
    the same effective budget would lose the same race deterministically — while the
    other budget keeps its default so a *different* kind of hang on the resume pass
    is still detected promptly. Scaling starts from the budget the failed pass
    actually used, so the resume is more generous by construction even if the first
    pass ran with an explicit override.

    If ``recovery_part`` is given it is yielded before resuming, letting consumers
    reset parse state and surface a "recovering" status.

    A resume that yields no parts re-raises the original stall instead of returning
    silently: an empty resume means the checkpoint held no pending work (the turn's
    input may never have been persisted), and completing normally would let the
    caller serve stale prior-turn state as this turn's result.

    A second stall propagates to the caller.
    """
    first_budget = first_timeout  # None → env default (see watch_stream)
    chunk_budget = chunk_timeout
    first_stall: StreamStallError | None = None
    while True:
        resuming = first_stall is not None
        parts_seen = False
        try:
            async for part in watch_stream(
                make_stream(resuming),
                label=label,
                first_timeout=first_budget,
                chunk_timeout=chunk_budget,
            ):
                parts_seen = True
                yield part
        except StreamStallError as stall:
            if resuming:
                raise
            first_stall = stall
            logger.warning("[watchdog] %s stalled (%s); auto-resuming once from checkpoint", label, stall)
            if recovery_part is not None:
                yield recovery_part
            if stall.phase == "first-token":
                first_budget = stall.budget * stall_resume_budget_multiplier()
            else:
                chunk_budget = stall.budget * stall_resume_budget_multiplier()
            continue
        if resuming and not parts_seen:
            logger.error("[watchdog] %s resume yielded no parts — checkpoint had no pending work; failing loudly", label)
            raise first_stall
        return
