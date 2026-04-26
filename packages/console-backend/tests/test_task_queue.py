"""Tests for SyncTaskQueue — in-memory task queue abstraction."""

import asyncio
from datetime import datetime, timezone

import pytest

from playground_backend.catalog.task_queue import (
    BatchResult,
    FileTaskPayload,
    FileTaskResult,
    InMemoryFileTaskProcessor,
    InMemoryTaskQueue,
    SyncTaskMessage,
)


class TestSyncTaskMessage:
    def test_str_representation(self):
        msg = SyncTaskMessage(catalog_id="cat-1", sync_job_id="job-1", triggered_by="manual")
        assert "cat-1" in str(msg)
        assert "job-1" in str(msg)
        assert "manual" in str(msg)

    def test_frozen(self):
        msg = SyncTaskMessage(catalog_id="cat-1", sync_job_id="job-1", triggered_by="scheduled")
        with pytest.raises(AttributeError):
            msg.catalog_id = "changed"  # type: ignore[misc]


class TestInMemoryTaskQueue:
    @pytest.mark.asyncio
    async def test_enqueue_and_process(self):
        """Tasks are processed by the handler."""
        processed: list[SyncTaskMessage] = []

        async def handler(msg: SyncTaskMessage) -> None:
            processed.append(msg)

        queue = InMemoryTaskQueue(max_workers=1)
        await queue.start(handler)

        msg = SyncTaskMessage(catalog_id="cat-1", sync_job_id="job-1", triggered_by="manual")
        await queue.enqueue(msg)

        # Give the worker time to pick up the task
        await asyncio.sleep(0.1)

        assert len(processed) == 1
        assert processed[0].catalog_id == "cat-1"

        await queue.stop()

    @pytest.mark.asyncio
    async def test_deduplication(self):
        """Same catalog_id is not enqueued twice."""
        processed: list[SyncTaskMessage] = []
        gate = asyncio.Event()

        async def slow_handler(msg: SyncTaskMessage) -> None:
            await gate.wait()
            processed.append(msg)

        queue = InMemoryTaskQueue(max_workers=1)
        await queue.start(slow_handler)

        msg1 = SyncTaskMessage(catalog_id="cat-1", sync_job_id="job-1", triggered_by="manual")
        msg2 = SyncTaskMessage(catalog_id="cat-1", sync_job_id="job-2", triggered_by="scheduled")

        await queue.enqueue(msg1)
        await asyncio.sleep(0.05)  # Let worker pick up msg1
        await queue.enqueue(msg2)  # Should be skipped (cat-1 in-flight)

        gate.set()
        await asyncio.sleep(0.1)

        assert len(processed) == 1
        assert processed[0].sync_job_id == "job-1"

        await queue.stop()

    @pytest.mark.asyncio
    async def test_active_catalog_ids(self):
        """active_catalog_ids reflects queued and in-flight tasks."""
        gate = asyncio.Event()

        async def blocking_handler(msg: SyncTaskMessage) -> None:
            await gate.wait()

        queue = InMemoryTaskQueue(max_workers=1)
        await queue.start(blocking_handler)

        assert queue.active_catalog_ids() == set()

        msg = SyncTaskMessage(catalog_id="cat-1", sync_job_id="job-1", triggered_by="manual")
        await queue.enqueue(msg)
        await asyncio.sleep(0.05)

        assert "cat-1" in queue.active_catalog_ids()

        gate.set()
        await asyncio.sleep(0.1)

        assert queue.active_catalog_ids() == set()
        await queue.stop()

    @pytest.mark.asyncio
    async def test_concurrent_workers(self):
        """Multiple workers process tasks concurrently."""
        processing = asyncio.Event()
        processed: list[str] = []

        async def handler(msg: SyncTaskMessage) -> None:
            processing.set()
            await asyncio.sleep(0.05)
            processed.append(msg.catalog_id)

        queue = InMemoryTaskQueue(max_workers=3)
        await queue.start(handler)

        for i in range(3):
            await queue.enqueue(
                SyncTaskMessage(
                    catalog_id=f"cat-{i}",
                    sync_job_id=f"job-{i}",
                    triggered_by="scheduled",
                )
            )

        await asyncio.sleep(0.2)
        assert len(processed) == 3
        await queue.stop()

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_kill_worker(self):
        """Worker continues processing after handler raises."""
        call_count = 0

        async def flaky_handler(msg: SyncTaskMessage) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Boom!")

        queue = InMemoryTaskQueue(max_workers=1)
        await queue.start(flaky_handler)

        await queue.enqueue(SyncTaskMessage(catalog_id="cat-1", sync_job_id="j1", triggered_by="manual"))
        await asyncio.sleep(0.1)

        await queue.enqueue(SyncTaskMessage(catalog_id="cat-2", sync_job_id="j2", triggered_by="manual"))
        await asyncio.sleep(0.1)

        assert call_count == 2
        await queue.stop()

    @pytest.mark.asyncio
    async def test_stop_drains_workers(self):
        """stop() waits for in-flight work to complete."""
        completed = asyncio.Event()

        async def handler(msg: SyncTaskMessage) -> None:
            await asyncio.sleep(0.05)
            completed.set()

        queue = InMemoryTaskQueue(max_workers=1)
        await queue.start(handler)
        await queue.enqueue(SyncTaskMessage(catalog_id="cat-1", sync_job_id="j1", triggered_by="manual"))

        await queue.stop()
        assert completed.is_set()

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self):
        """Calling start() twice doesn't create extra workers."""

        async def handler(msg: SyncTaskMessage) -> None:
            pass

        queue = InMemoryTaskQueue(max_workers=2)
        await queue.start(handler)
        assert len(queue._workers) == 2

        await queue.start(handler)
        assert len(queue._workers) == 2

        await queue.stop()


