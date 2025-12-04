"""Orchestrator cookie cache with TTL and DynamoDB backing.

This module provides a cache for orchestrator session cookies that combines:
- In-memory TTL cache for fast access (reduces DynamoDB reads)
- DynamoDB backing for persistence across backend server restarts
- Async locks per session_id to prevent cache stampede

The cache is designed for horizontal scaling where multiple backend servers
need to share orchestrator session state through DynamoDB.
"""

import logging

from asyncio import Lock
from datetime import datetime
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from cachetools import TTLCache


if TYPE_CHECKING:
    from services.session_service import SessionService


logger = logging.getLogger(__name__)


class OrchestratorCookieCache:
    """In-memory cache with TTL and DynamoDB backing for orchestrator cookies.

    This cache provides:
    - Fast in-memory access with automatic TTL expiration
    - DynamoDB backing for persistence and horizontal scaling
    - Per-session async locks to prevent duplicate fetches (cache stampede)
    - Automatic cleanup of expired entries
    """

    def __init__(
        self,
        session_service: 'SessionService',
        ttl: int = 60,
        maxsize: int = 10000,
    ) -> None:
        """Initialize the orchestrator cookie cache.

        Args:
            session_service: The session service for DynamoDB operations
            ttl: Time-to-live for cache entries in seconds (default: 60)
            maxsize: Maximum number of entries in cache (default: 10000)
        """
        self.session_service = session_service
        self.ttl = ttl
        self.maxsize = maxsize

        # TTL cache for orchestrator cookies
        # Key: session_id, Value: (cookie_value, expires_at)
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)

        # Per-session locks to prevent cache stampede
        # Using WeakValueDictionary so locks are automatically garbage collected
        # when no longer referenced
        self._locks: WeakValueDictionary = WeakValueDictionary()

        logger.info(f'OrchestratorCookieCache initialized (ttl={ttl}s, maxsize={maxsize})')

    def _get_lock(self, session_id: str) -> Lock:
        """Get or create an async lock for a session.

        Uses WeakValueDictionary to automatically clean up unused locks.
        The .get() method is used instead of direct dictionary access to avoid
        KeyError when garbage collection removes a lock.

        Args:
            session_id: The session ID

        Returns:
            An asyncio Lock for the session
        """
        lock = self._locks.get(session_id)
        if lock is None:
            lock = Lock()
            self._locks[session_id] = lock
        return lock

    async def get_cookie(self, session_id: str) -> tuple[str, datetime] | None:
        """Get orchestrator cookie from cache or DynamoDB.

        This method:
        1. Checks in-memory cache first (fast path)
        2. Falls back to DynamoDB on cache miss
        3. Uses per-session locks to prevent duplicate DynamoDB fetches

        Args:
            session_id: The session ID

        Returns:
            Tuple of (cookie_value, expires_at) if found and not expired, None otherwise
        """
        # Fast path: check cache first
        cached = self._cache.get(session_id)
        if cached is not None:
            cookie_value, expires_at = cached
            logger.debug(f'Cache hit for session {session_id}')
            return (cookie_value, expires_at)

        # Slow path: fetch from DynamoDB with lock to prevent stampede
        lock = self._get_lock(session_id)
        async with lock:
            # Double-check cache after acquiring lock (another coroutine may have populated it)
            cached = self._cache.get(session_id)
            if cached is not None:
                cookie_value, expires_at = cached
                logger.debug(f'Cache hit after lock for session {session_id}')
                return (cookie_value, expires_at)

            # Fetch from DynamoDB
            logger.debug(f'Cache miss for session {session_id}, fetching from DynamoDB')
            cookie_data = await self.session_service.get_orchestrator_cookie(session_id)

            if cookie_data is None:
                return None

            cookie_value, expires_at = cookie_data

            # Populate cache for future requests
            self._cache[session_id] = (cookie_value, expires_at)

            return (cookie_value, expires_at)

    async def set_cookie(
        self,
        session_id: str,
        cookie_value: str,
        expires_at: datetime,
    ) -> None:
        """Set orchestrator cookie in both cache and DynamoDB.

        This method updates both the in-memory cache and DynamoDB to ensure
        consistency across backend servers.

        Args:
            session_id: The session ID
            cookie_value: The orchestrator session cookie JWT
            expires_at: When the cookie expires
        """
        # Update DynamoDB first (source of truth)
        await self.session_service.update_orchestrator_cookie(
            session_id=session_id,
            cookie=cookie_value,
            expires_at=expires_at,
        )

        # Update cache
        self._cache[session_id] = (cookie_value, expires_at)
        logger.debug(f'Set orchestrator cookie for session {session_id}')

    async def clear_cookie(self, session_id: str) -> None:
        """Clear orchestrator cookie from both cache and DynamoDB.

        This method is called when user tokens are refreshed, as the old
        orchestrator cookie is tied to the expired token and must be invalidated.

        Args:
            session_id: The session ID
        """
        # Clear from DynamoDB first (source of truth)
        await self.session_service.clear_orchestrator_cookie(session_id)

        # Clear from cache
        if session_id in self._cache:
            del self._cache[session_id]

        logger.debug(f'Cleared orchestrator cookie for session {session_id}')

    def invalidate(self, session_id: str) -> None:
        """Invalidate cache entry for a session (cache-only operation).

        This is used when a cookie is detected as expired or invalid during
        request processing. It only clears the cache entry, not DynamoDB.

        Args:
            session_id: The session ID
        """
        if session_id in self._cache:
            del self._cache[session_id]
            logger.debug(f'Invalidated cache entry for session {session_id}')
