"""Per-orchestrator sandbox pool for managing sandbox lifecycle.

Sandboxes are acquired per sub-agent A2A turn (not per session) with a
short warm TTL for multi-turn iteration. Provider-agnostic — works with
any async factory that returns a BaseSandbox (SandboxBackendProtocol).

Configuration:
    SANDBOX_PROVIDER: Provider name (e.g., "gatana"). If unset, sandbox is disabled.
    SANDBOX_POOL_CAPACITY: Max concurrent sandboxes
    SANDBOX_WARM_TTL: Seconds to keep idle sandboxes warm (default: 60)
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deepagents.backends.sandbox import BaseSandbox

logger = logging.getLogger(__name__)


@dataclass
class PooledSandbox:
    """A sandbox instance managed by the pool."""

    backend: BaseSandbox
    skills_hash: str | None = None
    last_released_at: float = 0.0
    in_use: bool = True


class SandboxPool:
    """Per-orchestrator sandbox pool. Provider-agnostic.

    - Sandboxes are keyed by (session_id, sub_agent_name) for warm reuse.
    - Idle sandboxes are reaped after warm_ttl seconds.
    - Acquire raises RuntimeError at capacity (user-facing error).
    """

    def __init__(
        self,
        create_fn: Callable[[], Awaitable[BaseSandbox]],
        capacity: int,
        warm_ttl: float = 300.0,
        *,
        home: str | None = None,
    ) -> None:
        """Initialize the sandbox pool.

        Args:
            create_fn: Async factory that provisions a new sandbox (BaseSandbox).
            capacity: Max concurrent sandboxes.
            warm_ttl: Seconds to keep idle sandboxes warm before reaping.
            home: Home directory of the sandbox user. Provider-specific
                (e.g. "/home/ubuntu" for Gatana). When set, skill files are
                uploaded to ``{home}/skills/`` and a symlink ``/skills →
                {home}/skills`` is created. When None, uploads go directly
                to ``/skills/``.
        """
        self._create_fn = create_fn
        self._capacity = capacity
        self._warm_ttl = warm_ttl
        self._home = home
        self._pool: dict[tuple[str, str], PooledSandbox] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None
        self._shutdown_done = False
        atexit.register(self._sync_close_all)

    @property
    def home(self) -> str | None:
        """Home directory of the sandbox user, or None if not set."""
        return self._home

    @property
    def capacity(self) -> int:
        """Current pool capacity."""
        return self._capacity

    @property
    def active_count(self) -> int:
        """Number of currently in-use sandboxes."""
        return len([e for e in self._pool.values() if e.in_use])

    async def acquire(self, session_id: str, sub_agent_name: str) -> PooledSandbox:
        """Acquire a sandbox for use. Reuses warm sandbox if available.

        Args:
            session_id: Current session/conversation ID
            sub_agent_name: Name of the sub-agent

        Returns:
            A PooledSandbox ready for use

        Raises:
            RuntimeError: If pool is at capacity
        """
        async with self._lock:
            key = (session_id, sub_agent_name)

            # Try to reuse a warm sandbox
            entry = self._pool.get(key)
            if entry and not entry.in_use:
                entry.in_use = True
                logger.info(
                    "Reusing warm sandbox for %s/%s",
                    session_id[:8],
                    sub_agent_name,
                )
                return entry

            # Evict expired idle sandboxes to make room
            await self._evict_idle_locked()

            # Check capacity
            in_use_count = len([e for e in self._pool.values() if e.in_use])
            if in_use_count >= self._capacity:
                raise RuntimeError(
                    f"Sandbox pool at capacity ({self._capacity}). "
                    "This agent's compute pool is busy; please retry shortly."
                )

            # Provision new sandbox
            backend = await self._create_fn()
            entry = PooledSandbox(backend=backend)
            self._pool[key] = entry
            logger.info(
                "Provisioned new sandbox for %s/%s (pool: %d/%d)",
                session_id[:8],
                sub_agent_name,
                in_use_count + 1,
                self._capacity,
            )
            return entry

    async def release(self, session_id: str, sub_agent_name: str) -> None:
        """Release a sandbox back to the pool for warm reuse.

        Args:
            session_id: Current session/conversation ID
            sub_agent_name: Name of the sub-agent
        """
        async with self._lock:
            entry = self._pool.get((session_id, sub_agent_name))
            if entry is None:
                return
            entry.in_use = False
            entry.last_released_at = time.monotonic()
            logger.debug(
                "Released sandbox for %s/%s",
                session_id[:8],
                sub_agent_name,
            )

    async def _evict_idle_locked(self) -> None:
        """Evict sandboxes that have been idle beyond warm_ttl. Must hold lock."""
        now = time.monotonic()
        expired_keys = [
            k
            for k, e in self._pool.items()
            if not e.in_use and e.last_released_at > 0 and now - e.last_released_at > self._warm_ttl
        ]
        for k in expired_keys:
            entry = self._pool.pop(k)
            try:
                close = getattr(entry.backend, "close", None)
                if close:
                    await asyncio.to_thread(close)
                logger.debug("Evicted idle sandbox for %s/%s", k[0][:8], k[1])
            except Exception as e:
                logger.warning("Error stopping evicted sandbox: %s", e)

    async def start_reaper(self) -> None:
        """Start the background reaper task that evicts idle sandboxes."""

        async def _reaper_loop() -> None:
            while True:
                await asyncio.sleep(self._warm_ttl)
                async with self._lock:
                    await self._evict_idle_locked()

        self._reaper_task = asyncio.create_task(_reaper_loop())
        logger.info("Sandbox pool reaper started (ttl=%.0fs)", self._warm_ttl)

    async def shutdown(self) -> None:
        """Stop all sandboxes and cancel reaper. Call during app shutdown."""
        if self._shutdown_done:
            return
        self._shutdown_done = True

        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None

        async with self._lock:
            for key, entry in self._pool.items():
                try:
                    close = getattr(entry.backend, "close", None)
                    if close:
                        await asyncio.to_thread(close)
                except Exception as e:
                    logger.warning("Error stopping sandbox %s/%s: %s", key[0][:8], key[1], e)
            self._pool.clear()
        logger.info("Sandbox pool shut down")

    def _sync_close_all(self) -> None:
        """Synchronous fallback called by atexit to destroy leaked sandboxes.

        When the process is killed abruptly (e.g. mprocs 'q'), the async
        lifespan shutdown may not complete. This ensures sandboxes are
        cleaned up as long as Python runs atexit handlers (SIGTERM default).
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True

        entries = list(self._pool.values())
        self._pool.clear()
        for entry in entries:
            try:
                close = getattr(entry.backend, "close", None)
                if close:
                    close()
            except Exception as e:
                logger.warning("atexit: error closing sandbox: %s", e)
        if entries:
            logger.info("atexit: closed %d sandbox(es)", len(entries))
