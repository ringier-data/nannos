"""Dedicated thread pools for catalog sync operations.

Isolates blocking I/O (Google Drive API, PDF rendering, Gemini embeddings)
from the default asyncio executor used by the FastAPI application, preventing
sync workloads from starving HTTP request handling.

Two pools are provided:

- IO thread pool   (up to 20 workers): lightweight Drive API network calls,
  downloads, Bedrock/embedding calls.

- CPU thread pool  (1 worker):         poppler/PIL thumbnail render —
  serialised to bound cgroup peak memory so two render ops never overlap.

All LibreOffice (soffice) operations have been moved to the soffice-worker
pod, which runs in its own cgroup.  Catalog-worker no longer needs soffice.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_IO_MAX_WORKERS = 20  # Drive API / downloads — low memory, high concurrency OK
_CPU_MAX_WORKERS = 1  # poppler render — serialised to bound cgroup peak

_io_executor: ThreadPoolExecutor | None = None
_cpu_executor: ThreadPoolExecutor | None = None


def get_io_executor() -> ThreadPoolExecutor:
    """Return the dedicated I/O thread pool (Drive API, downloads), creating it lazily."""
    global _io_executor
    if _io_executor is None:
        _io_executor = ThreadPoolExecutor(
            max_workers=_IO_MAX_WORKERS,
            thread_name_prefix="catalog-io",
        )
        logger.info("Created catalog I/O thread pool (max_workers=%d)", _IO_MAX_WORKERS)
    return _io_executor


def get_cpu_executor() -> ThreadPoolExecutor:
    """Return the dedicated CPU thread pool (poppler render), creating it lazily."""
    global _cpu_executor
    if _cpu_executor is None:
        _cpu_executor = ThreadPoolExecutor(
            max_workers=_CPU_MAX_WORKERS,
            thread_name_prefix="catalog-cpu",
        )
        logger.info("Created catalog CPU thread pool (max_workers=%d)", _CPU_MAX_WORKERS)
    return _cpu_executor


def shutdown_executors() -> None:
    """Shut down all thread pools. Safe to call multiple times."""
    global _io_executor, _cpu_executor
    if _io_executor is not None:
        _io_executor.shutdown(wait=False)
        logger.info("Catalog I/O thread pool shut down")
        _io_executor = None
    if _cpu_executor is not None:
        _cpu_executor.shutdown(wait=False)
        logger.info("Catalog CPU thread pool shut down")
        _cpu_executor = None


async def run_in_sync_executor(func, *args):
    """Run a CPU/memory-intensive blocking function in the single-worker CPU pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_cpu_executor(), func, *args)
