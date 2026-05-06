"""Connection pool management for Socket.IO clients.

This module manages in-memory caching of httpx and A2A clients per socket connection.

Design considerations for horizontal scaling:
1. Each server instance only caches its own active socket connections
2. Connections are cleaned up on disconnect or after idle timeout
3. If a server goes down, Socket.IO will reconnect to another instance
4. No shared state between instances - each manages its own connections
5. Connection health checks prevent stale connection issues
6. Configurable limits prevent resource exhaustion

Scalability:
- Each connection uses ~1-5MB memory + 2-3 file descriptors
- With 8GB instance: ~1000-2000 concurrent connections per instance
- For higher scale, deploy multiple instances behind load balancer
"""

import asyncio
import contextlib
import heapq
import logging
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from a2a.client import A2ACardResolver
from a2a.client.client import Client, ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.types import AgentCard, TransportProtocol
from pydantic import BaseModel, ConfigDict

from ..middleware import OrchestratorAuth

logger = logging.getLogger(__name__)


# Connection pool configuration
MAX_CONNECTIONS = 1000  # Maximum connections per instance
IDLE_TIMEOUT_SECONDS = 1800  # 30 minutes - close idle connections
CONNECTION_TIMEOUT = 600.0  # 10 minutes for long-running requests


class CachedConnection(BaseModel):
    """Cached connection with metadata for health management."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    httpx_client: httpx.AsyncClient
    auth: OrchestratorAuth | None
    a2a_clients: dict[str, Client]  # {agent_url: A2A_client}
    created_at: float
    last_used_at: float


class ConnectionEntry(BaseModel):
    """Priority queue entry for idle connection cleanup."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    last_used_at: float
    socket_id: str

    def __lt__(self, other: "ConnectionEntry") -> bool:
        """Compare by last_used_at for heap ordering."""
        return self.last_used_at < other.last_used_at

    def __gt__(self, other: "ConnectionEntry") -> bool:
        """Compare by last_used_at for heap ordering."""
        return self.last_used_at > other.last_used_at


