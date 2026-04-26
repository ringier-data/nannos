"""Dedicated thread pool for catalog sync operations.

Isolates blocking I/O (Google Drive API, PDF rendering, Bedrock, Gemini embeddings)
from the default asyncio executor used by the FastAPI application, preventing
sync workloads from starving HTTP request handling.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Dedicated pool for catalog sync — sized to match FILE_CONCURRENCY (5)
# plus headroom for PDF rendering and embedding calls.
_sync_executor: ThreadPoolExecutor | None = None

_MAX_WORKERS = 20


def get_sync_executor() -> ThreadPoolExecutor:
    """Return the dedicated sync thread pool, creating it lazily."""
    global _sync_executor
    if _sync_executor is None:
        _sync_executor = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS,
            thread_name_prefix="catalog-sync",
        )
        logger.info("Created catalog sync thread pool (max_workers=%d)", _MAX_WORKERS)
    return _sync_executor


def shutdown_sync_executor() -> None:
    """Shut down the sync thread pool. Safe to call multiple times."""
    global _sync_executor
    if _sync_executor is not None:
        _sync_executor.shutdown(wait=False)
        logger.info("Catalog sync thread pool shut down")
        _sync_executor = None


async def run_in_sync_executor(func, *args):
    """Run a blocking function in the dedicated sync thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_sync_executor(), func, *args)
