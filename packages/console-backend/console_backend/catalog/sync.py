"""Catalog sync pipeline — orchestrates file extraction, thumbnail upload, and indexing.

Full sync:
  1. Create sync job (status=running)
  2. List files from source adapter
  3. For each file (concurrent, bounded): extract pages, upload thumbnails, generate summary, upsert DB records
  4. Two-pass contextualization: document summary → contextualized page content
  5. Index pages into vector store
  6. Remove deleted files and their pages
  7. Complete sync job

Performance:
  - Files processed concurrently (bounded by RENDER_SLIDE_BUDGET weighted semaphore)
  - Thumbnails fetched & uploaded concurrently (THUMB_CONCURRENCY)
  - Content hashes checked in batch (single query per file)
  - All external calls have retry with exponential backoff
  - PDF/Docs thumbnails: download once, render all pages from cached bytes

Incremental sync:
  Same as full sync but uses adapter.detect_changes() to only process changed files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar
from uuid import uuid4

import aiobotocore.session
import boto3
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from ringier_a2a_sdk.embeddings import GeminiEmbeddings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..catalog.adapters.base import CatalogSourceAdapter, ExtractedPage, ExtractionResourceError, SourceFile
from ..catalog.adapters.google_drive import _PPTX_THUMBNAILS_ENABLED, PPTX_MIME, ExportTimeoutError
from ..catalog.executor import get_io_executor, run_in_sync_executor
from ..catalog.task_queue import (
    FileTaskPayload,
    FileTaskProcessor,
    FileTaskResult,
    InMemoryFileTaskProcessor,
)
from ..catalog.vectorstore.factory import CatalogVectorStoreFactory
from ..catalog.vectorstore.s3_vectors import IMAGE_METADATA_KEY
from ..config import config

if TYPE_CHECKING:
    from ..services.llm_cost_tracking import InternalCostLogger

logger = logging.getLogger(__name__)

_AWS_REGION = os.environ.get("AWS_REGION", "eu-central-1")

# --- Concurrency limits ---
THUMB_CONCURRENCY = int(os.environ.get("CATALOG_THUMB_CONCURRENCY", "10"))
# Slide-weighted budget for file processing.  Each file consumes its page
# count from this budget — many small decks can run in parallel, while a
# single very large deck runs alone.  Gates the entire heavy pipeline
# (extraction + summarization + thumbnail render + indexing).  Choose this
# so the worst-case total live set fits comfortably in the pod's memory
# limit (e.g. 100 slides at ~10 MB peak each ≈ 1 GB).
RENDER_SLIDE_BUDGET = int(os.environ.get("CATALOG_RENDER_SLIDE_BUDGET", "100"))

# --- Memory instrumentation ---
# Set CATALOG_LOG_MEMORY=1 to log RSS at key pipeline stages.
_LOG_MEMORY = os.environ.get("CATALOG_LOG_MEMORY", "0") == "1"


def _log_memory(stage: str, detail: str = "") -> None:
    """Log current process RSS and total cgroup memory for memory debugging.

    Reads /proc/self/status (Python-process VmRSS) and the cgroup memory
    counter (all processes in the container, including pdftoppm / soffice
    subprocesses) on Linux. Silent no-op on other platforms.
    Enabled only when CATALOG_LOG_MEMORY=1 to avoid log noise.
    """
    if not _LOG_MEMORY:
        return
    try:
        rss_mb: int | None = None
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) // 1024
                    break

        # Try cgroup v2 first, fall back to v1.
        cgroup_mb: int | None = None
        for cgroup_path in (
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ):
            try:
                with open(cgroup_path) as f:
                    cgroup_mb = int(f.read().strip()) // (1024 * 1024)
                break
            except OSError:
                pass

        if rss_mb is not None:
            parts = [f"RSS={rss_mb} MB"]
            if cgroup_mb is not None:
                parts.append(f"CGROUP={cgroup_mb} MB")
            mem_str = ", ".join(parts)
            if detail:
                logger.info("MEM[%s] %s: %s", stage, detail, mem_str)
            else:
                logger.info("MEM[%s]: %s", stage, mem_str)
    except Exception:
        pass


# --- File size limits ---
# Files exceeding these limits are marked as "skipped" and not processed.
# Google-native files (Slides, Docs) don't report size — use page count limit.
MAX_FILE_SIZE_MB = int(os.environ.get("CATALOG_MAX_FILE_SIZE_MB", "100"))
MAX_PAGE_COUNT = int(os.environ.get("CATALOG_MAX_PAGE_COUNT", "500"))

# Budget floor for unknown files (prev_page_count=0).  With budget=100 and
# floor=50, at most 2 unknown files run concurrently, bounding the worst-case
# memory when page counts are unknown (before the first extract completes).
# After extract, upgrade() adjusts the hold to the real page count.
_BUDGET_FLOOR = int(os.environ.get("CATALOG_BUDGET_FLOOR", "50"))

# --- Retry configuration ---
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_MAX_DELAY = 30.0  # seconds


class _WeightedSemaphore:
    """FIFO weighted semaphore with two-tier priority.

    Each ``acquire(weight)`` removes ``weight`` units from a fixed
    ``capacity``.  If ``weight`` exceeds the capacity it is capped, so a
    single oversized job runs alone (no other waiters can be granted
    while it holds the full budget).

    Two priority tiers:
    - **High** (upgrade waiters): served first when budget is released.
    - **Normal** (initial-acquire waiters): served only after all
      high-priority waiters that can be satisfied are granted.

    This prevents priority inversion where new tasks steal budget from
    tasks that already have pages in memory and need to upgrade.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._available = self._capacity
        # Normal-priority queue (initial acquires)
        self._waiters: deque[tuple[int, asyncio.Future[None]]] = deque()
        # High-priority queue (upgrades — served first)
        self._upgrade_waiters: deque[tuple[int, asyncio.Future[None]]] = deque()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def available(self) -> int:
        return self._available

    async def acquire(self, weight: int) -> int:
        """Acquire ``weight`` units (capped at capacity).  Normal priority.

        Returns the number of units actually held — callers must pass
        the same value back to :meth:`release`.
        """
        effective = max(1, min(int(weight), self._capacity))
        # Fast path: room is available and no higher-priority or same-priority waiter is queued.
        if not self._upgrade_waiters and not self._waiters and self._available >= effective:
            self._available -= effective
            return effective
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        entry = (effective, fut)
        self._waiters.append(entry)
        try:
            await fut
        except BaseException:
            if entry in self._waiters:
                self._waiters.remove(entry)
            elif fut.done() and not fut.cancelled():
                self.release(effective)
            self._wake_waiters()
            raise
        return effective

    async def _acquire_priority(self, weight: int) -> int:
        """Acquire ``weight`` units with HIGH priority (upgrade path)."""
        effective = max(1, min(int(weight), self._capacity))
        # Fast path: no upgrade waiter ahead of us and budget available.
        if not self._upgrade_waiters and self._available >= effective:
            self._available -= effective
            return effective
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        entry = (effective, fut)
        self._upgrade_waiters.append(entry)
        try:
            await fut
        except BaseException:
            if entry in self._upgrade_waiters:
                self._upgrade_waiters.remove(entry)
            elif fut.done() and not fut.cancelled():
                self.release(effective)
            self._wake_waiters()
            raise
        return effective

    def release(self, weight: int) -> None:
        self._available += weight
        if self._available > self._capacity:
            self._available = self._capacity
        self._wake_waiters()

    def adjust_down(self, current_held: int, new_weight: int) -> int:
        """Release excess budget immediately (non-blocking).

        Only decreases the hold — if *new_weight* >= *current_held*, the
        hold is unchanged.  Returns the new held amount.
        """
        effective_new = max(1, min(int(new_weight), self._capacity))
        if effective_new < current_held:
            self.release(current_held - effective_new)
            return effective_new
        return current_held

    async def upgrade(self, current_held: int, new_weight: int) -> int:
        """Resize hold to *new_weight*, blocking with HIGH priority if more needed.

        If *new_weight* <= *current_held*, releases the excess (non-blocking).
        If *new_weight* > *current_held*, enqueues itself in the high-priority
        queue FIRST, then releases the current hold.  This guarantees that when
        release triggers _wake_waiters(), this upgrade entry is visible and
        will be served before any normal-priority waiter.

        High priority ensures that freed units flow to upgrading tasks (which
        already have pages in memory) before new tasks that haven't started
        extraction yet.  This prevents unbounded pile-up.

        Deadlock-free because:
        - Release-after-enqueue ensures units circulate among upgraders.
        - High-priority queue ensures upgraders are served before new entrants.
        - At least one upgrader can always be satisfied (FIFO within tier).

        Returns the new held amount (equal to min(new_weight, capacity)).
        """
        effective_new = max(1, min(int(new_weight), self._capacity))
        if effective_new <= current_held:
            if effective_new < current_held:
                self.release(current_held - effective_new)
            return effective_new

        # Enqueue in the high-priority queue BEFORE releasing current hold.
        # This ensures _wake_waiters() (triggered by release below) sees
        # this entry and serves it before any normal-priority waiter.
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        entry = (effective_new, fut)
        self._upgrade_waiters.append(entry)

        # Now release — _wake_waiters() will try to grant us immediately
        # if enough budget is available after release.
        self.release(current_held)

        try:
            await fut
        except BaseException:
            if entry in self._upgrade_waiters:
                self._upgrade_waiters.remove(entry)
            elif fut.done() and not fut.cancelled():
                self.release(effective_new)
            self._wake_waiters()
            raise
        return effective_new

    def _wake_waiters(self) -> None:
        """Grant budget to waiters: upgrade (high-priority) first, then normal.

        STRICT PRIORITY: if any unsatisfied upgrade waiter exists, normal
        waiters are completely blocked — even if enough budget is available
        for them.  This prevents new files from entering the pipeline while
        existing files are waiting to upgrade after extraction.
        """
        # Serve upgrade waiters first (FIFO within tier)
        while self._upgrade_waiters:
            head_weight, head_fut = self._upgrade_waiters[0]
            if head_fut.done():
                self._upgrade_waiters.popleft()
                continue
            if self._available < head_weight:
                # Can't satisfy the head upgrade waiter — block ALL normal waiters too.
                return
            self._upgrade_waiters.popleft()
            self._available -= head_weight
            head_fut.set_result(None)
        # Only serve normal waiters when NO upgrade waiters are pending.
        while self._waiters:
            head_weight, head_fut = self._waiters[0]
            if head_fut.done():
                self._waiters.popleft()
                continue
            if self._available < head_weight:
                break
            self._waiters.popleft()
            self._available -= head_weight
            head_fut.set_result(None)

    @asynccontextmanager
    async def slot(self, weight: int):
        held = await self.acquire(weight)
        try:
            yield held
        finally:
            self.release(held)


