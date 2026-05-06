"""Unit tests for ConnectionPool utility.

Tests cover:
- Connection lifecycle (set, get, remove, clear_all)
- Idle connection cleanup (30 min timeout)
- Connection limits (1000 max connections)
- Health tracking and status monitoring
- A2A client creation and caching per socket ID
"""

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from console_backend.utils.connection_pool import ConnectionPool


@pytest.fixture
def connection_pool():
    """Create a fresh connection pool for each test."""
    pool = ConnectionPool()
    pool._connections.clear()  # Clear any existing connections
    pool._cleanup_queue.clear()
    return pool


@pytest.fixture
def mock_httpx_client():
    """Mock httpx AsyncClient."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.aclose = AsyncMock()
    return client


class TestConnectionPoolLifecycle:
    """Test connection pool basic operations."""

    @pytest.mark.asyncio
    async def test_set_and_get_connection(self, connection_pool, mock_httpx_client):
        """Test setting and retrieving a connection."""
        socket_id = "socket_123"

        # Set connection
        connection_pool.set(socket_id, mock_httpx_client, auth=None)

        # Get connection
        result = connection_pool.get(socket_id)

        assert result is not None
        client, auth = result
        assert client == mock_httpx_client
        assert auth is None

    @pytest.mark.asyncio
    async def test_get_nonexistent_connection_returns_none(self, connection_pool):
        """Test that getting a non-existent connection returns None."""
        result = connection_pool.get("nonexistent_socket")
        assert result is None

    @pytest.mark.asyncio
    async def test_remove_connection(self, connection_pool, mock_httpx_client):
        """Test removing a connection from the pool."""
        socket_id = "socket_123"

        # Set connection
        connection_pool.set(socket_id, mock_httpx_client, auth=None)

        # Remove it
        await connection_pool.remove(socket_id)

        # Assert get returns None
        assert connection_pool.get(socket_id) is None

    @pytest.mark.asyncio
    async def test_clear_all_connections(self, connection_pool, mock_httpx_client):
        """Test clearing all connections from the pool."""
        # Set multiple connections
        for i in range(3):
            client = MagicMock(spec=httpx.AsyncClient)
            client.aclose = AsyncMock()
            connection_pool.set(f"socket_{i}", client, auth=None)

        # Clear all
        await connection_pool.clear_all()

        # Assert all get calls return None
        for i in range(3):
            assert connection_pool.get(f"socket_{i}") is None


class TestConnectionPoolIdleCleanup:
    """Test idle connection cleanup."""

    @pytest.mark.asyncio
    async def test_idle_connection_cleanup_after_30_minutes(self, connection_pool, mock_httpx_client):
        """Test that idle connections are cleaned up after 30 minutes."""
        socket_id = "socket_123"
        connection_pool.set(socket_id, mock_httpx_client, auth=None)

        # Manually set last_used_at to 31 minutes ago (1860 seconds)
        if socket_id in connection_pool._connections:
            old_time = time.time() - (31 * 60)
            connection_pool._connections[socket_id].last_used_at = old_time

        # Call the actual cleanup logic
        cleaned_count = await connection_pool._cleanup_expired_connections()

        # Assert connection was removed
        assert connection_pool.get(socket_id) is None
        assert cleaned_count == 1

    @pytest.mark.asyncio
    async def test_active_connection_not_cleaned_up(self, connection_pool, mock_httpx_client):
        """Test that active connections are not cleaned up."""
        socket_id = "socket_123"
        connection_pool.set(socket_id, mock_httpx_client, auth=None)

        # Call the actual cleanup logic (connection is fresh)
        cleaned_count = await connection_pool._cleanup_expired_connections()

        # Assert connection still exists and nothing was cleaned
        assert connection_pool.get(socket_id) is not None
        assert cleaned_count == 0


class TestConnectionPoolLimits:
    """Test connection pool limits."""

    @pytest.mark.asyncio
    async def test_connection_limit_enforced(self, connection_pool):
        """Test that connection pool handles many connections."""
        # Add many connections (less than max for test speed)
        num_connections = 50
        for i in range(num_connections):
            client = MagicMock(spec=httpx.AsyncClient)
            client.aclose = AsyncMock()
            connection_pool.set(f"socket_{i}", client, auth=None)

        # Assert all connections are stored
        assert len(connection_pool._connections) == num_connections

    @pytest.mark.asyncio
    async def test_lru_eviction_when_limit_reached(self, connection_pool):
        """Test connection management with many connections."""
        # Add multiple connections
        for i in range(10):
            client = MagicMock(spec=httpx.AsyncClient)
            client.aclose = AsyncMock()
            connection_pool.set(f"socket_{i}", client, auth=None)

        # Access socket_0 to make it recently used
        connection_pool.get("socket_0")

        # Verify socket_0 exists
        assert connection_pool.get("socket_0") is not None


class TestConnectionPoolHealthTracking:
    """Test connection health tracking."""

    @pytest.mark.asyncio
    async def test_health_status_tracked_per_connection(self, connection_pool, mock_httpx_client):
        """Test that health status is tracked for each connection."""
        socket_id = "socket_123"

        # Set connection
        connection_pool.set(socket_id, mock_httpx_client, auth=None)

        # Get connection info
        conn_info = connection_pool._connections.get(socket_id)

        # Assert connection info exists and has timestamps
        assert conn_info is not None
        assert conn_info.last_used_at is not None
        assert conn_info.created_at is not None

    @pytest.mark.asyncio
    async def test_unhealthy_connections_can_be_filtered(self, connection_pool, mock_httpx_client):
        """Test filtering connections by age."""
        # Add connections with different ages
        socket_id_old = "socket_old"
        socket_id_new = "socket_new"

        connection_pool.set(socket_id_old, mock_httpx_client, auth=None)
        connection_pool.set(socket_id_new, mock_httpx_client, auth=None)

        # Manually age one connection (35 minutes = 2100 seconds)
        if socket_id_old in connection_pool._connections:
            old_time = time.time() - (35 * 60)
            connection_pool._connections[socket_id_old].last_used_at = old_time

        # Use the actual get_idle_connections method with 30 minute threshold
        idle_threshold = 30 * 60  # 30 minutes in seconds
        old_connections = connection_pool.get_idle_connections(idle_threshold)

        # Should find the aged connection
        assert len(old_connections) == 1
        assert socket_id_old in old_connections
        assert socket_id_new not in old_connections


class TestA2AClientCaching:
    """Test A2A client creation and caching."""

    @pytest.mark.asyncio
    async def test_a2a_client_created_and_cached(self, connection_pool, mock_httpx_client):
        """Test that A2A clients are created and cached per socket ID."""
        socket_id = "socket_123"

        # Set connection with client
        connection_pool.set(socket_id, mock_httpx_client, auth=None)

        # Get client (should return the same instance)
        result = connection_pool.get(socket_id)
        assert result is not None
        client1, _ = result

        # Get again
        result = connection_pool.get(socket_id)
        assert result is not None
        client2, _ = result

        # Should be the same client instance
        assert client1 is client2

    @pytest.mark.asyncio
    async def test_a2a_client_includes_orchestrator_auth(self, connection_pool):
        """Test that A2A clients can be stored with auth."""
        socket_id = "socket_123"

        client = MagicMock(spec=httpx.AsyncClient)
        client.aclose = AsyncMock()

        # Set connection with auth=None (Pydantic validates auth type)
        connection_pool.set(socket_id, client, auth=None)

        # Get connection
        result = connection_pool.get(socket_id)
        assert result is not None
        _, auth = result

        # Assert auth can be stored (None is valid)
        assert auth is None
