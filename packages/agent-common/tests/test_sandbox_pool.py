"""Tests for SandboxPool acquire/release/eviction lifecycle."""

import asyncio

import pytest


class MockSandboxHandle:
    """Mock sandbox handle mimicking SandboxBackendProtocol (GatanaSandbox-like)."""

    def __init__(self):
        self.uploaded_files: list[tuple[str, bytes]] = []
        self.closed = False

    def upload_files(self, files: list[tuple[str, bytes]]) -> list:
        self.uploaded_files.extend(files)
        return []

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
        self.uploaded_files.extend(files)
        return []

    def execute(self, command: str, *, timeout: int | None = None):
        return type("ExecuteResponse", (), {"output": "", "exit_code": 0})()

    def close(self) -> None:
        self.closed = True


# Track created handles for assertions
_created_handles: list[MockSandboxHandle] = []


async def _mock_create_fn() -> MockSandboxHandle:
    """Factory function that creates mock sandbox handles."""
    handle = MockSandboxHandle()
    _created_handles.append(handle)
    return handle


from agent_common.core.sandbox_pool import SandboxPool


@pytest.fixture(autouse=True)
def _reset_created():
    """Reset the created handles list before each test."""
    _created_handles.clear()


@pytest.mark.asyncio
async def test_acquire_creates_new_sandbox():
    """First acquire for a key provisions a new sandbox."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=5, warm_ttl=60.0)

    entry = await pool.acquire("session-1", "my-agent")
    assert entry.in_use is True
    assert entry.backend is not None
    assert len(_created_handles) == 1


@pytest.mark.asyncio
async def test_release_marks_not_in_use():
    """Release marks the sandbox as idle for warm reuse."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=5, warm_ttl=60.0)

    await pool.acquire("session-1", "my-agent")
    await pool.release("session-1", "my-agent")

    assert pool.active_count == 0


@pytest.mark.asyncio
async def test_warm_reuse_same_key():
    """Second acquire with same key reuses the warm sandbox."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=5, warm_ttl=60.0)

    entry1 = await pool.acquire("session-1", "my-agent")
    await pool.release("session-1", "my-agent")

    entry2 = await pool.acquire("session-1", "my-agent")
    assert entry2.backend is entry1.backend  # Same backend reused
    assert len(_created_handles) == 1  # No new sandbox created


@pytest.mark.asyncio
async def test_different_session_gets_different_sandbox():
    """Different session_id gets a separate sandbox (no cross-user leakage)."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=5, warm_ttl=60.0)

    entry1 = await pool.acquire("session-user-A", "my-agent")
    entry2 = await pool.acquire("session-user-B", "my-agent")

    assert entry1.backend is not entry2.backend
    assert len(_created_handles) == 2


@pytest.mark.asyncio
async def test_different_agent_gets_different_sandbox():
    """Different sub_agent_name gets a separate sandbox (skill isolation)."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=5, warm_ttl=60.0)

    entry1 = await pool.acquire("session-1", "agent-A")
    entry2 = await pool.acquire("session-1", "agent-B")

    assert entry1.backend is not entry2.backend
    assert len(_created_handles) == 2


@pytest.mark.asyncio
async def test_capacity_enforcement():
    """Pool raises RuntimeError when at capacity."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=2, warm_ttl=60.0)

    await pool.acquire("s1", "a1")
    await pool.acquire("s2", "a2")

    with pytest.raises(RuntimeError, match="at capacity"):
        await pool.acquire("s3", "a3")


@pytest.mark.asyncio
async def test_eviction_frees_capacity():
    """Expired idle sandboxes are evicted to make room for new ones."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=2, warm_ttl=0.01)  # Very short TTL

    entry1 = await pool.acquire("s1", "a1")
    await pool.release("s1", "a1")

    # Wait for TTL to expire
    await asyncio.sleep(0.02)

    # This should evict the idle one and succeed
    await pool.acquire("s2", "a2")
    await pool.acquire("s3", "a3")

    assert entry1.backend.closed  # Evicted sandbox was stopped
    assert len(_created_handles) == 3


@pytest.mark.asyncio
async def test_shutdown_stops_all():
    """Shutdown stops all sandboxes and clears the pool."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=5, warm_ttl=60.0)

    entry1 = await pool.acquire("s1", "a1")
    entry2 = await pool.acquire("s2", "a2")
    await pool.release("s1", "a1")

    await pool.shutdown()

    assert entry1.backend.closed
    assert entry2.backend.closed
    assert pool.active_count == 0


@pytest.mark.asyncio
async def test_skills_hash_preserved_on_reuse():
    """Skills hash is preserved across warm reuse (avoid re-upload)."""
    pool = SandboxPool(create_fn=_mock_create_fn, capacity=5, warm_ttl=60.0)

    entry = await pool.acquire("s1", "a1")
    entry.skills_hash = "abc123"
    await pool.release("s1", "a1")

    reused = await pool.acquire("s1", "a1")
    assert reused.skills_hash == "abc123"
