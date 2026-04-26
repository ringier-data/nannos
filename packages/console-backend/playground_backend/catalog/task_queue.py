"""Pluggable task queue abstractions for catalog sync.

Two levels of abstraction:

**Job-level** – ``SyncTaskQueue`` / ``InMemoryTaskQueue``
    Decouples *scheduling* sync work from *executing* it.
    The default in-memory implementation runs workers inside the same
    process using :mod:`asyncio`.  An SQS-backed replacement only needs
    to implement the same three-method protocol.

**File-level** – ``FileTaskProcessor`` / ``InMemoryFileTaskProcessor``
    Fan-out/fan-in processor for individual files *within* a single sync
    job.  The in-memory implementation uses ``asyncio.Semaphore`` +
    ``asyncio.as_completed``.  A distributed replacement (SQS + DynamoDB
    atomic counter, Celery chord, …) only needs to satisfy the same ABC.

Usage::

    # Job queue
    queue = InMemoryTaskQueue(max_workers=3)
    await queue.start(handler=my_async_handler)
    await queue.enqueue(SyncTaskMessage(catalog_id="abc", sync_job_id="123"))
    await queue.stop()

    # File processor (inside a job handler)
    processor = InMemoryFileTaskProcessor(max_concurrency=3)
    payloads = [FileTaskPayload.from_source_file("cat-1", "job-1", f) for f in files]
    result = await processor.process_batch(payloads, handler=process_one_file)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncTaskMessage:
    """Immutable description of a catalog sync job to execute."""

    catalog_id: str
    sync_job_id: str
    triggered_by: str  # "scheduled" | "manual"
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    user_sub: str | None = None  # user who triggered the sync (for cost attribution)

    def __str__(self) -> str:
        return f"SyncTask(catalog={self.catalog_id}, job={self.sync_job_id}, by={self.triggered_by})"


# Type alias for the handler that processes each task.
SyncTaskHandler = Callable[[SyncTaskMessage], Awaitable[None]]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class SyncTaskQueue(ABC):
    """Abstract task queue for catalog sync work.

    Implementations must be safe to call from any ``asyncio`` context.
    ``start()`` must be called before ``enqueue()``; ``stop()`` drains
    remaining work and shuts down gracefully.
    """

    @abstractmethod
    async def enqueue(self, message: SyncTaskMessage) -> None:
        """Submit a sync task for asynchronous execution.

        Must not block.  Implementations may silently drop duplicates
        (e.g. same ``catalog_id`` already queued).
        """

    @abstractmethod
    async def start(self, handler: SyncTaskHandler) -> None:
        """Start consuming tasks.  ``handler`` is called once per task."""

    @abstractmethod
    async def stop(self) -> None:
        """Drain in-flight work and release resources."""

    @abstractmethod
    def active_catalog_ids(self) -> set[str]:
        """Return catalog IDs that are currently being processed or queued.

        Used by the scheduled engine to avoid enqueuing duplicates.
        """


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------

_SENTINEL = object()


class InMemoryTaskQueue(SyncTaskQueue):
    """Process-local task queue backed by :class:`asyncio.Queue`.

    Suitable for single-instance deployments or local development.
    For horizontal scaling, swap with an SQS-backed implementation.

    Parameters
    ----------
    max_workers:
        Maximum number of tasks processed concurrently.
    """

    def __init__(self, max_workers: int = 3) -> None:
        self._max_workers = max_workers
        self._queue: asyncio.Queue[SyncTaskMessage | object] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._handler: SyncTaskHandler | None = None
        # Track which catalog IDs are queued or in-flight
        self._queued: set[str] = set()
        self._in_flight: set[str] = set()

    # -- public API --

    async def enqueue(self, message: SyncTaskMessage) -> None:
        if message.catalog_id in self._queued | self._in_flight:
            logger.debug(
                "Skipping duplicate enqueue for catalog %s (already queued/in-flight)",
                message.catalog_id,
            )
            return
        self._queued.add(message.catalog_id)
        await self._queue.put(message)
        logger.info("Enqueued %s (queue_depth=%d)", message, self._queue.qsize())

    async def start(self, handler: SyncTaskHandler) -> None:
        if self._workers:
            return  # already running
        self._handler = handler
        for i in range(self._max_workers):
            task = asyncio.create_task(self._worker_loop(i), name=f"sync-worker-{i}")
            self._workers.append(task)
        logger.info("InMemoryTaskQueue started with %d workers", self._max_workers)

    async def stop(self) -> None:
        # Send sentinel for each worker so they exit cleanly
        for _ in self._workers:
            await self._queue.put(_SENTINEL)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._queued.clear()
        self._in_flight.clear()
        logger.info("InMemoryTaskQueue stopped")

    def active_catalog_ids(self) -> set[str]:
        return self._queued | self._in_flight

    def diagnostics(self) -> dict[str, Any]:
        """Return a snapshot of queue state for debugging."""
        return {
            "queue_depth": self._queue.qsize(),
            "queued_catalog_ids": sorted(self._queued),
            "in_flight_catalog_ids": sorted(self._in_flight),
            "workers_alive": sum(1 for w in self._workers if not w.done()),
            "workers_total": len(self._workers),
        }

    # -- internals --

    async def _worker_loop(self, worker_id: int) -> None:
        assert self._handler is not None
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                break

            message: SyncTaskMessage = item  # type: ignore[assignment]
            self._queued.discard(message.catalog_id)
            self._in_flight.add(message.catalog_id)

            try:
                logger.info("[worker-%d] Processing %s", worker_id, message)
                await self._handler(message)
            except Exception:
                logger.exception(
                    "[worker-%d] Unhandled error processing %s",
                    worker_id,
                    message,
                )
            finally:
                self._in_flight.discard(message.catalog_id)
                self._queue.task_done()


# =====================================================================
# File-level fan-out / fan-in
# =====================================================================

# ---------------------------------------------------------------------------
# Payloads & results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileTaskPayload:
    """Serializable description of a single file to process within a sync job.

    All fields are JSON-safe primitives so implementations backed by SQS or
    another message broker can serialise the payload directly.  Use
    :meth:`from_source_file` / :meth:`to_source_file` for convenient
    conversion to/from the in-process ``SourceFile`` dataclass.
    """

    catalog_id: str
    sync_job_id: str
    source_file_id: str
    source_file_name: str
    source_file_mime_type: str
    source_file_modified_at: str  # ISO 8601
    source_file_folder_path: str = ""
    source_file_metadata: dict[str, Any] = field(default_factory=dict)

    # -- convenience converters --

    @classmethod
    def from_source_file(
        cls,
        catalog_id: str,
        sync_job_id: str,
        file: Any,  # SourceFile (avoids hard import)
    ) -> FileTaskPayload:
        """Build a payload from a :class:`SourceFile` instance."""
        return cls(
            catalog_id=catalog_id,
            sync_job_id=sync_job_id,
            source_file_id=file.id,
            source_file_name=file.name,
            source_file_mime_type=file.mime_type,
            source_file_modified_at=file.modified_at.isoformat(),
            source_file_folder_path=file.folder_path,
            source_file_metadata=dict(file.metadata) if file.metadata else {},
        )

    def to_source_file(self) -> Any:  # -> SourceFile
        """Reconstruct a :class:`SourceFile` from this payload."""
        from .adapters.base import SourceFile

        return SourceFile(
            id=self.source_file_id,
            name=self.source_file_name,
            mime_type=self.source_file_mime_type,
            modified_at=datetime.fromisoformat(self.source_file_modified_at),
            folder_path=self.source_file_folder_path,
            metadata=dict(self.source_file_metadata),
        )

    def __str__(self) -> str:
        return f"FileTask(file={self.source_file_name!r}, catalog={self.catalog_id}, job={self.sync_job_id})"


@dataclass
class FileTaskResult:
    """Outcome of processing a single file."""

    file_id: str
    file_name: str
    success: bool
    error: str | None = None


@dataclass
class BatchResult:
    """Aggregated outcome of a fan-out batch."""

    processed: int
    failed: int
    errors: list[dict[str, str]]


# Type aliases
FileTaskHandler = Callable[[FileTaskPayload], Awaitable[FileTaskResult]]
FileProgressCallback = Callable[[int, int], Awaitable[None]]  # (processed, failed)
CancelChecker = Callable[[], Awaitable[None]]  # raises on cancellation


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class FileTaskProcessor(ABC):
    """Fan-out/fan-in processor for file-level tasks within a sync job.

    Implementations receive a list of file payloads and a handler, then
    process them with whatever parallelism strategy they support.

    *  ``InMemoryFileTaskProcessor`` — ``asyncio.Semaphore`` + ``as_completed``
    *  (future) SQS-backed — enqueue N messages, track via DynamoDB counter
    """

    @abstractmethod
    async def process_batch(
        self,
        tasks: list[FileTaskPayload],
        handler: FileTaskHandler,
        on_progress: FileProgressCallback | None = None,
        check_cancelled: CancelChecker | None = None,
    ) -> BatchResult:
        """Process *tasks* with fan-out parallelism.

        Parameters
        ----------
        tasks:
            File payloads to process.
        handler:
            Async callable invoked once per file.  Must return
            :class:`FileTaskResult`.  May raise — the processor wraps
            exceptions into ``FileTaskResult(success=False)``.
        on_progress:
            Called after each file completes with ``(processed, failed)``.
        check_cancelled:
            Called before each file starts.  Should raise (e.g.
            ``SyncCancelled``) to abort the batch.  The exception
            propagates to the caller after remaining tasks are cancelled.
        """


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryFileTaskProcessor(FileTaskProcessor):
    """Process-local file processor using ``asyncio`` concurrency.

    Parameters
    ----------
    max_concurrency:
        Maximum number of files processed in parallel.
    """

    def __init__(self, max_concurrency: int = 3) -> None:
        self._max_concurrency = max_concurrency

    async def process_batch(
        self,
        tasks: list[FileTaskPayload],
        handler: FileTaskHandler,
        on_progress: FileProgressCallback | None = None,
        check_cancelled: CancelChecker | None = None,
    ) -> BatchResult:
        if not tasks:
            return BatchResult(processed=0, failed=0, errors=[])

        semaphore = asyncio.Semaphore(self._max_concurrency)
        processed = 0
        failed = 0
        errors: list[dict[str, str]] = []

        async def _run_one(payload: FileTaskPayload) -> FileTaskResult:
            async with semaphore:
                if check_cancelled:
                    await check_cancelled()  # raises on cancellation
                try:
                    return await handler(payload)
                except Exception as exc:
                    logger.exception("File task failed: %s", payload)
                    return FileTaskResult(
                        file_id=payload.source_file_id,
                        file_name=payload.source_file_name,
                        success=False,
                        error=str(exc),
                    )

        running = [asyncio.create_task(_run_one(p)) for p in tasks]

        try:
            for coro in asyncio.as_completed(running):
                result: FileTaskResult = await coro
                if result.success:
                    processed += 1
                else:
                    failed += 1
                    if result.error:
                        errors.append({"file": result.file_name, "error": result.error})
                if on_progress:
                    await on_progress(processed, failed)
        except Exception:
            # Cancellation (or unexpected error) — cancel remaining tasks
            for t in running:
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise

        return BatchResult(processed=processed, failed=failed, errors=errors)
