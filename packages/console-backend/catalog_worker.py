"""Catalog sync worker — polls for pending sync jobs and executes them.

Runs as a separate Deployment (same codebase, different entrypoint).
Communicates sync progress to console-backend via HTTP webhook,
which relays it to the frontend through Socket.IO.
"""

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("catalog_worker")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.getenv("CATALOG_WORKER_POLL_INTERVAL", "5"))
TICK_INTERVAL = int(os.getenv("CATALOG_SYNC_TICK_INTERVAL_SECONDS", "300"))
SYNC_INTERVAL = int(os.getenv("CATALOG_SYNC_INTERVAL_SECONDS", "86400"))
MAX_CONCURRENT = int(os.getenv("CATALOG_SYNC_MAX_CONCURRENT", "3"))
AUTO_SYNC_ENABLED = os.getenv("CATALOG_AUTO_SYNC_ENABLED", "true").lower() == "true"
REINDEX_HEAL_INTERVAL = int(os.getenv("CATALOG_REINDEX_HEAL_INTERVAL_SECONDS", "600"))
HEARTBEAT_INTERVAL = int(os.getenv("CATALOG_HEARTBEAT_INTERVAL_SECONDS", "10"))
CONSOLE_BACKEND_URL = os.getenv("CONSOLE_BACKEND_URL", "http://console:8080")
HEARTBEAT_PATH = "/tmp/worker-heartbeat"

# Module-level cost logger — initialized in main(), used by sync/reindex jobs.
_cost_logger = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def main() -> None:
    global _cost_logger

    from playground_backend.catalog.executor import shutdown_sync_executor
    from playground_backend.db.connection import close_db, get_async_session_factory, init_db
    from playground_backend.repositories.rate_card_repository import RateCardRepository
    from playground_backend.repositories.usage_repository import UsageRepository
    from playground_backend.services.llm_cost_tracking import InternalCostLogger
    from playground_backend.services.rate_card_service import RateCardService
    from playground_backend.services.usage_service import UsageService

    await init_db()
    logger.info("Database initialized")

    # Set up internal cost logger for LLM usage tracking during syncs
    rate_card_service = RateCardService()
    rate_card_service.set_repository(RateCardRepository())
    usage_service = UsageService()
    usage_service.set_repository(UsageRepository())
    usage_service.set_rate_card_service(rate_card_service)

    _cost_logger = InternalCostLogger(
        usage_service=usage_service,
        db_session_factory=get_async_session_factory(),
    )
    await _cost_logger.start()
    logger.info("Internal cost logger started")

    await _heal_stuck_jobs()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: shutdown_event.set())

    poll_task = asyncio.create_task(_poll_loop(semaphore, shutdown_event), name="poll-loop")
    tick_task = asyncio.create_task(_scheduler_tick_loop(shutdown_event), name="tick-loop")
    heal_task = asyncio.create_task(_reindex_heal_loop(shutdown_event), name="reindex-heal")
    heartbeat_task = asyncio.create_task(_heartbeat_loop(shutdown_event), name="heartbeat-loop")

    logger.info(
        "Catalog worker started (poll=%ds, tick=%ds, heal=%ds, max_concurrent=%d)",
        POLL_INTERVAL,
        TICK_INTERVAL,
        REINDEX_HEAL_INTERVAL,
        MAX_CONCURRENT,
    )

    await shutdown_event.wait()
    logger.info("Shutdown signal received, draining in-flight jobs...")

    poll_task.cancel()
    tick_task.cancel()
    heal_task.cancel()
    heartbeat_task.cancel()
    await asyncio.gather(poll_task, tick_task, heal_task, heartbeat_task, return_exceptions=True)

    # Wait for in-flight sync tasks (bounded by semaphore)
    for _ in range(MAX_CONCURRENT):
        await semaphore.acquire()

    await _cost_logger.shutdown()
    logger.info("Internal cost logger stopped")
    shutdown_sync_executor()
    await close_db()
    logger.info("Catalog worker stopped")