class FileSkippedError(Exception):
    """Raised when a file exceeds hard limits and should be marked as skipped."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_T = TypeVar("_T")


async def _retry_async(
    coro_factory: Any,
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    operation: str = "operation",
) -> _T:  # type: ignore[type-var]
    """Retry an async operation with exponential backoff and jitter.

    Args:
        coro_factory: A callable that returns a new coroutine each call.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay in seconds.
        operation: Description for logging.
    """
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except (ExportTimeoutError, ExtractionResourceError):
            # These are structural failures — retrying the same file won't help.
            raise
        except Exception:
            if attempt == max_retries:
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            jitter = random.uniform(0, delay * 0.5)
            total_delay = delay + jitter
            logger.warning(
                "Retry %d/%d for %s after %.1fs",
                attempt + 1,
                max_retries,
                operation,
                total_delay,
            )
            await asyncio.sleep(total_delay)


def _content_hash(text_content: str, speaker_notes: str) -> str:
    """Compute sha256 hash of text content + speaker notes."""
    combined = f"{text_content}\n---\n{speaker_notes}"
    return hashlib.sha256(combined.encode()).hexdigest()


def normalize_source_config(source_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of source dicts from a source_config.

    Handles both the new ``{"sources": [...]}`` format and the legacy
    flat ``{"shared_drive_id": "...", "folder_id": "..."}`` format.
    """
    if "sources" in source_config:
        return list(source_config["sources"])
    # Legacy flat format → convert to single-source list
    if source_config.get("shared_drive_id"):
        source: dict[str, Any] = {
            "id": str(uuid4()),
            "drive_id": source_config["shared_drive_id"],
            "drive_name": source_config.get("shared_drive_name", ""),
        }
        if source_config.get("folder_id"):
            source["type"] = "drive_folder"
            source["folder_id"] = source_config["folder_id"]
            source["folder_name"] = source_config.get("folder_name", "")
        else:
            source["type"] = "shared_drive"
        if source_config.get("change_token"):
            source["change_token"] = source_config["change_token"]
        return [source]
    return []


# --- Cooperative state check polling ---
_STATE_CHECK_INTERVAL = 2.0  # seconds between checks when paused
_STATE_CHECK_THROTTLE = 5.0  # minimum seconds between DB checks when running
_MAX_PAUSE_DURATION = 3600.0  # seconds (1 hour) — auto-cancel if paused longer


class SyncCancelled(Exception):
    """Raised when the sync job has been cancelled by the user."""


@dataclass
class _SyncJobState:
    """Per-sync-job state — isolates concurrent syncs from each other."""

    user_sub: str | None = None
    catalog_id: str | None = None
    index_embeddings: GeminiEmbeddings = field(
        default_factory=lambda: GeminiEmbeddings(role="document", executor=get_io_executor())
    )
    query_embeddings: GeminiEmbeddings = field(
        default_factory=lambda: GeminiEmbeddings(role="query", executor=get_io_executor())
    )
    progress_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None