# =====================================================================
# File-level processor tests
# =====================================================================


def _make_payload(
    file_id: str = "f-1",
    file_name: str = "slide.pptx",
    catalog_id: str = "cat-1",
    sync_job_id: str = "job-1",
) -> FileTaskPayload:
    return FileTaskPayload(
        catalog_id=catalog_id,
        sync_job_id=sync_job_id,
        source_file_id=file_id,
        source_file_name=file_name,
        source_file_mime_type="application/vnd.google-apps.presentation",
        source_file_modified_at=datetime.now(timezone.utc).isoformat(),
    )


class TestFileTaskPayload:
    def test_str(self):
        p = _make_payload()
        assert "slide.pptx" in str(p)
        assert "cat-1" in str(p)

    def test_frozen(self):
        p = _make_payload()
        with pytest.raises(AttributeError):
            p.catalog_id = "changed"  # type: ignore[misc]

    def test_round_trip_source_file(self):
        from playground_backend.catalog.adapters.base import SourceFile

        now = datetime.now(timezone.utc)
        sf = SourceFile(
            id="abc",
            name="deck.pptx",
            mime_type="application/pdf",
            modified_at=now,
            folder_path="Sales/Q1",
            metadata={"extra": "data"},
        )
        payload = FileTaskPayload.from_source_file("cat-1", "job-1", sf)
        restored = payload.to_source_file()

        assert restored.id == sf.id
        assert restored.name == sf.name
        assert restored.mime_type == sf.mime_type
        assert restored.modified_at == now
        assert restored.folder_path == sf.folder_path
        assert restored.metadata == sf.metadata


