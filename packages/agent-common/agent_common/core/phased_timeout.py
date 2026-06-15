"""Two-phase streaming stall timeout for *async* LangChain chat models.

A healthy LLM stream has two very different gaps:

  * **time to first token** — request sent -> first streamed event. This can
    legitimately take several seconds (prompt processing, cold prompt cache,
    large tool-injected prompts, provider tail latency).
  * **between subsequent tokens** — once flowing, events arrive every few
    hundred milliseconds, so a multi-second gap means the stream has stalled.

A single socket-level timeout (botocore ``read_timeout``, openai ``read``) has to
be sized for the *first* gap, leaving it far too loose to detect a mid-stream
stall quickly. This wrapper applies a **generous** timeout to the first event and
a **tight** timeout to every event after it.

On a stall it cancels the underlying stream. This is only safe to use with
genuinely async clients (openai-on-httpx, gemini-on-grpc-aio): cancelling the
``__anext__`` coroutine propagates into the transport and frees the connection
immediately. The sync boto3 client (Bedrock) is deliberately NOT wrapped — there
a cancelled coroutine leaves the socket read blocked in a threadpool thread until
``read_timeout``, so Bedrock relies on botocore ``read_timeout`` instead (see
model_factory; langchain-aws has no async client yet — langchain-aws#663).

If the stall happens BEFORE the first token (nothing emitted yet), the stream is
retried from scratch up to ``first_token_retries`` times — a fresh connection
typically clears a half-open/stalled one. A stall AFTER tokens have already been
emitted cannot be retried safely (partial output was streamed downstream), so it
surfaces as a ``StreamStalledError``.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class StreamStalledError(TimeoutError):
    """Raised when an async chat-model stream stops producing events."""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# Generous: only gates the wait for the first event. Tight: gates each event
# after the first, so a stalled stream is detected in seconds, not minutes.
FIRST_TOKEN_TIMEOUT = _env_float("LLM_STREAM_FIRST_TOKEN_TIMEOUT", 30.0)
INTER_CHUNK_TIMEOUT = _env_float("LLM_STREAM_INTER_CHUNK_TIMEOUT", 5.0)
# How many times to restart the stream when it stalls before the first token.
FIRST_TOKEN_RETRIES = _env_int("LLM_STREAM_FIRST_TOKEN_RETRIES", 2)


def with_phased_stream_timeout(
    inner_cls: type,
    *,
    first_token_timeout: float | None = None,
    inter_chunk_timeout: float | None = None,
    first_token_retries: int | None = None,
) -> type:
    """Return a subclass of ``inner_cls`` whose ``_astream`` enforces a two-phase stall timeout.

    ``inner_cls`` must be an async-capable ``BaseChatModel`` (i.e. implement
    ``_astream``). The override is inert for non-streaming calls (``_agenerate``).
    Tool binding and structured output keep working: ``bind_tools`` /
    ``with_structured_output`` wrap *this* instance, so their streaming flows back
    through the overridden ``_astream``.
    """
    ftt = FIRST_TOKEN_TIMEOUT if first_token_timeout is None else first_token_timeout
    ict = INTER_CHUNK_TIMEOUT if inter_chunk_timeout is None else inter_chunk_timeout
    ftr = FIRST_TOKEN_RETRIES if first_token_retries is None else first_token_retries

    # Real `class` statement (not type()) so zero-arg super() resolves via __class__.
    class _PhasedTimeoutModel(inner_cls):  # type: ignore[misc, valid-type]
        async def _astream(self, *args, **kwargs):
            attempt = 0
            while True:
                agen = super()._astream(*args, **kwargs)
                yielded = False
                stalled = False
                try:
                    while True:
                        timeout = ict if yielded else ftt
                        try:
                            chunk = await asyncio.wait_for(agen.__anext__(), timeout)
                        except StopAsyncIteration:
                            return
                        except asyncio.TimeoutError:
                            stalled = True
                            break
                        yielded = True
                        yield chunk
                finally:
                    # wait_for has already cancelled+awaited the in-flight __anext__,
                    # so the generator is idle and safe to close. Best-effort.
                    try:
                        await agen.aclose()
                    except Exception:  # pragma: no cover - cleanup only
                        pass

                # Reached only when the inner loop broke on a stall.
                if not stalled:  # pragma: no cover - defensive
                    return
                phase_timeout = ict if yielded else ftt
                if not yielded and attempt < ftr:
                    attempt += 1
                    logger.warning(
                        "[STREAM STALL] %s produced no first token in %.1fs; "
                        "restarting stream (attempt %d/%d)",
                        inner_cls.__name__,
                        phase_timeout,
                        attempt,
                        ftr,
                    )
                    continue
                logger.error(
                    "[STREAM STALL] %s stalled: no event for %.1fs (%s phase, "
                    "tokens_emitted=%s, restarts=%d)",
                    inner_cls.__name__,
                    phase_timeout,
                    "inter-chunk" if yielded else "first-token",
                    yielded,
                    attempt,
                )
                raise StreamStalledError(
                    f"{inner_cls.__name__} stream stalled: no event for "
                    f"{phase_timeout:.1f}s ({'inter-chunk' if yielded else 'first-token'} phase)"
                )

    _PhasedTimeoutModel.__name__ = f"PhasedTimeout{inner_cls.__name__}"
    _PhasedTimeoutModel.__qualname__ = _PhasedTimeoutModel.__name__
    return _PhasedTimeoutModel