class CatalogSyncPipeline:
    """Orchestrates the catalog synchronization process."""

    def __init__(
        self,
        adapter: CatalogSourceAdapter,
        db_session_factory: Any,
        file_processor: FileTaskProcessor | None = None,
        cost_logger: InternalCostLogger | None = None,
    ) -> None:
        self._adapter = adapter
        self._db_session_factory = db_session_factory
        self._file_processor: FileTaskProcessor = file_processor or InMemoryFileTaskProcessor()
        self._s3_session = aiobotocore.session.get_session()
        self._cost_logger = cost_logger
        # Slide-weighted budget — gates the entire heavy pipeline
        # (extraction + summarization + thumbnail render + indexing) by
        # total slides in flight.  A large deck (>= budget) runs alone;
        # many small decks can run in parallel.
        self._render_budget = _WeightedSemaphore(RENDER_SLIDE_BUDGET)
        # Per-job state — keyed by sync_job_id to isolate concurrent syncs
        self._job_state: dict[str, _SyncJobState] = {}
        # Bedrock client for summarization (Claude Haiku)
        self._bedrock_client = boto3.client("bedrock-runtime", region_name=_AWS_REGION)

    def setup_job(
        self,
        sync_job_id: str,
        user_sub: str | None = None,
        catalog_id: str | None = None,
        progress_callback: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """Register per-job state (cost attribution, embeddings, progress callback).

        Must be called before ``run_full_sync`` / ``run_incremental_sync`` /
        ``reindex_unindexed_pages`` and cleaned up via ``teardown_job()``
        when the sync finishes.
        """
        self._job_state[sync_job_id] = _SyncJobState(
            user_sub=user_sub,
            catalog_id=catalog_id,
            index_embeddings=GeminiEmbeddings(
                role="document",
                cost_logger=self._cost_logger,
                user_sub=user_sub,
                catalog_id=catalog_id,
                executor=get_io_executor(),
            ),
            query_embeddings=GeminiEmbeddings(
                role="query",
                cost_logger=self._cost_logger,
                user_sub=user_sub,
                catalog_id=catalog_id,
                executor=get_io_executor(),
            ),
            progress_callback=progress_callback,
        )

    def teardown_job(self, sync_job_id: str) -> None:
        """Remove per-job state after a sync finishes."""
        self._job_state.pop(sync_job_id, None)

    async def _check_should_continue(self, sync_job_id: str) -> None:
        """Check the DB for pause/cancel requests. Called between files.

        Throttled: skips the DB query if less than _STATE_CHECK_THROTTLE
        seconds have passed since the last check (avoids excessive DB
        round-trips when processing hundreds of files quickly).

        - running → continue
        - paused  → poll until resumed (running) or cancelled (max 1 hour)
        - cancelling/cancelled → raise SyncCancelled
        """
        now = time.monotonic()
        state = self._job_state.get(sync_job_id)
        if state is not None:
            last_check = getattr(state, "_last_state_check", 0.0)
            if now - last_check < _STATE_CHECK_THROTTLE:
                return  # recently checked, skip DB query
            state._last_state_check = now  # type: ignore[attr-defined]

        async with self._db_session_factory() as db:
            pause_started: float | None = None
            while True:
                result = await db.execute(
                    text("SELECT status FROM catalog_sync_jobs WHERE id = :id"),
                    {"id": sync_job_id},
                )
                row = result.mappings().first()
                if not row:
                    logger.warning("Sync job %s not found in DB, treating as cancelled", sync_job_id)
                    raise SyncCancelled()

                status = row["status"]
                if status == "running":
                    return  # proceed
                if status in ("cancelling", "cancelled"):
                    raise SyncCancelled()
                if status == "paused":
                    if pause_started is None:
                        pause_started = time.monotonic()
                    elif time.monotonic() - pause_started > _MAX_PAUSE_DURATION:
                        logger.warning(
                            "Sync job %s paused for > %ds, auto-cancelling",
                            sync_job_id,
                            int(_MAX_PAUSE_DURATION),
                        )
                        raise SyncCancelled()
                    logger.info("Sync job %s paused, waiting for resume...", sync_job_id)
                    await asyncio.sleep(_STATE_CHECK_INTERVAL)
                    continue
                # Any unexpected status — treat as cancelled
                logger.warning("Sync job %s has unexpected status '%s', treating as cancelled", sync_job_id, status)
                raise SyncCancelled()

    def _get_vector_store(self, catalog_id: str, sync_job_id: str | None = None) -> VectorStore:
        """Get or create a vector store for a catalog."""
        state = self._job_state.get(sync_job_id) if sync_job_id else None
        index_emb = state.index_embeddings if state else GeminiEmbeddings(role="document", executor=get_io_executor())
        query_emb = state.query_embeddings if state else GeminiEmbeddings(role="query", executor=get_io_executor())
        return CatalogVectorStoreFactory.create(
            catalog_id=catalog_id,
            index_embedding=index_emb,
            query_embedding=query_emb,
        )

    async def run_full_sync(
        self,
        catalog_id: str,
        source_config: dict[str, Any],
        sync_job_id: str,
        credentials: Any,
    ) -> None:
        """Run a full sync for a catalog with concurrent file processing."""
        async with self._db_session_factory() as db:
            try:
                await self._update_sync_job(db, sync_job_id, status="running", started_at=datetime.now(timezone.utc))

                # Collect files from all configured sources
                sources = normalize_source_config(source_config)
                all_files: list[SourceFile] = []
                seen_file_ids: set[str] = set()
                await self._emit_sync_progress(sync_job_id, current_file_name="Scanning sources...")

                _last_progress_time = 0.0
                _PROGRESS_INTERVAL = 0.5  # seconds — at most 2 DB writes/sec

                async def _scan_progress(msg: str) -> None:
                    nonlocal _last_progress_time
                    now = asyncio.get_event_loop().time()
                    if now - _last_progress_time < _PROGRESS_INTERVAL:
                        return
                    _last_progress_time = now
                    # Use a dedicated session — this callback may be invoked
                    # concurrently from asyncio.gather and a single AsyncSession
                    # does not support concurrent operations.
                    await self._emit_sync_progress(sync_job_id, current_file_name=msg)

                for source in sources:
                    src_config = {**source, "credentials": credentials}
                    src_files = await self._adapter.list_files(src_config, progress_callback=_scan_progress)
                    for f in src_files:
                        if f.id not in seen_file_ids:
                            seen_file_ids.add(f.id)
                            all_files.append(f)
                    await self._emit_sync_progress(
                        sync_job_id,
                        current_file_name=f"Scanning sources... ({len(all_files):,} files found)",
                    )

                total_files = len(all_files)

                # Bulk-register discovered files so the UI shows them immediately
                await self._emit_sync_progress(
                    sync_job_id,
                    current_file_name=f"Registering {total_files:,} files...",
                )
                await self._bulk_register_files(db, catalog_id, all_files)

                # Emit total_files AFTER bulk-register so the frontend knows
                # the file list is ready to be fetched.
                await self._update_sync_job(db, sync_job_id, total_files=total_files)

                logger.info("Sync job %s: processing %d files for catalog %s", sync_job_id, total_files, catalog_id)

                # Build file-level payloads
                payloads = [FileTaskPayload.from_source_file(catalog_id, sync_job_id, f) for f in all_files]

                # Handler closure — captures credentials and db factory.
                # Budget is acquired BEFORE opening a DB session so that at
                # most budget/_BUDGET_FLOOR files hold a connection at once,
                # preventing QueuePool exhaustion.
                async def _handle_file(payload: FileTaskPayload) -> FileTaskResult:
                    file = payload.to_source_file()
                    held = await self._render_budget.acquire(_BUDGET_FLOOR)
                    try:
                        try:
                            async with self._db_session_factory() as file_db:
                                await self._set_file_sync_status(file_db, catalog_id, file.id, "syncing")
                                held = await self._process_file(
                                    file_db, catalog_id, file, credentials, sync_job_id, held
                                )
                                await self._set_file_sync_status(file_db, catalog_id, file.id, "synced")
                        except FileSkippedError as skip_exc:
                            logger.warning(
                                "File %s skipped: %s",
                                file.name,
                                skip_exc.reason,
                            )
                            async with self._db_session_factory() as skip_db:
                                await self._set_file_sync_status(
                                    skip_db,
                                    catalog_id,
                                    file.id,
                                    "skipped",
                                    skip_reason=skip_exc.reason,
                                )
                            # Not a failure — file was intentionally skipped
                            return FileTaskResult(
                                file_id=payload.source_file_id,
                                file_name=payload.source_file_name,
                                success=True,
                            )
                        except Exception:
                            async with self._db_session_factory() as err_db:
                                await self._set_file_sync_status(err_db, catalog_id, file.id, "failed")
                            raise
                        return FileTaskResult(
                            file_id=payload.source_file_id,
                            file_name=payload.source_file_name,
                            success=True,
                        )
                    finally:
                        self._render_budget.release(held)

                # Throttled progress callback — emit at most every 0.5s to
                # avoid flooding the DB and Socket.IO when many files complete
                # rapidly on the fast-path.
                _progress_last_emit = 0.0
                _PROGRESS_EMIT_INTERVAL = 0.5  # seconds

                async def _on_progress(processed: int, failed: int) -> None:
                    nonlocal _progress_last_emit
                    now = time.monotonic()
                    if now - _progress_last_emit < _PROGRESS_EMIT_INTERVAL:
                        return  # skip — will catch up on next call
                    _progress_last_emit = now
                    await self._update_sync_job(
                        db,
                        sync_job_id,
                        processed_files=processed,
                        failed_files=failed,
                    )

                # Fan-out / fan-in via pluggable processor
                batch = await self._file_processor.process_batch(
                    payloads,
                    handler=_handle_file,
                    on_progress=_on_progress,
                    check_cancelled=lambda: self._check_should_continue(sync_job_id),
                )

                # Emit final accurate count (throttle may have skipped the last update)
                await self._emit_sync_progress(
                    sync_job_id,
                    processed_files=batch.processed,
                    failed_files=batch.failed,
                )

                # Remove catalog_files that are no longer in the source
                source_file_ids = {f.id for f in all_files}
                await self._remove_deleted_files(db, catalog_id, source_file_ids)

                # Complete
                await self._update_sync_job(
                    db,
                    sync_job_id,
                    status="completed",
                    completed_at=datetime.now(timezone.utc),
                    processed_files=batch.processed,
                    failed_files=batch.failed,
                    **({"error_details": batch.errors} if batch.errors else {}),
                )
                await self._update_catalog_synced(db, catalog_id)
                await db.commit()

                logger.info(
                    "Sync job %s completed: %d processed, %d failed out of %d total",
                    sync_job_id,
                    batch.processed,
                    batch.failed,
                    total_files,
                )

            except SyncCancelled:
                logger.info("Sync job %s cancelled for catalog %s", sync_job_id, catalog_id)
                await self._update_sync_job(
                    db,
                    sync_job_id,
                    status="cancelled",
                    completed_at=datetime.now(timezone.utc),
                )
                await db.commit()

            except Exception as exc:
                logger.exception("Sync job %s failed for catalog %s", sync_job_id, catalog_id)
                await self._update_sync_job(
                    db,
                    sync_job_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    error_details={"error": str(exc)},
                )
                await db.commit()

    async def run_incremental_sync(
        self,
        catalog_id: str,
        source_config: dict[str, Any],
        sync_job_id: str,
        credentials: Any,
    ) -> dict[str, str | None]:
        """Run an incremental sync with concurrent file processing.

        Returns a dict mapping source ``id`` → new change token (or None).
        """
        async with self._db_session_factory() as db:
            try:
                await self._update_sync_job(db, sync_job_id, status="running", started_at=datetime.now(timezone.utc))

                # Gather changes from all sources
                sources = normalize_source_config(source_config)
                all_changed: list[SourceFile] = []
                all_deleted: list[str] = []
                new_tokens: dict[str, str | None] = {}
                seen_file_ids: set[str] = set()
                await self._emit_sync_progress(sync_job_id, current_file_name="Detecting changes...")

                for source in sources:
                    src_config = {**source, "credentials": credentials}
                    since_token = source.get("change_token")
                    changeset = await self._adapter.detect_changes(src_config, since_token=since_token)
                    new_tokens[source["id"]] = changeset.new_page_token
                    for f in changeset.added + changeset.modified:
                        if f.id not in seen_file_ids:
                            seen_file_ids.add(f.id)
                            all_changed.append(f)
                    all_deleted.extend(changeset.deleted_ids)

                total_files = len(all_changed) + len(all_deleted)
                await self._update_sync_job(db, sync_job_id, total_files=total_files)

                # Build file-level payloads
                payloads = [FileTaskPayload.from_source_file(catalog_id, sync_job_id, f) for f in all_changed]

                # Handler closure — acquires budget BEFORE opening a DB session
                # so that at most budget/_BUDGET_FLOOR files hold a connection
                # at once, preventing QueuePool exhaustion.
                async def _handle_file(payload: FileTaskPayload) -> FileTaskResult:
                    file = payload.to_source_file()
                    held = await self._render_budget.acquire(_BUDGET_FLOOR)
                    try:
                        try:
                            async with self._db_session_factory() as file_db:
                                await self._set_file_sync_status(file_db, catalog_id, file.id, "syncing")
                                held = await self._process_file(
                                    file_db, catalog_id, file, credentials, sync_job_id, held
                                )
                                await self._set_file_sync_status(file_db, catalog_id, file.id, "synced")
                        except FileSkippedError as skip_exc:
                            logger.warning(
                                "File %s skipped: %s",
                                file.name,
                                skip_exc.reason,
                            )
                            async with self._db_session_factory() as skip_db:
                                await self._set_file_sync_status(
                                    skip_db,
                                    catalog_id,
                                    file.id,
                                    "skipped",
                                    skip_reason=skip_exc.reason,
                                )
                            return FileTaskResult(
                                file_id=payload.source_file_id,
                                file_name=payload.source_file_name,
                                success=True,
                            )
                        except Exception:
                            async with self._db_session_factory() as err_db:
                                await self._set_file_sync_status(err_db, catalog_id, file.id, "failed")
                            raise
                        return FileTaskResult(
                            file_id=payload.source_file_id,
                            file_name=payload.source_file_name,
                            success=True,
                        )
                    finally:
                        self._render_budget.release(held)

                # Throttled progress callback — emit at most every 0.5s
                _progress_last_emit = 0.0
                _PROGRESS_EMIT_INTERVAL = 0.5

                async def _on_progress(processed: int, failed: int) -> None:
                    nonlocal _progress_last_emit
                    now = time.monotonic()
                    if now - _progress_last_emit < _PROGRESS_EMIT_INTERVAL:
                        return
                    _progress_last_emit = now
                    await self._update_sync_job(
                        db,
                        sync_job_id,
                        processed_files=processed,
                        failed_files=failed,
                    )

                # Fan-out / fan-in via pluggable processor
                batch = await self._file_processor.process_batch(
                    payloads,
                    handler=_handle_file,
                    on_progress=_on_progress,
                    check_cancelled=lambda: self._check_should_continue(sync_job_id),
                )

                # Delete removed files
                processed = batch.processed
                for deleted_id in all_deleted:
                    await self._check_should_continue(sync_job_id)
                    await self._delete_file_by_source_id(db, catalog_id, deleted_id)
                    processed += 1

                await self._update_sync_job(
                    db,
                    sync_job_id,
                    status="completed",
                    completed_at=datetime.now(timezone.utc),
                    processed_files=processed,
                    failed_files=batch.failed,
                    **({"error_details": batch.errors} if batch.errors else {}),
                )
                await self._update_catalog_synced(db, catalog_id)
                await db.commit()

                return new_tokens

            except SyncCancelled:
                logger.info("Incremental sync job %s cancelled", sync_job_id)
                await self._update_sync_job(
                    db,
                    sync_job_id,
                    status="cancelled",
                    completed_at=datetime.now(timezone.utc),
                )
                await db.commit()
                return {}

            except Exception as exc:
                logger.exception("Incremental sync job %s failed", sync_job_id)
                await self._update_sync_job(
                    db,
                    sync_job_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc),
                    error_details={"error": str(exc)},
                )
                await db.commit()
                return {}

    async def reindex_unindexed_pages(
        self,
        catalog_id: str,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        sync_job_id: str | None = None,
    ) -> dict[str, int]:
        """Re-index all pages that have indexed_at = NULL.

        All data (text, summary, thumbnails) is already in the DB —
        this only re-builds the embeddings and writes to the vector store.
        Thumbnails are loaded concurrently per batch to avoid blocking.
        Returns {"total": N, "indexed": M, "failed": F}.
        """
        async with self._db_session_factory() as db:
            # Fetch unindexed pages with their file info
            result = await db.execute(
                text("""
                    SELECT cp.id AS page_id, cp.file_id, cp.page_number, cp.title,
                           cp.text_content, cp.speaker_notes, cp.content_hash,
                           cp.thumbnail_s3_key, cp.source_ref,
                           cf.source_file_id, cf.source_file_name, cf.mime_type,
                           cf.folder_path, cf.page_count, cf.summary
                    FROM catalog_pages cp
                    JOIN catalog_files cf ON cf.id = cp.file_id
                    WHERE cp.catalog_id = :catalog_id
                      AND cp.indexed_at IS NULL
                      AND cf.indexing_excluded = FALSE
                    ORDER BY cf.source_file_name, cp.page_number
                """),
                {"catalog_id": catalog_id},
            )
            rows = [dict(r) for r in result.mappings().all()]

        total = len(rows)
        if total == 0:
            return {"total": 0, "indexed": 0, "failed": 0}

        logger.info("Re-indexing %d unindexed pages for catalog %s", total, catalog_id)

        vector_store = self._get_vector_store(catalog_id, sync_job_id)

        batch_size = 20
        indexed = 0
        failed_count = 0
        thumb_semaphore = asyncio.Semaphore(THUMB_CONCURRENCY)
        bucket = config.catalog.thumbnails_s3_bucket

        # Reuse a single S3 client for all thumbnail loads to avoid
        # per-request TLS handshake overhead (O(n) connections → 1).
        async with self._s3_session.create_client("s3", region_name=_AWS_REGION) as s3_client:

            async def _load_thumbnail(s3_key: str) -> bytes | None:
                """Load a single thumbnail from S3 with concurrency limiting."""
                async with thumb_semaphore:
                    try:
                        resp = await s3_client.get_object(Bucket=bucket, Key=s3_key)
                        return await resp["Body"].read()
                    except Exception:
                        logger.warning("Failed to load thumbnail for re-index: %s", s3_key)
                        return None

            for i in range(0, total, batch_size):
                batch_rows = rows[i : i + batch_size]

                # Load thumbnails concurrently for this batch
                thumb_coros = []
                for row in batch_rows:
                    if row["thumbnail_s3_key"]:
                        thumb_coros.append(_load_thumbnail(row["thumbnail_s3_key"]))
                    else:
                        fut: asyncio.Future[bytes | None] = asyncio.get_running_loop().create_future()
                        fut.set_result(None)
                        thumb_coros.append(fut)
                thumb_results = await asyncio.gather(*thumb_coros)

                # Build documents for this batch
                batch_docs: list[Document] = []
                batch_keys: list[tuple[str, int]] = []
                for row, thumb_bytes in zip(batch_rows, thumb_results):
                    parts = [
                        f'Document: "{row["source_file_name"]}" ({row["folder_path"]})',
                        f"Summary: {row['summary'] or ''}",
                        "",
                        f'Page {row["page_number"]} of {row["page_count"] or "?"}: "{row["title"] or ""}"',
                        "",
                        row["text_content"] or "",
                    ]
                    if row["speaker_notes"]:
                        parts.append(f"\nSpeaker notes: {row['speaker_notes']}")
                    contextualized = "\n".join(parts)

                    meta = {
                        "catalog_id": catalog_id,
                        "file_id": str(row["file_id"]),
                        "page_number": row["page_number"],
                        "mime_type": row["mime_type"],
                        "folder_path": row["folder_path"],
                        "source_file_name": row["source_file_name"],
                        "document_summary": row["summary"] or "",
                        "page_count": row["page_count"] or 0,
                        "title": row["title"] or "",
                        "content": row["text_content"] or "",
                        "speaker_notes": row["speaker_notes"] or "",
                        "thumbnail_s3_key": row["thumbnail_s3_key"] or "",
                        "source_ref": json.dumps(row["source_ref"]) if row["source_ref"] else "{}",
                        "content_hash": row["content_hash"] or "",
                    }

                    if thumb_bytes:
                        meta[IMAGE_METADATA_KEY] = thumb_bytes

                    doc = Document(
                        id=f"{row['source_file_id']}#page_{row['page_number']}",
                        page_content=contextualized,
                        metadata=meta,
                    )
                    batch_docs.append(doc)
                    batch_keys.append((str(row["file_id"]), row["page_number"]))

                # Index batch and update DB
                try:
                    await vector_store.aadd_documents(batch_docs)
                    now = datetime.now(timezone.utc)
                    # Batch-update indexed_at grouped by file_id
                    async with self._db_session_factory() as idx_db:
                        by_file: dict[str, list[int]] = {}
                        for file_id, page_number in batch_keys:
                            by_file.setdefault(file_id, []).append(page_number)
                        for file_id, page_numbers in by_file.items():
                            await idx_db.execute(
                                text("""
                                    UPDATE catalog_pages SET indexed_at = :now
                                    WHERE file_id = :file_id AND page_number = ANY(:page_numbers)
                                """),
                                {"now": now, "file_id": file_id, "page_numbers": page_numbers},
                            )
                        await idx_db.commit()
                    indexed += len(batch_docs)
                    logger.info("Re-indexed batch %d-%d of %d", i + 1, i + len(batch_docs), total)
                    if progress_callback:
                        await progress_callback({"indexed": indexed, "failed": failed_count, "total": total})
                except Exception:
                    logger.exception("Failed to re-index batch %d-%d", i + 1, i + len(batch_docs))
                    failed_count += len(batch_docs)
                    if progress_callback:
                        await progress_callback({"indexed": indexed, "failed": failed_count, "total": total})

        logger.info(
            "Re-index complete for catalog %s: %d total, %d indexed, %d failed",
            catalog_id,
            total,
            indexed,
            failed_count,
        )
        return {"total": total, "indexed": indexed, "failed": failed_count}

    async def _process_file(
        self,
        db: AsyncSession,
        catalog_id: str,
        file: SourceFile,
        credentials: Any,
        sync_job_id: str | None = None,
        held: int = _BUDGET_FLOOR,
    ) -> int:
        """Process a single file with concurrent thumbnails and batch DB ops.

        **Fast-path for unchanged files**: if ``source_modified_at`` has not
        changed since the last sync AND pages are already stored, the file
        is skipped without calling the Google API or Bedrock — saving ~2-3 s
        per file.

        Returns the (possibly upgraded) budget weight held, so the caller can
        release the correct amount.
        """
        # Upsert catalog_files record (also tells us if the source changed)
        file_db_id, content_changed, existing_summary, prev_page_count = await self._upsert_catalog_file(
            db,
            catalog_id,
            file,
            0,  # page_count updated below once known
        )

        # --- Fast-path: file unchanged at the source level -----------------
        if not content_changed:
            # File was already synced and source_modified_at hasn't changed.
            # Verify we actually have pages stored (first sync may have failed).
            existing_hashes = await self._get_all_page_hashes(db, file_db_id)
            if existing_hashes:
                # Also check if any pages are missing thumbnails — if so, fall
                # through to full processing so thumbnails can be retried.
                # Skip retry for PPTX when thumbnails are disabled — they'll
                # never be generated so retrying is pointless.
                all_have_thumbs = all(has_thumb for _, has_thumb in existing_hashes.values())
                if not all_have_thumbs and file.mime_type == PPTX_MIME and not _PPTX_THUMBNAILS_ENABLED:
                    logger.info(
                        "File %s unchanged, %d pages missing thumbnails but PPTX thumbnails disabled, skipping",
                        file.name,
                        sum(1 for _, has_thumb in existing_hashes.values() if not has_thumb),
                    )
                    all_have_thumbs = True
                if all_have_thumbs:
                    logger.info("File %s unchanged (source_modified_at match), skipping", file.name)
                    await db.commit()
                    return held
                logger.info(
                    "File %s unchanged but %d pages missing thumbnails, retrying",
                    file.name,
                    sum(1 for _, has_thumb in existing_hashes.values() if not has_thumb),
                )
            else:
                # No pages yet — fall through to full processing
                logger.debug("File %s unchanged but no pages stored, processing", file.name)

        # --- Full processing path ------------------------------------------
        t_file_start = time.monotonic()

        # Gate 1: file-size check (available for uploaded PPTX/PDF, None for Google-native)
        raw_size = file.metadata.get("size")
        if raw_size is not None:
            size_mb = int(raw_size) / (1024 * 1024)
            if size_mb > MAX_FILE_SIZE_MB:
                raise FileSkippedError(f"File size {size_mb:.0f} MB exceeds limit of {MAX_FILE_SIZE_MB} MB")

        # --- Budget is already held by the caller (_handle_file) -----------
        # Release excess if prev_page_count is actually smaller than the floor.
        if prev_page_count and prev_page_count < held:
            held = self._render_budget.adjust_down(held, prev_page_count)
        _log_memory("budget_acquired", f"{file.name} held={held}/{self._render_budget.capacity}")

        # Extract pages (with retry).
        # ExtractionResourceError (e.g. soffice not found, ODP parse failure)
        # is not retryable — convert immediately to FileSkippedError.
        t0 = time.monotonic()
        try:
            pages = await _retry_async(
                lambda: self._adapter.extract_pages(file, credentials),
                operation=f"extract_pages({file.name})",
            )
        except ExtractionResourceError as exc:
            raise FileSkippedError(f"PPTX extraction failed (resource error): {exc}") from exc
        page_count = len(pages)
        logger.info("extract_pages(%s): %d pages in %.1fs", file.name, page_count, time.monotonic() - t0)
        _log_memory("after_extract", file.name)

        if page_count == 0:
            raise FileSkippedError("Extraction returned 0 pages (empty or corrupted file)")

        # Upgrade budget hold to actual page_count.  Blocks if the budget
        # doesn't have enough free units — this is the key mechanism that
        # prevents too many large decks from being in memory simultaneously.
        # Deadlock-free: files already holding their full budget continue to
        # make forward progress (summary → render → release).
        held = await self._render_budget.upgrade(held, page_count)
        _log_memory("budget_upgraded", f"{file.name} held={held}/{self._render_budget.capacity}")

        # Gate 2: page-count check (works for all file types including Google-native)
        if page_count > MAX_PAGE_COUNT:
            await db.execute(
                text("UPDATE catalog_files SET page_count = :pc, updated_at = :now WHERE id = :fid"),
                {"pc": page_count, "now": datetime.now(timezone.utc), "fid": file_db_id},
            )
            await db.commit()
            raise FileSkippedError(f"Page count {page_count} exceeds limit of {MAX_PAGE_COUNT}")

        # Update page_count now that we know it
        await db.execute(
            text("UPDATE catalog_files SET page_count = :pc, updated_at = :now WHERE id = :fid"),
            {"pc": page_count, "now": datetime.now(timezone.utc), "fid": file_db_id},
        )

        if sync_job_id:
            await self._emit_sync_progress(
                sync_job_id,
                current_file_name=file.name,
                current_file_pages_total=page_count,
                current_file_pages_done=0,
            )

        await self._process_file_inner(
            db,
            catalog_id,
            file_db_id,
            file,
            credentials,
            sync_job_id,
            pages,
            page_count,
            existing_summary,
        )

        logger.info(
            "Processed file %s (%d pages) in %.1fs",
            file.name,
            page_count,
            time.monotonic() - t_file_start,
        )

        return held

    async def _process_file_inner(
        self,
        db: AsyncSession,
        catalog_id: str,
        file_db_id: str,
        file: SourceFile,
        credentials: Any,
        sync_job_id: str | None,
        pages: list[ExtractedPage],
        page_count: int,
        existing_summary: str | None,
    ) -> None:
        """Run summarization, thumbnail render, uploads, and indexing.

        Called inside the slide-budget context manager so memory is bounded.
        """
        # --- Batch fetch existing content hashes (1 query instead of N) ---
        existing_hashes = await self._get_all_page_hashes(db, file_db_id)

        # Determine which pages actually changed OR are missing thumbnails
        changed_pages_info: list[tuple[ExtractedPage, str]] = []
        for page in pages:
            content_hash = _content_hash(page.text_content, page.speaker_notes)
            existing = existing_hashes.get(page.page_number)
            if existing is not None:
                existing_hash, has_thumbnail = existing
                if existing_hash == content_hash and has_thumbnail:
                    logger.debug("Page %d of %s unchanged, skipping", page.page_number, file.name)
                    continue
                if existing_hash == content_hash:
                    logger.debug("Page %d of %s unchanged but missing thumbnail, retrying", page.page_number, file.name)
            changed_pages_info.append((page, content_hash))

        if not changed_pages_info:
            logger.info("All %d pages of %s unchanged, skipping", page_count, file.name)
            # Re-use existing summary if available (no Bedrock call needed)
            if not existing_summary:
                summary = await _retry_async(
                    lambda: self._generate_document_summary(file, pages, sync_job_id),
                    operation=f"summary({file.name})",
                )
                await self._update_file_summary(db, file_db_id, summary)
            await self._remove_extra_pages(db, file_db_id, page_count)
            await db.commit()
            return

        # --- Pass 1: Document summary (once per file, with retry) ---
        t0 = time.monotonic()
        summary = await _retry_async(
            lambda: self._generate_document_summary(file, pages, sync_job_id),
            operation=f"summary({file.name})",
        )
        await self._update_file_summary(db, file_db_id, summary)
        await db.commit()  # Release row lock before the long thumbnail pipeline
        logger.info("summary(%s): %.1fs", file.name, time.monotonic() - t0)
        _log_memory("after_summary", file.name)

        # --- Batch fetch all thumbnails (download PDF/PPTX once, render all) ---
        t0 = time.monotonic()
        changed_page_list = [page for page, _ in changed_pages_info]
        all_thumbnails: dict[int, bytes] = {}
        _log_memory("before_thumbnails", file.name)
        try:
            all_thumbnails = await _retry_async(
                lambda: self._adapter.get_all_thumbnails(file, changed_page_list, credentials),
                operation=f"get_all_thumbnails({file.name})",
            )
        except Exception:
            logger.warning("Failed to get thumbnails for %s, continuing without", file.name, exc_info=True)
        logger.info("thumbnails(%s): %d thumbs in %.1fs", file.name, len(all_thumbnails), time.monotonic() - t0)
        _log_memory("after_thumbnails", file.name)

        # --- Concurrent S3 thumbnail uploads ---
        thumb_semaphore = asyncio.Semaphore(THUMB_CONCURRENCY)

        async def _upload_thumbnail_for_page(
            page: ExtractedPage, content_hash: str
        ) -> tuple[ExtractedPage, str, str | None, bytes | None]:
            async with thumb_semaphore:
                thumbnail_bytes = all_thumbnails.get(page.page_number)
                thumbnail_s3_key = None

                if thumbnail_bytes:
                    try:
                        thumbnail_s3_key = await _retry_async(
                            lambda p=page, tb=thumbnail_bytes: self._upload_thumbnail(
                                catalog_id, file_db_id, p.page_number, tb
                            ),
                            operation=f"upload_thumb({file.name}#p{page.page_number})",
                        )
                    except Exception:
                        logger.warning("Failed to upload thumbnail for page %d of %s", page.page_number, file.name)

                return page, content_hash, thumbnail_s3_key, thumbnail_bytes

        upload_results = await asyncio.gather(
            *[_upload_thumbnail_for_page(page, ch) for page, ch in changed_pages_info]
        )

        # Free the bulk thumbnail dict — individual bytes are still
        # referenced in upload_results tuples for vector indexing.
        del all_thumbnails
        _log_memory("after_s3_upload", file.name)

        # --- Batch DB upserts (single multi-row INSERT) ---
        await self._batch_upsert_catalog_pages(
            db,
            catalog_id=catalog_id,
            file_id=file_db_id,
            rows=[
                (page, content_hash, thumbnail_s3_key) for page, content_hash, thumbnail_s3_key, _tb in upload_results
            ],
        )
        if sync_job_id:
            await self._emit_sync_progress(
                sync_job_id,
                current_file_name=file.name,
                current_file_pages_done=len(upload_results),
                current_file_pages_total=page_count,
            )

        changed_pages = upload_results

        # Remove pages that no longer exist (e.g. slides were deleted)
        await self._remove_extra_pages(db, file_db_id, page_count)
        await db.commit()

        # --- Pass 2: Contextualize & index changed pages into vector store ---
        if changed_pages:
            vector_store = self._get_vector_store(catalog_id, sync_job_id)
            docs: list[Document] = []

            for page, content_hash, thumbnail_s3_key, thumbnail_bytes in changed_pages:
                contextualized = self._build_contextualized_content(file, summary, page, page_count)

                meta = {
                    # Filterable metadata
                    "catalog_id": catalog_id,
                    "file_id": file_db_id,
                    "page_number": page.page_number,
                    "mime_type": file.mime_type,
                    "folder_path": file.folder_path,
                    "content_type": page.source_ref.get("type", ""),
                    # Non-filterable metadata (large text fields)
                    "source_file_name": file.name,
                    "document_summary": summary,
                    "page_count": page_count,
                    "title": (page.title or "")[:500],
                    "content": page.text_content,
                    "speaker_notes": page.speaker_notes,
                    "thumbnail_s3_key": thumbnail_s3_key or "",
                    "source_ref": json.dumps(page.source_ref),
                    "content_hash": content_hash,
                }

                # Attach thumbnail bytes for multimodal embedding.
                # MultimodalS3Vectors.add_texts() consumes & strips this key.
                if thumbnail_bytes:
                    meta[IMAGE_METADATA_KEY] = thumbnail_bytes

                doc = Document(
                    id=f"{file.id}#page_{page.page_number}",
                    page_content=contextualized,
                    metadata=meta,
                )
                docs.append(doc)

            try:
                await vector_store.aadd_documents(docs)
                # Mark pages as indexed (single batch UPDATE)
                now = datetime.now(timezone.utc)
                page_numbers = [page.page_number for page, _, _, _ in changed_pages]
                async with self._db_session_factory() as index_db:
                    await index_db.execute(
                        text("""
                            UPDATE catalog_pages SET indexed_at = :now
                            WHERE file_id = :file_id AND page_number = ANY(:page_numbers)
                        """),
                        {"now": now, "file_id": file_db_id, "page_numbers": page_numbers},
                    )
                    await index_db.commit()
                logger.info("Indexed %d pages from %s into vector store", len(docs), file.name)
            except Exception:
                logger.exception("Failed to index pages from %s into vector store", file.name)
            _log_memory("after_index", file.name)

    async def _bulk_register_files(
        self,
        db: AsyncSession,
        catalog_id: str,
        files: list[SourceFile],
    ) -> None:
        """Insert all discovered files with sync_status='pending'.

        Uses ON CONFLICT to update metadata for files that already exist
        while resetting their sync_status to pending for the new sync.
        Batches inserts for performance (500 rows per statement).
        """
        if not files:
            return
        now = datetime.now(timezone.utc)
        batch_size = 500

        for batch_start in range(0, len(files), batch_size):
            batch = files[batch_start : batch_start + batch_size]
            # Build multi-row VALUES clause
            value_clauses = []
            params: dict[str, Any] = {"catalog_id": catalog_id, "now": now}
            for idx, file in enumerate(batch):
                prefix = f"f{idx}"
                value_clauses.append(
                    f"(:{prefix}_id, :catalog_id, :{prefix}_sfid, :{prefix}_name, :{prefix}_mime,"
                    f" :{prefix}_folder, 0, CAST(:{prefix}_meta AS jsonb), :{prefix}_mod, 'pending', :now, :now)"
                )
                params[f"{prefix}_id"] = str(uuid4())
                params[f"{prefix}_sfid"] = file.id
                params[f"{prefix}_name"] = file.name
                params[f"{prefix}_mime"] = file.mime_type
                params[f"{prefix}_folder"] = file.folder_path
                params[f"{prefix}_meta"] = json.dumps(file.metadata)
                params[f"{prefix}_mod"] = file.modified_at

            values_sql = ",\n".join(value_clauses)
            await db.execute(
                text(f"""
                    INSERT INTO catalog_files (id, catalog_id, source_file_id, source_file_name, mime_type,
                        folder_path, page_count, metadata, source_modified_at, sync_status, created_at, updated_at)
                    VALUES {values_sql}
                    ON CONFLICT (catalog_id, source_file_id)
                    DO UPDATE SET
                        source_file_name = EXCLUDED.source_file_name,
                        mime_type = EXCLUDED.mime_type,
                        folder_path = EXCLUDED.folder_path,
                        metadata = EXCLUDED.metadata,
                        source_modified_at = EXCLUDED.source_modified_at,
                        sync_status = 'pending',
                        updated_at = EXCLUDED.updated_at
                """),
                params,
            )

        await db.commit()
        logger.info("Bulk-registered %d files for catalog %s", len(files), catalog_id)

    async def _set_file_sync_status(
        self,
        db: AsyncSession,
        catalog_id: str,
        source_file_id: str,
        status: str,
        *,
        skip_reason: str | None = None,
    ) -> None:
        """Update sync_status (and optionally skip_reason) for a single file."""
        await db.execute(
            text("""
                UPDATE catalog_files
                SET sync_status = :status,
                    skip_reason = :skip_reason,
                    updated_at = :now
                WHERE catalog_id = :catalog_id AND source_file_id = :source_file_id
            """),
            {
                "status": status,
                "catalog_id": catalog_id,
                "source_file_id": source_file_id,
                "skip_reason": skip_reason,
                "now": datetime.now(timezone.utc),
            },
        )
        await db.commit()

    async def _upsert_catalog_file(
        self,
        db: AsyncSession,
        catalog_id: str,
        file: SourceFile,
        page_count: int,
    ) -> tuple[str, bool, str | None, int]:
        """Upsert a catalog_files record.

        Returns ``(db_id, content_changed, existing_summary, prev_page_count)``
        where *content_changed* is ``True`` when the file's
        ``source_modified_at`` differs from the value already stored in the
        DB (i.e. the source has been modified since the last sync).
        *prev_page_count* is the previously-stored page count (0 for new files).
        """
        # Fetch the existing row (if any) BEFORE upserting so we can detect
        # whether source_modified_at actually changed.
        existing = await db.execute(
            text(
                "SELECT id, source_modified_at, summary, page_count FROM catalog_files WHERE catalog_id = :cid AND source_file_id = :sfid"
            ),
            {"cid": catalog_id, "sfid": file.id},
        )
        existing_row = existing.mappings().first()

        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                INSERT INTO catalog_files (id, catalog_id, source_file_id, source_file_name, mime_type,
                    folder_path, page_count, metadata, source_modified_at, sync_status, synced_at, created_at, updated_at)
                VALUES (:id, :catalog_id, :source_file_id, :name, :mime_type,
                    :folder_path, :page_count, CAST(:metadata AS jsonb), :source_modified_at, 'syncing', :now, :now, :now)
                ON CONFLICT (catalog_id, source_file_id)
                DO UPDATE SET
                    source_file_name = EXCLUDED.source_file_name,
                    mime_type = EXCLUDED.mime_type,
                    folder_path = EXCLUDED.folder_path,
                    page_count = catalog_files.page_count,
                    metadata = EXCLUDED.metadata,
                    source_modified_at = EXCLUDED.source_modified_at,
                    sync_status = 'syncing',
                    synced_at = EXCLUDED.synced_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
            """),
            {
                "id": str(uuid4()),
                "catalog_id": catalog_id,
                "source_file_id": file.id,
                "name": file.name,
                "mime_type": file.mime_type,
                "folder_path": file.folder_path,
                "page_count": page_count,
                "metadata": __import__("json").dumps(file.metadata),
                "source_modified_at": file.modified_at,
                "now": now,
            },
        )
        row = result.mappings().first()
        db_id = str(row["id"])

        if existing_row is None:
            # Brand new file — always process
            return db_id, True, None, 0

        existing_summary: str | None = existing_row["summary"]
        prev_page_count: int = existing_row["page_count"] or 0
        prev_modified = existing_row["source_modified_at"]
        # Compare timestamps: if source reports the same modified_at, file is unchanged
        content_changed = prev_modified is None or prev_modified != file.modified_at
        return db_id, content_changed, existing_summary, prev_page_count

    async def _upsert_catalog_page(
        self,
        db: AsyncSession,
        catalog_id: str,
        file_id: str,
        page: ExtractedPage,
        content_hash: str,
        thumbnail_s3_key: str | None,
    ) -> str:
        """Upsert a catalog_pages record, returning the DB ID."""

        now = datetime.now(timezone.utc)
        result = await db.execute(
            text("""
                INSERT INTO catalog_pages (id, catalog_id, file_id, page_number, title, text_content,
                    speaker_notes, content_hash, thumbnail_s3_key, source_ref, metadata,
                    indexed_at, created_at, updated_at)
                VALUES (:id, :catalog_id, :file_id, :page_number, :title, :text_content,
                    :speaker_notes, :content_hash, :thumbnail_s3_key, CAST(:source_ref AS jsonb), CAST(:metadata AS jsonb),
                    NULL, :now, :now)
                ON CONFLICT (file_id, page_number)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    text_content = EXCLUDED.text_content,
                    speaker_notes = EXCLUDED.speaker_notes,
                    content_hash = EXCLUDED.content_hash,
                    thumbnail_s3_key = COALESCE(EXCLUDED.thumbnail_s3_key, catalog_pages.thumbnail_s3_key),
                    source_ref = EXCLUDED.source_ref,
                    metadata = EXCLUDED.metadata,
                    indexed_at = NULL,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
            """),
            {
                "id": str(uuid4()),
                "catalog_id": catalog_id,
                "file_id": file_id,
                "page_number": page.page_number,
                "title": page.title,
                "text_content": page.text_content,
                "speaker_notes": page.speaker_notes,
                "content_hash": content_hash,
                "thumbnail_s3_key": thumbnail_s3_key,
                "source_ref": json.dumps(page.source_ref),
                "metadata": json.dumps(page.metadata),
                "now": now,
            },
        )
        row = result.mappings().first()
        return str(row["id"])

    async def _batch_upsert_catalog_pages(
        self,
        db: AsyncSession,
        catalog_id: str,
        file_id: str,
        rows: list[tuple["ExtractedPage", str, str | None]],
    ) -> None:
        """Upsert multiple catalog_pages in a single multi-row INSERT.

        Falls back to individual inserts if the batch fails (e.g. a single row
        has bad data), so one corrupt page cannot lose an entire file.
        """
        if not rows:
            return

        now = datetime.now(timezone.utc)
        params: list[dict] = []
        for page, content_hash, thumbnail_s3_key in rows:
            params.append(
                {
                    "id": str(uuid4()),
                    "catalog_id": catalog_id,
                    "file_id": file_id,
                    "page_number": page.page_number,
                    "title": page.title,
                    "text_content": page.text_content,
                    "speaker_notes": page.speaker_notes,
                    "content_hash": content_hash,
                    "thumbnail_s3_key": thumbnail_s3_key,
                    "source_ref": json.dumps(page.source_ref),
                    "metadata": json.dumps(page.metadata),
                    "now": now,
                }
            )

        # Build a single multi-row VALUES clause with numbered placeholders
        value_clauses = []
        flat_params: dict = {}
        for i, p in enumerate(params):
            suffix = f"_{i}"
            value_clauses.append(
                f"(:id{suffix}, :catalog_id{suffix}, :file_id{suffix}, :page_number{suffix}, "
                f":title{suffix}, :text_content{suffix}, :speaker_notes{suffix}, :content_hash{suffix}, "
                f":thumbnail_s3_key{suffix}, CAST(:source_ref{suffix} AS jsonb), "
                f"CAST(:metadata{suffix} AS jsonb), NULL, :now{suffix}, :now{suffix})"
            )
            for key, val in p.items():
                flat_params[f"{key}{suffix}"] = val

        sql = text(f"""
            INSERT INTO catalog_pages (id, catalog_id, file_id, page_number, title, text_content,
                speaker_notes, content_hash, thumbnail_s3_key, source_ref, metadata,
                indexed_at, created_at, updated_at)
            VALUES {", ".join(value_clauses)}
            ON CONFLICT (file_id, page_number)
            DO UPDATE SET
                title = EXCLUDED.title,
                text_content = EXCLUDED.text_content,
                speaker_notes = EXCLUDED.speaker_notes,
                content_hash = EXCLUDED.content_hash,
                thumbnail_s3_key = COALESCE(EXCLUDED.thumbnail_s3_key, catalog_pages.thumbnail_s3_key),
                source_ref = EXCLUDED.source_ref,
                metadata = EXCLUDED.metadata,
                indexed_at = NULL,
                updated_at = EXCLUDED.updated_at
        """)

        try:
            await db.execute(sql, flat_params)
        except Exception:
            logger.warning(
                "Batch upsert failed for file %s (%d pages), falling back to individual inserts",
                file_id,
                len(rows),
                exc_info=True,
            )
            # Rollback the failed statement, then insert one by one
            await db.rollback()
            for page, content_hash, thumbnail_s3_key in rows:
                try:
                    await self._upsert_catalog_page(
                        db,
                        catalog_id,
                        file_id,
                        page,
                        content_hash,
                        thumbnail_s3_key,
                    )
                except Exception:
                    logger.error(
                        "Failed to upsert page %d of file %s",
                        page.page_number,
                        file_id,
                        exc_info=True,
                    )

    async def _get_all_page_hashes(self, db: AsyncSession, file_id: str) -> dict[int, tuple[str, bool]]:
        """Batch-fetch content hashes and thumbnail status for a file's pages.

        Returns:
            Dict mapping page_number → (content_hash, has_thumbnail).
        """
        result = await db.execute(
            text("SELECT page_number, content_hash, thumbnail_s3_key FROM catalog_pages WHERE file_id = :file_id"),
            {"file_id": file_id},
        )
        return {
            row["page_number"]: (row["content_hash"], row["thumbnail_s3_key"] is not None) for row in result.mappings()
        }

    async def _remove_extra_pages(self, db: AsyncSession, file_id: str, max_page: int) -> None:
        """Remove pages beyond the current page count (slides might have been deleted)."""
        await db.execute(
            text("DELETE FROM catalog_pages WHERE file_id = :file_id AND page_number > :max_page"),
            {"file_id": file_id, "max_page": max_page},
        )

    async def _remove_deleted_files(self, db: AsyncSession, catalog_id: str, current_source_ids: set[str]) -> None:
        """Remove files that no longer exist in the source, including their vectors."""
        result = await db.execute(
            text("SELECT id, source_file_id FROM catalog_files WHERE catalog_id = :catalog_id"),
            {"catalog_id": catalog_id},
        )
        vector_ids_to_delete: list[str] = []
        for row in result.mappings():
            if row["source_file_id"] not in current_source_ids:
                # Collect vector IDs before deleting DB rows
                pages_result = await db.execute(
                    text("SELECT page_number FROM catalog_pages WHERE file_id = :file_id"),
                    {"file_id": row["id"]},
                )
                for page_row in pages_result.mappings():
                    vector_ids_to_delete.append(f"{row['source_file_id']}#page_{page_row['page_number']}")
                # CASCADE handles pages
                await db.execute(
                    text("DELETE FROM catalog_files WHERE id = :id"),
                    {"id": row["id"]},
                )

        # Remove vectors for deleted files
        if vector_ids_to_delete:
            try:
                vector_store = self._get_vector_store(catalog_id)
                await vector_store.adelete(ids=vector_ids_to_delete)
                logger.info("Deleted %d vectors for removed files in catalog %s", len(vector_ids_to_delete), catalog_id)
            except Exception:
                logger.warning("Failed to delete vectors for removed files in catalog %s", catalog_id, exc_info=True)

    async def _delete_file_by_source_id(self, db: AsyncSession, catalog_id: str, source_file_id: str) -> None:
        """Delete a specific file by its source ID, including its vectors."""
        # Collect page numbers for vector ID construction
        file_result = await db.execute(
            text("SELECT id FROM catalog_files WHERE catalog_id = :catalog_id AND source_file_id = :source_file_id"),
            {"catalog_id": catalog_id, "source_file_id": source_file_id},
        )
        file_row = file_result.mappings().first()
        vector_ids: list[str] = []
        if file_row:
            pages_result = await db.execute(
                text("SELECT page_number FROM catalog_pages WHERE file_id = :file_id"),
                {"file_id": file_row["id"]},
            )
            vector_ids = [f"{source_file_id}#page_{r['page_number']}" for r in pages_result.mappings()]

        await db.execute(
            text("DELETE FROM catalog_files WHERE catalog_id = :catalog_id AND source_file_id = :source_file_id"),
            {"catalog_id": catalog_id, "source_file_id": source_file_id},
        )

        if vector_ids:
            try:
                vector_store = self._get_vector_store(catalog_id)
                await vector_store.adelete(ids=vector_ids)
                logger.info("Deleted %d vectors for file %s in catalog %s", len(vector_ids), source_file_id, catalog_id)
            except Exception:
                logger.warning(
                    "Failed to delete vectors for file %s in catalog %s", source_file_id, catalog_id, exc_info=True
                )

    async def _upload_thumbnail(
        self,
        catalog_id: str,
        file_id: str,
        page_number: int,
        image_bytes: bytes,
    ) -> str:
        """Upload a thumbnail to S3 and return the S3 key."""
        bucket = config.catalog.thumbnails_s3_bucket
        key = f"{catalog_id}/{file_id}/page_{page_number}.png"

        async with self._s3_session.create_client("s3", region_name=_AWS_REGION) as s3:
            await s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=image_bytes,
                ContentType="image/png",
            )

        return key

    async def _update_sync_job(self, db: AsyncSession, job_id: str, **fields: Any) -> None:
        """Update a sync job record with the given fields."""
        set_clauses = []
        params: dict[str, Any] = {"job_id": job_id}
        for key, value in fields.items():
            if key == "error_details":
                set_clauses.append(f"{key} = CAST(:{key} AS jsonb)")
                params[key] = json.dumps(value)
            else:
                set_clauses.append(f"{key} = :{key}")
                params[key] = value

        if set_clauses:
            sql = f"UPDATE catalog_sync_jobs SET {', '.join(set_clauses)} WHERE id = :job_id"
            await db.execute(text(sql), params)
            await db.commit()

        # Push update via Socket.IO if callback is set
        await self._emit_sync_progress(job_id, **fields)

    async def _emit_sync_progress(self, job_id: str, **fields: Any) -> None:
        """Push progress via Socket.IO without writing to the database."""
        state = self._job_state.get(job_id)
        callback = state.progress_callback if state else None
        if callback:
            try:
                await callback(job_id, fields)
            except Exception:
                logger.debug("Failed to emit sync progress for job %s", job_id)

    async def _update_catalog_synced(self, db: AsyncSession, catalog_id: str) -> None:
        """Update catalog's last_synced_at timestamp."""
        now = datetime.now(timezone.utc)
        await db.execute(
            text("UPDATE catalogs SET last_synced_at = :now, status = 'active', updated_at = :now WHERE id = :id"),
            {"now": now, "id": catalog_id},
        )

    # --- Summarization & Contextualization ---

    async def _generate_document_summary(
        self, file: SourceFile, pages: list[ExtractedPage], sync_job_id: str | None = None
    ) -> str:
        """Generate a 2-3 sentence document summary using Claude Haiku (Pass 1).

        Concatenates page texts (capped at ~8K chars) and asks Haiku to summarize.
        """
        # Resolve per-job cost attribution
        state = self._job_state.get(sync_job_id) if sync_job_id else None
        job_user_sub = state.user_sub if state else None
        job_catalog_id = state.catalog_id if state else None

        # Concatenate page texts, capping at ~8K chars to stay within Haiku's sweet spot
        combined = ""
        for page in pages:
            chunk = f"--- Page {page.page_number}: {page.title} ---\n{page.text_content}\n"
            if len(combined) + len(chunk) > 8000:
                break
            combined += chunk

        if not combined.strip():
            return f"Document: {file.name}"

        prompt = (
            "Summarize this document in 2-3 sentences. Include: document type, "
            "subject matter, time period if mentioned, and key topics covered.\n\n"
            f"Document name: {file.name}\n\n{combined}"
        )

        model_id = config.catalog.summarization_model_id

        def _invoke() -> str:
            response = self._bedrock_client.invoke_model(
                modelId=model_id,
                body=json.dumps(
                    {
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 200,
                        "messages": [{"role": "user", "content": prompt}],
                    }
                ),
            )
            result = json.loads(response["body"].read())

            # Log Bedrock summarization cost
            if self._cost_logger and job_user_sub:
                usage = result.get("usage", {})
                billing: dict[str, int] = {}
                if usage.get("input_tokens"):
                    billing["base_input_tokens"] = usage["input_tokens"]
                if usage.get("output_tokens"):
                    billing["base_output_tokens"] = usage["output_tokens"]
                if billing:
                    self._cost_logger.log_cost_async(
                        user_sub=job_user_sub,
                        billing_unit_breakdown=billing,
                        provider="bedrock_converse",
                        model_name=model_id,
                        catalog_id=job_catalog_id,
                    )

            return result["content"][0]["text"].strip()

        try:
            return await run_in_sync_executor(_invoke)
        except Exception:
            logger.warning("Failed to generate summary for %s, using fallback", file.name)
            return f"Document: {file.name}"

    @staticmethod
    def _build_contextualized_content(
        file: SourceFile,
        summary: str,
        page: ExtractedPage,
        total_pages: int,
    ) -> str:
        """Build contextualized content for a page (Pass 2).

        Prepends document summary + file metadata to each page's text.
        This becomes the content used for embedding.
        """
        parts = [
            f'Document: "{file.name}" ({file.folder_path})',
            f"Summary: {summary}",
            "",
            f'Page {page.page_number} of {total_pages}: "{page.title}"',
            "",
            page.text_content,
        ]
        if page.speaker_notes:
            parts.append(f"\nSpeaker notes: {page.speaker_notes}")

        return "\n".join(parts)

    async def _update_file_summary(self, db: AsyncSession, file_id: str, summary: str) -> None:
        """Update the summary field on a catalog_files record."""
        await db.execute(
            text("UPDATE catalog_files SET summary = :summary, updated_at = :now WHERE id = :id"),
            {"summary": summary, "now": datetime.now(timezone.utc), "id": file_id},
        )
