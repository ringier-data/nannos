"""Cost logger for batching and sending usage metrics to backend API."""

import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# Context variables for dynamic access token and user ID (set per-request)
# Private implementation detail - use public functions below
_current_access_token: ContextVar[Optional[str]] = ContextVar("_current_access_token", default=None)
_current_user_sub: ContextVar[Optional[str]] = ContextVar("_current_user_sub", default=None)
_current_conversation_id: ContextVar[Optional[str]] = ContextVar("_current_conversation_id", default=None)

# Cost accumulator for batching multiple embedding calls into a single log entry
# Structure: {"tokens": int, "call_count": int, "provider": str, "model": str}
_cost_accumulator: ContextVar[Optional[Dict[str, Any]]] = ContextVar("_cost_accumulator", default=None)


def set_request_access_token(token: Optional[str]) -> None:
    """
    Set the access token for the current async context (request).

    This token will be used by CostLogger for authenticating API calls
    within the current async context (e.g., during a single A2A request).

    Args:
        token: JWT access token or None to clear

    Example:
        ```python
        async def stream(self, query, user_config, task):
            # Set token for this request context
            set_request_access_token(user_config.access_token)
            try:
                async for event in self._graph.astream(...):
                    yield event
            finally:
                set_request_access_token(None)  # Clean up
        ```
    """
    _current_access_token.set(token)


def set_request_user_sub(user_sub: Optional[str]) -> None:
    """
    Set the user ID for the current async context (request).

    This user ID can be used by tools/interceptors that need user context
    within the current async context (e.g., for MCP credential injection).

    Args:
        user_sub: User identifier or None to clear
    """
    _current_user_sub.set(user_sub)


def set_request_conversation_id(conversation_id: Optional[str]) -> None:
    """
    Set the conversation ID for the current async context (request).

    This conversation ID is used for cost attribution to specific conversations.

    Args:
        conversation_id: Conversation identifier or None to clear
    """
    _current_conversation_id.set(conversation_id)


def get_request_access_token() -> Optional[str]:
    """
    Get the access token for the current async context (request).

    Returns:
        The JWT access token set via set_request_access_token(), or None
    """
    return _current_access_token.get()


def get_request_user_sub() -> Optional[str]:
    """
    Get the user ID for the current async context (request).

    Returns:
        The user ID set via set_request_user_sub(), or None
    """
    return _current_user_sub.get()


def get_request_conversation_id() -> Optional[str]:
    """
    Get the conversation ID for the current async context (request).

    Returns:
        The conversation ID set via set_request_conversation_id(), or None
    """
    return _current_conversation_id.get()


def start_cost_batching() -> None:
    """
    Start accumulating costs in the current context instead of logging immediately.

    Use this before operations that make many small cost-generating calls
    (e.g., document indexing with semantic chunking). Call flush_cost_batch()
    when the operation completes to log the accumulated total.

    Example:
        ```python
        start_cost_batching()
        try:
            # Many embedding calls happen here
            await index_document(content)
        finally:
            await flush_cost_batch(cost_logger)
        ```
    """
    _cost_accumulator.set(
        {
            "tokens": 0,
            "call_count": 0,
            "provider": None,
            "model": None,
        }
    )
    logger.debug("[COST BATCHING] Started cost accumulation")


def is_cost_batching_active() -> bool:
    """
    Check if cost batching is currently active in this context.

    Returns:
        True if costs should be accumulated, False if they should be logged immediately
    """
    return _cost_accumulator.get() is not None


def add_to_cost_batch(
    token_count: int,
    provider: str,
    model_name: str,
) -> None:
    """
    Add costs to the current batch accumulator.

    Only effective if start_cost_batching() was called first.
    If batching is not active, this is a no-op (caller should log directly).

    Args:
        token_count: Number of tokens to add
        provider: Provider name (e.g., 'bedrock_embeddings')
        model_name: Model identifier
    """
    accumulator = _cost_accumulator.get()
    if accumulator is None:
        logger.warning("[COST BATCHING] add_to_cost_batch called but batching not active")
        return

    # Accumulate tokens
    accumulator["tokens"] += token_count
    accumulator["call_count"] += 1

    # Store provider/model from first call
    if accumulator["provider"] is None:
        accumulator["provider"] = provider
        accumulator["model"] = model_name

    logger.debug(
        f"[COST BATCHING] Added {token_count} tokens to batch "
        f"(total: {accumulator['tokens']} tokens, {accumulator['call_count']} calls)"
    )