# ---------------------------------------------------------------------------
# Job polling
# ---------------------------------------------------------------------------


async def _poll_loop(semaphore: asyncio.Semaphore, shutdown: asyncio.Event) -> None:
    """Poll for pending sync jobs every POLL_INTERVAL seconds."""
    from playground_backend.db.connection import get_async_session_factory
    from playground_backend.repositories.catalog_repository import CatalogRepository

    repo = CatalogRepository()

    while not shutdown.is_set():
        try:
            session_factory = get_async_session_factory()
            async with session_factory() as db:
                jobs = await repo.claim_pending_jobs(db, limit=MAX_CONCURRENT)
                await db.commit()

            for job in jobs:
                await semaphore.acquire()
                asyncio.create_task(
                    _run_job(job.catalog_id, job.id, job.status, semaphore),
                    name=f"sync-{job.catalog_id}",
                )

        except Exception:
            logger.exception("Error in poll loop")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=POLL_INTERVAL)
            break  # shutdown was set
        except asyncio.TimeoutError:
            pass  # normal timeout, continue polling


async def _run_job(catalog_id: str, sync_job_id: str, job_status: str, semaphore: asyncio.Semaphore) -> None:
    """Execute a single sync or reindex job."""
    try:
        if job_status == "reindexing":
            await _execute_reindex(catalog_id, sync_job_id)
        else:
            await _execute_sync(catalog_id, sync_job_id)
    except Exception:
        logger.exception("Unhandled error in sync job %s (catalog %s)", sync_job_id, catalog_id)
    finally:
        semaphore.release()


async def _execute_sync(catalog_id: str, sync_job_id: str) -> None:
    """Core sync execution — mirrors the old CatalogService.handle_sync_task()."""
    from playground_backend.catalog.adapters.google_drive import GoogleDriveAdapter
    from playground_backend.catalog.sync import CatalogSyncPipeline, normalize_source_config
    from playground_backend.catalog.token_service import CatalogTokenService
    from playground_backend.config import config
    from playground_backend.db.connection import get_async_session_factory

    session_factory = get_async_session_factory()

    # Build the sync pipeline
    pipeline = CatalogSyncPipeline(
        adapter=GoogleDriveAdapter(),
        db_session_factory=session_factory,
        cost_logger=_cost_logger,
    )

    token_service = CatalogTokenService(
        kms_key_id=config.catalog.kms_key_id,
        client_id=config.catalog.google_oauth_client_id,
        client_secret=config.catalog.google_oauth_client_secret.get_secret_value(),
    )

    # Look up catalog
    from playground_backend.repositories.catalog_repository import CatalogRepository

    repo = CatalogRepository()
    async with session_factory() as db:
        catalog = await repo.get_catalog(db, catalog_id)

    if not catalog:
        logger.error("Catalog %s not found; failing sync job %s", catalog_id, sync_job_id)
        async with session_factory() as db:
            await pipeline._update_sync_job(
                db,
                sync_job_id,
                status="failed",
                completed_at=datetime.now(timezone.utc),
                error_details={"error": "Catalog not found"},
            )
        return

    # Set up progress callback (HTTP webhook to console-backend)
    progress_callback = _make_progress_callback(catalog_id, sync_job_id, catalog.owner_user_id)

    pipeline.setup_job(
        sync_job_id=sync_job_id,
        user_sub=catalog.owner_user_id,
        catalog_id=catalog_id,
        progress_callback=progress_callback,
    )

    try:
        # Get OAuth credentials
        async with session_factory() as db:
            credentials = await token_service.get_credentials(db, catalog_id)

        if not credentials:
            logger.error("No active Google connection for catalog %s", catalog_id)
            async with session_factory() as db:
                await pipeline._update_sync_job(
                    db,
                    sync_job_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    error_details={"error": "No active Google connection"},
                )
            return

        source_config = catalog.source_config or {}
        sources = normalize_source_config(source_config)
        has_change_tokens = any(s.get("change_token") for s in sources)

        # Check for prior completed sync
        async with session_factory() as db:
            result = await db.execute(
                text("""
                    SELECT id FROM catalog_sync_jobs
                    WHERE catalog_id = :cid AND status = 'completed'
                    ORDER BY completed_at DESC LIMIT 1
                """),
                {"cid": catalog_id},
            )
            has_prior_sync = result.first() is not None

        if has_prior_sync and has_change_tokens:
            new_tokens = await pipeline.run_incremental_sync(
                catalog_id=catalog_id,
                source_config=source_config,
                sync_job_id=sync_job_id,
                credentials=credentials,
            )
            if new_tokens:
                await _persist_change_tokens(catalog_id, source_config, new_tokens, session_factory)
        else:
            await pipeline.run_full_sync(
                catalog_id=catalog_id,
                source_config=source_config,
                sync_job_id=sync_job_id,
                credentials=credentials,
            )

        logger.info("Sync completed for catalog %s (job %s)", catalog_id, sync_job_id)

    except Exception:
        logger.exception("Sync failed for catalog %s (job %s)", catalog_id, sync_job_id)
        try:
            async with session_factory() as db:
                await pipeline._update_sync_job(
                    db,
                    sync_job_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    error_details={"error": "Unexpected sync failure. Check worker logs."},
                )
        except Exception:
            logger.exception("Failed to mark sync job %s as failed", sync_job_id)
    finally:
        pipeline.teardown_job(sync_job_id)