class TestInMemoryFileTaskProcessor:
    @pytest.mark.asyncio
    async def test_empty_batch(self):
        proc = InMemoryFileTaskProcessor(max_concurrency=2)
        result = await proc.process_batch([], handler=self._noop_handler)
        assert result == BatchResult(processed=0, failed=0, errors=[])

    @pytest.mark.asyncio
    async def test_all_success(self):
        proc = InMemoryFileTaskProcessor(max_concurrency=2)
        payloads = [_make_payload(file_id=f"f-{i}") for i in range(5)]

        result = await proc.process_batch(payloads, handler=self._success_handler)
        assert result.processed == 5
        assert result.failed == 0
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_handler_failure_captured(self):
        """Exceptions from handler are turned into FileTaskResult(success=False)."""
        proc = InMemoryFileTaskProcessor(max_concurrency=2)
        payloads = [_make_payload(file_id="f-1"), _make_payload(file_id="f-2")]

        async def failing_handler(p: FileTaskPayload) -> FileTaskResult:
            if p.source_file_id == "f-1":
                raise RuntimeError("Boom!")
            return FileTaskResult(file_id=p.source_file_id, file_name=p.source_file_name, success=True)

        result = await proc.process_batch(payloads, handler=failing_handler)
        assert result.processed == 1
        assert result.failed == 1
        assert len(result.errors) == 1
        assert "Boom!" in result.errors[0]["error"]

    @pytest.mark.asyncio
    async def test_progress_callback(self):
        proc = InMemoryFileTaskProcessor(max_concurrency=1)
        payloads = [_make_payload(file_id=f"f-{i}") for i in range(3)]
        progress_calls: list[tuple[int, int]] = []

        async def on_progress(processed: int, failed: int) -> None:
            progress_calls.append((processed, failed))

        await proc.process_batch(payloads, handler=self._success_handler, on_progress=on_progress)
        # One callback per file
        assert len(progress_calls) == 3
        assert progress_calls[-1] == (3, 0)

    @pytest.mark.asyncio
    async def test_cancellation(self):
        """check_cancelled raising aborts the batch and propagates."""
        proc = InMemoryFileTaskProcessor(max_concurrency=1)
        payloads = [_make_payload(file_id=f"f-{i}") for i in range(5)]
        call_count = 0

        class Cancelled(Exception):
            pass

        async def cancel_after_2() -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise Cancelled("stopped")

        with pytest.raises(Cancelled):
            await proc.process_batch(
                payloads,
                handler=self._success_handler,
                check_cancelled=cancel_after_2,
            )

    @pytest.mark.asyncio
    async def test_concurrency_respected(self):
        """At most max_concurrency files run in parallel."""
        proc = InMemoryFileTaskProcessor(max_concurrency=2)
        payloads = [_make_payload(file_id=f"f-{i}") for i in range(6)]
        peak = 0
        current = 0
        lock = asyncio.Lock()

        async def tracking_handler(p: FileTaskPayload) -> FileTaskResult:
            nonlocal peak, current
            async with lock:
                current += 1
                if current > peak:
                    peak = current
            await asyncio.sleep(0.02)
            async with lock:
                current -= 1
            return FileTaskResult(file_id=p.source_file_id, file_name=p.source_file_name, success=True)

        await proc.process_batch(payloads, handler=tracking_handler)
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self):
        proc = InMemoryFileTaskProcessor(max_concurrency=3)
        payloads = [_make_payload(file_id=f"f-{i}") for i in range(4)]

        async def mixed_handler(p: FileTaskPayload) -> FileTaskResult:
            if p.source_file_id in ("f-1", "f-3"):
                raise ValueError(f"fail-{p.source_file_id}")
            return FileTaskResult(file_id=p.source_file_id, file_name=p.source_file_name, success=True)

        result = await proc.process_batch(payloads, handler=mixed_handler)
        assert result.processed == 2
        assert result.failed == 2
        assert len(result.errors) == 2

    # -- helpers --

    @staticmethod
    async def _noop_handler(_p: FileTaskPayload) -> FileTaskResult:
        return FileTaskResult(file_id="x", file_name="x", success=True)

    @staticmethod
    async def _success_handler(p: FileTaskPayload) -> FileTaskResult:
        return FileTaskResult(
            file_id=p.source_file_id,
            file_name=p.source_file_name,
            success=True,
        )