async def flush_cost_batch(cost_logger: Optional["CostLogger"] = None) -> None:
    """
    Flush accumulated costs as a single log entry and reset the accumulator.

    Args:
        cost_logger: CostLogger instance to log to. If None, costs are discarded.
    """
    accumulator = _cost_accumulator.get()
    if accumulator is None:
        logger.debug("[COST BATCHING] flush_cost_batch called but no accumulator active")
        return

    # Clear the accumulator first to prevent re-entrancy issues
    _cost_accumulator.set(None)

    tokens = accumulator["tokens"]
    call_count = accumulator["call_count"]
    provider = accumulator["provider"]
    model = accumulator["model"]

    if tokens == 0 or call_count == 0:
        logger.debug("[COST BATCHING] No costs to flush")
        return

    if cost_logger is None:
        logger.warning(f"[COST BATCHING] Discarding {tokens} tokens from {call_count} calls (no CostLogger provided)")
        return

    # Get context for attribution
    user_sub = get_request_user_sub()
    conversation_id = get_request_conversation_id()

    if not user_sub:
        logger.warning(f"[COST BATCHING] Discarding {tokens} tokens from {call_count} calls (no user_sub in context)")
        return

    # Log the accumulated total as a single entry
    cost_logger.log_cost_async(
        user_sub=user_sub,
        billing_unit_breakdown={"input_tokens": tokens},
        provider=provider,
        model_name=model,
        conversation_id=conversation_id,
    )

    logger.info(
        f"[COST BATCHING] Flushed batch: {tokens} tokens from {call_count} calls "
        f"(model={model}, conversation={conversation_id})"
    )


def get_request_credentials() -> tuple[Optional[str], Optional[str]]:
    """
    Get both user ID and access token for the current async context.

    Returns:
        Tuple of (user_sub, access_token) or (None, None)
    """
    return get_request_user_sub(), get_request_access_token()