async def _execute_reindex(catalog_id: str, sync_job_id: str) -> None:
    """Re-index pages with indexed_at = NULL into the vector store."""
    from playground_backend.catalog.adapters.google_drive import GoogleDriveAdapter
    from playground_backend.catalog.sync import CatalogSyncPipeline
    from playground_backend.db.connection import get_async_session_factory
    from playground_backend.repositories.catalog_repository import CatalogRepository

    session_factory = get_async_session_factory()
    repo = CatalogRepository()

    async with session_factory() as db:
        catalog = await repo.get_catalog(db, catalog_id)

    if not catalog:
        logger.error("Catalog %s not found; failing reindex job %s", catalog_id, sync_job_id)
        async with session_factory() as db:
            await repo.update_sync_job(
                db,
                sync_job_id,
                {
                    "status": "failed",
                    "completed_at": datetime.now(timezone.utc),
                    "error_details": {"error": "Catalog not found"},
                },
            )
            await db.commit()
        return

    pipeline = CatalogSyncPipeline(
        adapter=GoogleDriveAdapter(),
        db_session_factory=session_factory,
        cost_logger=_cost_logger,
    )

    progress_callback = _make_reindex_progress_callback(catalog_id, sync_job_id, catalog.owner_user_id)

    pipeline.setup_job(
        sync_job_id=sync_job_id,
        user_sub=catalog.owner_user_id,
        catalog_id=catalog_id,
    )

    try:
        result = await pipeline.reindex_unindexed_pages(
            catalog_id,
            progress_callback=progress_callback,
            sync_job_id=sync_job_id,
        )
        async with session_factory() as db:
            await pipeline._update_sync_job(
                db,
                sync_job_id,
                status="completed",
                completed_at=datetime.now(timezone.utc),
                processed_files=result.get("indexed", 0),
                failed_files=result.get("failed", 0),
            )
        logger.info("Reindex completed for catalog %s (job %s)", catalog_id, sync_job_id)
    except Exception:
        logger.exception("Reindex failed for catalog %s (job %s)", catalog_id, sync_job_id)
        try:
            async with session_factory() as db:
                await pipeline._update_sync_job(
                    db,
                    sync_job_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    error_details={"error": "Unexpected reindex failure. Check worker logs."},
                )
        except Exception:
            logger.exception("Failed to mark reindex job %s as failed", sync_job_id)
    finally:
        pipeline.teardown_job(sync_job_id)


