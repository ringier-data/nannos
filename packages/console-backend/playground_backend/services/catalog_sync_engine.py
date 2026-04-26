"""Catalog sync engine — background tick loop that triggers scheduled catalog syncs.

Follows the same pattern as SchedulerEngine: an asyncio background task that
periodically checks for catalogs due for sync and dispatches sync tasks
via a pluggable :class:`SyncTaskQueue`.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import text

from ..catalog.task_queue import SyncTaskMessage, SyncTaskQueue
from ..repositories.catalog_repository import CatalogRepository

logger = logging.getLogger(__name__)


class CatalogSyncEngine:
    """Background tick loop that discovers due catalogs and enqueues sync tasks.

    The engine is responsible only for *scheduling* — it creates DB jobs
    atomically and places messages on the :class:`SyncTaskQueue`.  Actual
    sync execution is handled by whatever worker consumes the queue.
    """

    def __init__(
        self,
        repo: CatalogRepository,
        task_queue: SyncTaskQueue,
        db_session_factory: Any,
        sync_interval_seconds: int = 86400,
        tick_interval_seconds: int = 300,
    ) -> None:
        self._repo = repo
        self._task_queue = task_queue
        self._db_session_factory = db_session_factory
        self._sync_interval = sync_interval_seconds
        self._tick_interval = tick_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background tick loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="catalog-sync-engine")
        logger.info(
            "Catalog sync engine started (sync_interval=%ds, tick_interval=%ds)",
            self._sync_interval,
            self._tick_interval,
        )

    async def stop(self) -> None:
        """Stop the background tick loop gracefully."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Catalog sync engine stopped")

    async def heal_stuck_jobs(self) -> None:
        """Mark all sync jobs stuck in active states as failed on startup.

        No in-memory task survives a process restart, so ANY job still in
        an active state is orphaned and must be marked failed immediately.
        """
        try:
            async with self._db_session_factory() as db:
                result = await db.execute(
                    text("""
                        UPDATE catalog_sync_jobs
                        SET status = 'failed',
                            completed_at = NOW(),
                            error_details = '{"error": "Sync was interrupted (process restart)"}'::jsonb
                        WHERE status IN ('pending', 'running', 'paused', 'cancelling')
                        RETURNING id
                    """)
                )
                healed = result.rowcount
                await db.commit()
            if healed:
                logger.warning("Healed %d stuck catalog sync job(s) on startup", healed)
        except Exception:
            logger.exception("Failed to heal stuck catalog sync jobs on startup")

    async def _loop(self) -> None:
        """Main tick loop."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Unhandled error in catalog sync engine tick")
            await asyncio.sleep(self._tick_interval)

    async def _tick(self) -> None:
        """Check for catalogs due for sync and enqueue tasks."""
        async with self._db_session_factory() as db:
            catalogs = await self._repo.get_catalogs_due_for_sync(db, self._sync_interval)

        if not catalogs:
            return

        # Filter out catalogs that are already queued or in-flight
        active = self._task_queue.active_catalog_ids()
        eligible = [c for c in catalogs if c.id not in active]

        if not eligible:
            return

        enqueued = 0
        for catalog in eligible:
            async with self._db_session_factory() as db:
                job_id = await self._repo.create_sync_job_atomic(db, catalog.id)
                await db.commit()

            if job_id is None:
                logger.debug("Skipping catalog %s — active sync job already exists", catalog.id)
                continue

            await self._task_queue.enqueue(
                SyncTaskMessage(
                    catalog_id=catalog.id,
                    sync_job_id=job_id,
                    triggered_by="scheduled",
                    user_sub=catalog.owner_user_id,
                )
            )
            enqueued += 1

        if enqueued:
            logger.info("Catalog sync engine enqueued %d sync(s)", enqueued)
