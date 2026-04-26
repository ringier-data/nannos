"""Cost tracking helper for LLM calls in the playground backend."""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from ringier_a2a_sdk.cost_tracking import CostLogger, CostTrackingCallback

logger = logging.getLogger(__name__)

# Global cost logger instance (HTTP-based, for remote agents)
_cost_logger: CostLogger | None = None


def get_cost_logger() -> CostLogger:
    """Get or create the global cost logger instance.

    Returns:
        CostLogger instance configured for the playground backend.
    """
    global _cost_logger
    if _cost_logger is None:
        backend_url = os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")
        _cost_logger = CostLogger(
            backend_url=backend_url,
            batch_size=10,
            flush_interval=5.0,
        )
        logger.info(f"Initialized cost logger for backend URL: {backend_url}")
    return _cost_logger


def get_llm_cost_callback(user_id: str, sub_agent_id: int | None = None) -> CostTrackingCallback:
    """Create a cost tracking callback for LLM invocations.

    Args:
        user_id: User ID for cost attribution
        sub_agent_id: Optional sub-agent ID for cost attribution

    Returns:
        CostTrackingCallback instance that can be passed to LLM invocations.
    """
    cost_logger = get_cost_logger()
    callback = CostTrackingCallback(cost_logger=cost_logger, sub_agent_id=sub_agent_id)
    logger.info(f"Created cost tracking callback for user_id={user_id}, sub_agent_id={sub_agent_id}")
    return callback


class InternalCostLogger:
    """Cost logger for internal backend use — writes directly to DB, no HTTP.

    The sync pipeline runs inside the same process as the backend, so there's
    no need to POST to the usage API (which requires JWT authentication).
    Instead, we batch records and flush them directly via usage_service.
    """

    def __init__(
        self, usage_service: Any, db_session_factory: Any, batch_size: int = 20, flush_interval: float = 5.0
    ) -> None:
        self._usage_service = usage_service
        self._db_session_factory = db_session_factory
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._shutdown = False

    async def start(self) -> None:
        """Start the background batch worker."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._batch_worker())
            logger.info("Internal cost logger batch worker started")

    def log_cost_async(
        self,
        user_sub: str,
        billing_unit_breakdown: dict[str, int],
        provider: str | None = None,
        model_name: str | None = None,
        conversation_id: str | None = None,
        catalog_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Queue a cost record for async batch writing to DB."""
        record = {
            "user_id": user_sub,
            "provider": provider,
            "model_name": model_name,
            "billing_unit_breakdown": billing_unit_breakdown,
            "conversation_id": conversation_id,
            "catalog_id": catalog_id,
            "invoked_at": datetime.now(timezone.utc),
        }
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning("Internal cost logger queue full, dropping record")

    async def _batch_worker(self) -> None:
        """Background worker that batches and writes cost records to DB."""
        batch: list[dict[str, Any]] = []
        last_flush = asyncio.get_event_loop().time()

        while not self._shutdown:
            try:
                timeout = max(0.1, self._flush_interval - (asyncio.get_event_loop().time() - last_flush))
                try:
                    record = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    batch.append(record)
                except asyncio.TimeoutError:
                    pass

                current_time = asyncio.get_event_loop().time()
                should_flush = len(batch) >= self._batch_size or (
                    batch and (current_time - last_flush) >= self._flush_interval
                )

                if should_flush:
                    await self._flush_batch(batch)
                    batch = []
                    last_flush = current_time

            except Exception:
                logger.exception("Error in internal cost logger batch worker")
                await asyncio.sleep(1)

        if batch:
            await self._flush_batch(batch)

    async def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        """Write a batch of cost records directly to DB via usage_service."""
        if not batch:
            return
        try:
            async with self._db_session_factory() as db:
                await self._usage_service.batch_log_usage(db=db, logs=batch)
                await db.commit()
            logger.info("Internal cost logger flushed %d records to DB", len(batch))
        except Exception:
            logger.exception("Failed to flush %d cost records to DB", len(batch))

    async def flush(self) -> None:
        """Force flush all pending records."""
        batch: list[dict[str, Any]] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._flush_batch(batch)

    async def shutdown(self) -> None:
        """Shutdown and flush remaining records."""
        self._shutdown = True
        if self._worker_task and not self._worker_task.done():
            await self._worker_task
        await self.flush()


# Global internal cost logger instance
_internal_cost_logger: InternalCostLogger | None = None


def get_internal_cost_logger(usage_service: Any, db_session_factory: Any) -> InternalCostLogger:
    """Get or create the internal cost logger for backend-process cost tracking.

    Unlike get_cost_logger() which POSTs to the HTTP API (requiring JWT auth),
    this writes directly to the DB — suitable for background tasks like catalog sync.
    """
    global _internal_cost_logger
    if _internal_cost_logger is None:
        _internal_cost_logger = InternalCostLogger(
            usage_service=usage_service,
            db_session_factory=db_session_factory,
        )
        logger.info("Initialized internal cost logger")
    return _internal_cost_logger