def _make_reindex_progress_callback(catalog_id: str, sync_job_id: str, owner_user_id: str | None) -> Any:
    """Return an async callback for reindex progress (dict-based, not (job_id, fields))."""

    async def _progress(data: dict) -> None:
        if not owner_user_id:
            return
        payload = {
            "job_id": sync_job_id,
            "catalog_id": catalog_id,
            "user_id": owner_user_id,
            "processed_files": data.get("indexed", 0),
            "failed_files": data.get("failed", 0),
            "total": data.get("total", 0),
            "status": "reindexing",
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{CONSOLE_BACKEND_URL}/api/internal/catalog-sync-progress",
                    json=payload,
                )
        except Exception:
            logger.debug("Failed to send reindex progress webhook for job %s", sync_job_id)

    return _progress


# ---------------------------------------------------------------------------
# Progress webhook
# ---------------------------------------------------------------------------


def _make_progress_callback(catalog_id: str, sync_job_id: str, owner_user_id: str | None) -> Any:
    """Return an async callback that POSTs progress updates to console-backend."""

    async def _progress(job_id: str, fields: dict) -> None:
        if not owner_user_id:
            return
        payload: dict[str, Any] = {}
        for k, v in fields.items():
            payload[k] = v.isoformat() if isinstance(v, datetime) else v
        payload["job_id"] = job_id
        payload["catalog_id"] = catalog_id
        payload["user_id"] = owner_user_id

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{CONSOLE_BACKEND_URL}/api/internal/catalog-sync-progress",
                    json=payload,
                )
        except Exception:
            logger.debug("Failed to send progress webhook for job %s", job_id)

    return _progress


# ---------------------------------------------------------------------------
# Scheduled sync tick
# ---------------------------------------------------------------------------


async def _scheduler_tick_loop(shutdown: asyncio.Event) -> None:
    """Periodically check for catalogs due for auto-sync and insert pending jobs."""
    if not AUTO_SYNC_ENABLED:
        logger.info("Auto-sync disabled, scheduler tick loop not started")
        return

    from playground_backend.db.connection import get_async_session_factory
    from playground_backend.repositories.catalog_repository import CatalogRepository

    repo = CatalogRepository()

    while not shutdown.is_set():
        try:
            session_factory = get_async_session_factory()
            async with session_factory() as db:
                catalogs = await repo.get_catalogs_due_for_sync(db, SYNC_INTERVAL)

            enqueued = 0
            for catalog in catalogs:
                async with session_factory() as db:
                    job_id = await repo.create_sync_job_atomic(db, catalog.id)
                    await db.commit()

                if job_id is None:
                    continue

                enqueued += 1
                logger.info(
                    "Scheduled sync for catalog %s (job %s)",
                    catalog.id,
                    job_id,
                )

            if enqueued:
                logger.info("Scheduler tick enqueued %d sync(s)", enqueued)

        except Exception:
            logger.exception("Error in scheduler tick")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=TICK_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Auto-heal: reindex catalogs with un-indexed pages
# ---------------------------------------------------------------------------


