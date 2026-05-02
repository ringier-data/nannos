"""Task abstractions for catalog sync.

**File-level** – ``FileTaskProcessor`` / ``InMemoryFileTaskProcessor``
    Fan-out/fan-in processor for individual files *within* a single sync
    job.  The in-memory implementation uses ``asyncio.as_completed`` for
    task orchestration; actual concurrency is bounded externally by the
    caller's slide-weighted budget.  A distributed replacement (SQS +
    DynamoDB atomic counter, Celery chord, …) only needs to satisfy the
    same ABC.

Usage::

    # File processor (inside a job handler)
    processor = InMemoryFileTaskProcessor()
    payloads = [FileTaskPayload.from_source_file("cat-1", "job-1", f) for f in files]
    result = await processor.process_batch(payloads, handler=process_one_file)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


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

    Concurrency is bounded externally by the caller (e.g. the
    slide-weighted budget in `CatalogSyncPipeline`).  This processor
    just orchestrates task fan-out, error collection, and progress.
    """

    async def process_batch(
        self,
        tasks: list[FileTaskPayload],
        handler: FileTaskHandler,
        on_progress: FileProgressCallback | None = None,
        check_cancelled: CancelChecker | None = None,
    ) -> BatchResult:
        if not tasks:
            return BatchResult(processed=0, failed=0, errors=[])

        processed = 0
        failed = 0
        errors: list[dict[str, str]] = []

        async def _run_one(payload: FileTaskPayload) -> FileTaskResult:
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