class CostLogger:
    """
    Asynchronous cost logger that batches usage records and sends them to backend API.

    Uses an asyncio queue to batch records and reduce API calls.
    Automatically authenticates using the provided access token.
    """

    def __init__(
        self,
        backend_url: str,
        access_token: Optional[str] = None,
        access_token_provider: Optional[Callable[[], Optional[str]]] = None,
        batch_size: int = 10,
        flush_interval: float = 5.0,
        sub_agent_id: Optional[int] = None,
        scheduled_job_id: Optional[int] = None,
    ):
        """
        Initialize the cost logger.

        Args:
            backend_url: Base URL of the playground backend API (e.g., "https://backend.nannos.ai")
            access_token: Static JWT access token for API authentication (optional if using provider)
            access_token_provider: Callable that returns current access token (for dynamic tokens via ContextVar)
            batch_size: Number of records to batch before sending (default: 10)
            flush_interval: Time in seconds to wait before auto-flushing partial batches (default: 5.0)
            sub_agent_id: Optional sub-agent ID for cost attribution (can be updated dynamically)
            scheduled_job_id: Optional scheduler ID for cost attribution (can be updated dynamically)
        """
        self.backend_url = backend_url.rstrip("/")
        self.access_token = access_token
        self.access_token_provider = access_token_provider or get_request_access_token
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.sub_agent_id = sub_agent_id
        self.scheduled_job_id = scheduled_job_id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._auto_started = False

    async def start(self):
        """Start the background worker task for batch processing.

        Must be called from an async context (when event loop is running).
        Safe to call multiple times (no-op if already started).
        """
        if not self._auto_started:
            self._start_worker()
            self._auto_started = True

    def _start_worker(self):
        """Start the background worker task for batch processing."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._batch_worker())
            logger.info("Cost tracking batch worker started")

    async def _batch_worker(self):
        """Background worker that batches and sends cost records."""
        batch = []
        last_flush = asyncio.get_event_loop().time()

        while not self._shutdown:
            try:
                # Wait for next record or timeout
                timeout = max(0.1, self.flush_interval - (asyncio.get_event_loop().time() - last_flush))
                try:
                    record = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    batch.append(record)
                except asyncio.TimeoutError:
                    pass  # Timeout is normal for auto-flush

                # Send batch if full or time to auto-flush
                current_time = asyncio.get_event_loop().time()
                should_flush = len(batch) >= self.batch_size or (
                    batch and (current_time - last_flush) >= self.flush_interval
                )

                if should_flush:
                    await self._send_batch(batch)
                    batch = []
                    last_flush = current_time

            except Exception as e:
                logger.exception(f"Error in cost tracking batch worker: {e}")
                await asyncio.sleep(1)  # Back off on errors

        # Final flush on shutdown
        if batch:
            await self._send_batch(batch)

    async def _send_batch(self, batch: list[Dict[str, Any]]):
        """Send a batch of cost records to the backend API."""
        # Group records by token (to support multi-user scenarios)
        token_groups = {}
        for record, token in batch:
            token_groups.setdefault(token, []).append(record)

        for token, records in token_groups.items():
            if not token:
                logger.warning(f"No access token available, skipping cost tracking batch for {len(records)} records")
                continue

            # Extract user_sub for context logging
            user_subs = {r.get("user_sub") for r in records}
            user_context = f"users={user_subs}" if len(user_subs) == 1 else f"{len(user_subs)} users"

            try:
                url = urljoin(self.backend_url, "/api/v1/usage/batch-log")
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        url,
                        json={"logs": records},
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                        timeout=30.0,
                    )

                    if response.status_code == 201:
                        logger.info(f"Successfully sent cost batch: {len(records)} records ({user_context})")
                    elif response.status_code in (401, 403):
                        # Authentication/authorization failures - likely token expired or user mismatch
                        logger.error(
                            f"Cost batch authentication failed ({response.status_code}): "
                            f"{len(records)} records ({user_context}). "
                            f"Response: {response.text}"
                        )
                        # Don't retry auth failures - they won't succeed without new token
                    else:
                        # Other errors - log with full context
                        logger.error(
                            f"Failed to send cost batch: {response.status_code} - "
                            f"{len(records)} records ({user_context}). "
                            f"Response: {response.text}"
                        )
            except httpx.TimeoutException as e:
                logger.error(f"Cost batch timeout: {len(records)} records ({user_context}) - {e}")
            except httpx.NetworkError as e:
                logger.error(f"Cost batch network error: {len(records)} records ({user_context}) - {e}")
            except Exception:
                logger.exception(f"Unexpected error sending cost batch: {len(records)} records ({user_context})")

    def log_cost_async(
        self,
        user_sub: str,
        billing_unit_breakdown: Dict[str, int],
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
        langsmith_run_id: Optional[str] = None,
        langsmith_trace_id: Optional[str] = None,
        invoked_at: Optional[datetime] = None,
        _sub_agent_id_from_tag: Optional[int] = None,
        _scheduled_job_id_from_tag: Optional[int] = None,
    ):
        """
        Queue a cost record for async batch sending.

        Args:
            user_sub: User sub who triggered the service call
            billing_unit_breakdown: Dict of billing units to counts (e.g., {'input_tokens': 100, 'output_tokens': 50})
            provider: Optional provider name ('bedrock_converse', 'openai', etc.).
                Not required if mapping against agent specific rate cards.
            model_name: Optional model identifier.
                Not required if mapping against agent specific rate cards.
            conversation_id: Optional conversation/thread ID
            langsmith_run_id: Optional LangSmith run ID
            langsmith_trace_id: Optional LangSmith trace ID (root run)
            invoked_at: Timestamp of invocation (defaults to now)
            _sub_agent_id_from_tag: INTERNAL ONLY - sub_agent_id extracted from LangGraph tags.
                Do not set manually. Automatically instrumented by cost tracking callback.
            _scheduled_job_id_from_tag: INTERNAL ONLY - scheduled_job_id extracted from LangGraph tags.
                Do not set manually. Automatically instrumented by cost tracking callback.
        Note:
            sub_agent_id is automatically extracted from LangGraph tags by CostTrackingCallback.
            Falls back to instance attribute if not provided (for backward compatibility).
        """
        # Use tag-extracted sub_agent_id if available, otherwise fall back to instance attribute
        # Tags are the source of truth for unified tracking across local and remote agents
        sub_agent_id = _sub_agent_id_from_tag if _sub_agent_id_from_tag is not None else self.sub_agent_id
        _scheduled_job_id_from_tag = (
            _scheduled_job_id_from_tag if _scheduled_job_id_from_tag is not None else self.scheduled_job_id
        )
        record = {
            "user_sub": user_sub,
            "provider": provider,
            "model_name": model_name,
            "billing_unit_breakdown": billing_unit_breakdown,
            "conversation_id": conversation_id,
            "sub_agent_id": sub_agent_id,  # From tag (preferred) or instance attribute (fallback)
            "scheduled_job_id": _scheduled_job_id_from_tag,  # From tag (preferred) or instance attribute (fallback)
            "langsmith_run_id": langsmith_run_id,
            "langsmith_trace_id": langsmith_trace_id,
            "invoked_at": (invoked_at or datetime.now(timezone.utc)).isoformat(),
        }

        # Attach the access token at queue time, since ContextVar does not propagate to the batch worker
        token = None
        if self.access_token_provider is not None:
            token = self.access_token_provider()
        if not token:
            logger.debug("No access token available for cost record; will attempt to batch anyway.")
        # Store the token with the record for use in the batch worker since ContextVars do not propagate
        # to the worker task.
        try:
            self._queue.put_nowait((record, token))
        except asyncio.QueueFull:
            logger.warning("Cost tracking queue full, dropping record")

    async def flush(self):
        """Force flush all pending records immediately."""
        # Drain the queue and send everything
        batch = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if batch:
            await self._send_batch(batch)

    async def shutdown(self):
        """Shutdown the cost logger and flush all pending records."""
        logger.info("Shutting down cost logger...")
        self._shutdown = True
        # Wait for worker to finish if it was started
        if self._worker_task is not None and not self._worker_task.done():
            await self._worker_task
        # Final flush
        await self.flush()
        logger.info("Cost logger shutdown complete")