class ConnectionPool:
    """Manages httpx and A2A client instances for active socket connections.

    Features:
    - Automatic cleanup of idle connections
    - Connection limits to prevent resource exhaustion
    - Health tracking for connection management
    - Thread-safe operations for concurrent access
    """

    def __init__(
        self,
        max_connections: int = MAX_CONNECTIONS,
        idle_timeout: int = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the connection pool.

        Args:
            max_connections: Maximum number of cached connections
            idle_timeout: Seconds of inactivity before connection cleanup
        """
        self._connections: dict[str, CachedConnection] = {}
        self._cleanup_queue: list[ConnectionEntry] = []  # Min-heap by last_used_at
        self._max_connections = max_connections
        self._idle_timeout = idle_timeout
        self._cleanup_task: asyncio.Task | None = None
        logger.info(f"ConnectionPool initialized (max: {max_connections}, idle_timeout: {idle_timeout}s)")

    def start_cleanup_task(self) -> None:
        """Start background task to clean up idle connections."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_idle_connections())
            logger.info("Started connection pool cleanup task")

    async def _cleanup_idle_connections(self) -> None:
        """Background task to periodically clean up idle connections.

        Uses a priority queue (min-heap) for efficient idle connection detection.
        Only checks connections at the front of the queue (oldest last_used_at).
        """
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._cleanup_expired_connections()
            except asyncio.CancelledError:
                logger.info("Connection pool cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in connection cleanup task: {e}", exc_info=True)

    async def _cleanup_expired_connections(self) -> int:
        """Clean up expired connections from the pool.

        This method contains the actual cleanup logic without the infinite loop,
        making it testable. Called by the background task and can be called
        directly in tests.

        Returns:
            Number of connections cleaned up
        """
        now = time.time()
        cleaned_count = 0

        # Process expired connections from the priority queue
        while self._cleanup_queue:
            # Peek at the oldest entry
            entry = self._cleanup_queue[0]

            # Connection no longer exists - pop stale entry and continue
            if entry.socket_id not in self._connections:
                heapq.heappop(self._cleanup_queue)
                continue

            # Get actual connection to check current timestamp
            conn = self._connections[entry.socket_id]

            # Entry is stale (connection was updated after this entry was created)
            # Pop it and continue checking
            if entry.last_used_at < conn.last_used_at:
                heapq.heappop(self._cleanup_queue)
                continue

            # Check if connection has actually expired based on its current timestamp
            if now - conn.last_used_at <= self._idle_timeout:
                # This connection is still fresh, and since heap is sorted by timestamp,
                # all remaining entries must also be fresh (or stale)
                break

            # Connection is expired - pop and clean up
            heapq.heappop(self._cleanup_queue)
            idle_duration = now - conn.last_used_at
            logger.warning(f"Cleaning up idle connection for socket {entry.socket_id} (idle for {idle_duration:.1f}s)")
            await self.remove(entry.socket_id, reason="idle_timeout")
            cleaned_count += 1

        if cleaned_count > 0:
            logger.info(f"Cleaned up {cleaned_count} idle connections")

        return cleaned_count

    def get(self, socket_id: str) -> tuple[httpx.AsyncClient, OrchestratorAuth | None] | None:
        """Get cached clients for a socket connection.

        Updates the last_used_at timestamp for connection health tracking.

        Args:
            socket_id: The Socket.IO session ID

        Returns:
            Tuple of (httpx_client, auth) if found, None otherwise
        """
        conn = self._connections.get(socket_id)
        if conn:
            conn.last_used_at = time.time()  # Update activity timestamp
            return (conn.httpx_client, conn.auth)
        return None

    def set(
        self,
        socket_id: str,
        httpx_client: httpx.AsyncClient,
        auth: OrchestratorAuth | None = None,
    ) -> bool:
        """Cache clients for a socket connection.

        Args:
            socket_id: The Socket.IO session ID
            httpx_client: The httpx async client
            auth: The orchestrator auth instance (optional)

        Returns:
            True if cached successfully, False if limit reached
        """
        # Update existing connection
        if socket_id in self._connections:
            now = time.time()
            # Update the connection in place to preserve created_at
            conn = self._connections[socket_id]
            conn.httpx_client = httpx_client
            conn.auth = auth
            conn.last_used_at = now
            # Add new entry to cleanup queue with updated timestamp
            # Old entries will be detected as stale and skipped during cleanup
            heapq.heappush(self._cleanup_queue, ConnectionEntry(last_used_at=now, socket_id=socket_id))
            logger.debug(f"Updated cached connection for socket {socket_id}")
            return True

        # Check connection limit for new connections
        if len(self._connections) >= self._max_connections:
            logger.warning(
                f"Connection pool limit reached ({self._max_connections}). Cannot cache connection for {socket_id}"
            )
            return False

        now = time.time()
        self._connections[socket_id] = CachedConnection(
            httpx_client=httpx_client,
            auth=auth,
            a2a_clients={},
            created_at=now,
            last_used_at=now,
        )
        # Add to cleanup queue
        heapq.heappush(self._cleanup_queue, ConnectionEntry(last_used_at=now, socket_id=socket_id))
        logger.debug(f"Cached connection for socket {socket_id} (total: {len(self._connections)})")
        return True

    async def remove(self, socket_id: str, reason: str = "disconnect") -> None:
        """Remove and clean up clients for a socket connection.

        Args:
            socket_id: The Socket.IO session ID
            reason: Reason for removal (for logging)
        """
        if socket_id not in self._connections:
            return

        conn = self._connections.pop(socket_id)

        # Clean up A2A clients first (no explicit cleanup needed for A2A clients, is just garbage collection)
        conn.a2a_clients.clear()
        # Clean up resources with proper exception handling
        try:
            await conn.httpx_client.aclose()
        except Exception as e:
            logger.error(f"Error closing httpx client for {socket_id}: {e}", exc_info=True)

        if conn.auth is not None:
            try:
                await conn.auth.aclose()
            except Exception as e:
                logger.error(f"Error closing auth for {socket_id}: {e}", exc_info=True)

        logger.debug(
            f"Cleaned up connection for socket {socket_id} (reason: {reason}, total: {len(self._connections)})"
        )

    def has(self, socket_id: str) -> bool:
        """Check if clients are cached for a socket connection.

        Args:
            socket_id: The Socket.IO session ID

        Returns:
            True if clients are cached, False otherwise
        """
        return socket_id in self._connections

    async def get_or_create_a2a_client(
        self,
        socket_id: str,
        agent_url: str,
    ) -> Client | None:
        """Get or create A2A client for a socket connection.

        Args:
            socket_id: The Socket.IO session ID
            agent_url: The agent card URL

        Returns:
            A2A Client instance or None if connection not found

        Raises:
            Exception: If client creation fails
        """
        conn = self._connections.get(socket_id)
        if not conn:
            logger.warning(f"No connection found for socket {socket_id}")
            return None

        # Update activity timestamp
        conn.last_used_at = time.time()

        # Check if A2A client already exists for this agent
        if agent_url in conn.a2a_clients:
            return conn.a2a_clients[agent_url]

        # Create new A2A client using the connection's httpx client
        try:
            agent_card = await self._fetch_agent_card(agent_url, conn.httpx_client)
            a2a_client = self._create_a2a_client(agent_card, conn.httpx_client)

            # Cache the A2A client
            conn.a2a_clients[agent_url] = a2a_client

            return a2a_client

        except Exception as e:
            logger.error(f"Failed to create A2A client for socket {socket_id}, agent {agent_url}: {e}", exc_info=True)
            raise

    async def get_agent_card(self, socket_id: str, agent_url: str) -> AgentCard | None:
        """Fetch agent card on-demand for a socket connection.

        This does not cache agent cards; it uses the connection's httpx client
        to fetch the agent card each time.

        Args:
            socket_id: The Socket.IO session ID
            agent_url: The agent card URL

        Returns:
            AgentCard instance or None if connection not found
        """
        conn = self._connections.get(socket_id)
        if not conn:
            return None

        # Use connection's httpx client to fetch the card on demand
        return await self._fetch_agent_card(agent_url, conn.httpx_client)

    async def _fetch_agent_card(self, agent_url: str, httpx_client: httpx.AsyncClient) -> AgentCard:
        """Fetch agent card from URL using existing httpx client.

        Args:
            agent_url: The agent card URL
            httpx_client: The existing httpx client to use

        Returns:
            AgentCard instance

        Raises:
            Exception: If fetching fails
        """
        card_resolver = self._get_card_resolver(httpx_client, agent_url)
        agent_card = await card_resolver.get_agent_card()

        logger.debug(f"Fetched agent card from {agent_url}")
        return agent_card

    def _get_card_resolver(self, client: httpx.AsyncClient, agent_card_url: str) -> A2ACardResolver:
        """Create A2ACardResolver for the given agent card URL.

        Args:
            client: httpx client for fetching
            agent_card_url: The agent card URL

        Returns:
            A2ACardResolver instance
        """
        parsed_url = urlparse(agent_card_url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        path_with_query = urlunparse(("", "", parsed_url.path, "", parsed_url.query, ""))
        card_path = path_with_query.lstrip("/")

        if card_path:
            return A2ACardResolver(client, base_url, agent_card_path=card_path)

        return A2ACardResolver(client, base_url)

    def _create_a2a_client(self, agent_card: AgentCard, httpx_client: httpx.AsyncClient) -> Client:
        """Create A2A client from agent card.

        Args:
            agent_card: The agent card
            httpx_client: The httpx client to use for agent communication

        Returns:
            A2A Client instance
        """
        a2a_config = ClientConfig(
            supported_transports=[
                TransportProtocol.http_json,
                TransportProtocol.jsonrpc,
            ],
            use_client_preference=True,
            httpx_client=httpx_client,
        )
        factory = ClientFactory(a2a_config)
        return factory.create(agent_card)

    def get_stats(self) -> dict[str, Any]:
        """Get connection pool statistics.

        Returns:
            Dict with pool statistics
        """
        now = time.time()
        connections = list(self._connections.values())
        total_a2a_clients = sum(len(c.a2a_clients) for c in connections)

        return {
            "total_connections": len(self._connections),
            "total_a2a_clients": total_a2a_clients,
            "max_connections": self._max_connections,
            "utilization_pct": (len(self._connections) / self._max_connections * 100)
            if self._max_connections > 0
            else 0,
            "idle_timeout_seconds": self._idle_timeout,
            "avg_connection_age_seconds": (
                sum(now - c.created_at for c in connections) / len(connections) if connections else 0
            ),
            "avg_idle_seconds": (
                sum(now - c.last_used_at for c in connections) / len(connections) if connections else 0
            ),
            "avg_a2a_clients_per_connection": (total_a2a_clients / len(connections) if connections else 0),
        }

    def get_idle_connections(self, idle_threshold_seconds: float | None = None) -> list[str]:
        """Get list of socket IDs for connections that have been idle beyond threshold.

        Useful for monitoring and health checks.

        Args:
            idle_threshold_seconds: Idle time threshold in seconds.
                                   Defaults to the pool's idle_timeout if not provided.

        Returns:
            List of socket IDs for connections exceeding the idle threshold
        """
        threshold = idle_threshold_seconds if idle_threshold_seconds is not None else self._idle_timeout
        now = time.time()
        return [socket_id for socket_id, conn in self._connections.items() if (now - conn.last_used_at) > threshold]

    async def clear_all(self) -> None:
        """Clean up all cached connections (for shutdown)."""
        socket_ids = list(self._connections.keys())
        for socket_id in socket_ids:
            await self.remove(socket_id, reason="shutdown")
        logger.info("Cleared all connections from pool")

        # Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None


# Global connection pool instance (per server instance)
connection_pool = ConnectionPool()