async def _reindex_heal_loop(shutdown: asyncio.Event) -> None:
    """Periodically find catalogs with un-indexed pages and create reindex jobs.

    Targets catalogs where:
    - The latest sync job is completed (sync finished normally)
    - There are pages with indexed_at = NULL (indexing partially failed)
    - No active job is already running/pending/reindexing
    """
    from playground_backend.db.connection import get_async_session_factory
    from playground_backend.repositories.catalog_repository import CatalogRepository

    repo = CatalogRepository()

    while not shutdown.is_set():
        try:
            session_factory = get_async_session_factory()
            async with session_factory() as db:
                # Find catalogs with completed last sync but un-indexed pages
                result = await db.execute(
                    text("""
                        SELECT DISTINCT cp.catalog_id
                        FROM catalog_pages cp
                        JOIN catalog_files cf ON cf.id = cp.file_id
                        WHERE cp.indexed_at IS NULL
                          AND cf.indexing_excluded = FALSE
                          -- Only if latest job for this catalog is terminal (completed/failed)
                          AND NOT EXISTS (
                              SELECT 1 FROM catalog_sync_jobs sj
                              WHERE sj.catalog_id = cp.catalog_id
                                AND sj.status IN ('pending', 'running', 'reindexing', 'paused', 'cancelling')
                          )
                    """)
                )
                catalog_ids = [row[0] for row in result.fetchall()]

            enqueued = 0
            for catalog_id in catalog_ids:
                async with session_factory() as db:
                    # Create a reindexing job
                    job_id = await repo.create_sync_job(db, catalog_id)
                    await repo.update_sync_job(db, job_id, {"status": "reindexing"})
                    await db.commit()

                enqueued += 1
                logger.info(
                    "Auto-heal: created reindex job %s for catalog %s",
                    job_id,
                    catalog_id,
                )

            if enqueued:
                logger.info("Reindex heal created %d job(s)", enqueued)

        except Exception:
            logger.exception("Error in reindex heal loop")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=REINDEX_HEAL_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


async def _heal_stuck_jobs() -> None:
    """Mark all sync jobs stuck in active states as failed on startup."""
    from playground_backend.db.connection import get_async_session_factory

    try:
        session_factory = get_async_session_factory()
        async with session_factory() as db:
            result = await db.execute(
                text("""
                    UPDATE catalog_sync_jobs
                    SET status = 'failed',
                        completed_at = NOW(),
                        error_details = '{"error": "Sync was interrupted (worker restart)"}'::jsonb
                    WHERE status IN ('pending', 'running', 'reindexing', 'paused', 'cancelling')
                    RETURNING id
                """)
            )
            healed = result.rowcount
            await db.commit()
        if healed:
            logger.warning("Healed %d stuck catalog sync job(s) on startup", healed)
    except Exception:
        logger.exception("Failed to heal stuck catalog sync jobs on startup")


async def _persist_change_tokens(
    catalog_id: str,
    source_config: dict,
    new_tokens: dict[str, str | None],
    session_factory: Any,
) -> None:
    """Write updated per-source change tokens back to source_config."""
    try:
        from playground_backend.catalog.sync import normalize_source_config

        sources = normalize_source_config(source_config)
        updated = False
        for src in sources:
            token = new_tokens.get(src.get("id", ""))
            if token is not None:
                src["change_token"] = token
                updated = True
        if updated:
            async with session_factory() as db:
                await db.execute(
                    text("UPDATE catalogs SET source_config = :cfg WHERE id = :cid"),
                    {"cfg": json.dumps({"sources": sources}), "cid": catalog_id},
                )
                await db.commit()
    except Exception:
        logger.warning("Failed to persist change tokens for catalog %s", catalog_id)


def _touch_heartbeat() -> None:
    """Touch a file for the K8s exec liveness probe."""
    try:
        with open(HEARTBEAT_PATH, "w") as f:
            f.write(str(datetime.now(timezone.utc).isoformat()))
    except Exception:
        pass


async def _heartbeat_loop(shutdown: asyncio.Event) -> None:
    """Refresh the liveness heartbeat independently of job scheduling.

    Runs as a dedicated task so the kubelet probe stays green even when
    the poll loop is blocked acquiring a busy concurrency slot or waiting
    on slow Drive API calls.
    """
    while not shutdown.is_set():
        _touch_heartbeat()
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=HEARTBEAT_INTERVAL)
            break  # shutdown was set
        except asyncio.TimeoutError:
            pass  # normal tick


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    asyncio.run(main())
