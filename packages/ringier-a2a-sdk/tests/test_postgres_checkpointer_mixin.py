"""Unit tests for PostgreSQLCheckpointerMixin lifecycle and helpers.

Covers the paths that test_postgres_checkpointer_s3_offload.py does not:
    - _build_serde() factory
    - _create_checkpointer() fallback to MemorySaver
    - _create_checkpointer() pool construction with correct kwargs
    - _verify_postgres_version() boundary cases
    - _setup_checkpointer() lifecycle (open → version check → swap → setup)
    - _teardown_checkpointer() close behavior
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the real langgraph.checkpoint subpackage before stubbing siblings
# under it, so we don't shadow it with a bare ModuleType.
from langgraph.checkpoint.memory import MemorySaver


# Stub modules for postgres-only deps so the test file imports cleanly even
# when langgraph-checkpoint-postgres / psycopg_pool aren't installed (they
# live behind the `langgraph` optional-dependencies extra). Individual tests
# patch the same symbols to assert correct usage.
def _install_submodule(modname: str, **attrs) -> None:
    if modname not in sys.modules:
        sys.modules[modname] = types.ModuleType(modname)
    for name, value in attrs.items():
        setattr(sys.modules[modname], name, value)


if "psycopg_pool" not in sys.modules:
    _install_submodule("psycopg_pool", AsyncConnectionPool=MagicMock())
if "langgraph.checkpoint.postgres" not in sys.modules:
    _install_submodule("langgraph.checkpoint.postgres")
    _install_submodule("langgraph.checkpoint.postgres.aio", AsyncPostgresSaver=MagicMock())
    sys.modules["langgraph.checkpoint"].postgres = sys.modules["langgraph.checkpoint.postgres"]
    sys.modules["langgraph.checkpoint.postgres"].aio = sys.modules["langgraph.checkpoint.postgres.aio"]


from ringier_a2a_sdk.agent import postgres_checkpointer_mixin as mod  # noqa: E402
from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import (  # noqa: E402
    PostgreSQLCheckpointerMixin,
    S3OffloadingSerde,
    _build_serde,
    _verify_postgres_version,
)


# ─── helpers ──────────────────────────────────────────────────────────────────


class _AsyncCM:
    """Minimal async context manager that yields a preset value."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return None


def _fake_pool_with_version(version_num: int) -> MagicMock:
    """Build a pool mock whose `connection()` yields a conn returning version_num."""
    result = MagicMock()
    result.fetchone = AsyncMock(return_value=[version_num])
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)
    pool = MagicMock()
    pool.connection = MagicMock(return_value=_AsyncCM(conn))
    return pool


class _Stub(PostgreSQLCheckpointerMixin):
    """Concrete subclass used to call mixin methods on an instance."""


# ─── _build_serde ─────────────────────────────────────────────────────────────


class TestBuildSerde:
    def test_returns_none_when_bucket_unset(self, monkeypatch):
        monkeypatch.delenv("CHECKPOINT_S3_BUCKET_NAME", raising=False)
        assert _build_serde() is None

    def test_returns_serde_with_default_threshold(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINT_S3_BUCKET_NAME", "test-bucket")
        monkeypatch.delenv("CHECKPOINT_S3_THRESHOLD_MB", raising=False)

        serde = _build_serde()

        assert isinstance(serde, S3OffloadingSerde)
        assert serde._bucket == "test-bucket"
        # Default is 10 MB → 10 * 1024 * 1024 bytes
        assert serde._threshold == 10 * 1024 * 1024

    def test_returns_serde_with_custom_threshold(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINT_S3_BUCKET_NAME", "another-bucket")
        monkeypatch.setenv("CHECKPOINT_S3_THRESHOLD_MB", "0.5")

        serde = _build_serde()

        assert isinstance(serde, S3OffloadingSerde)
        assert serde._bucket == "another-bucket"
        assert serde._threshold == int(0.5 * 1024 * 1024)


# ─── _create_checkpointer ─────────────────────────────────────────────────────


class TestCreateCheckpointer:
    def test_falls_back_to_memory_saver_when_host_unset(self, monkeypatch):
        monkeypatch.delenv("CHECKPOINT_POSTGRES_HOST", raising=False)
        stub = _Stub()

        result = stub._create_checkpointer()

        assert isinstance(result, MemorySaver)
        assert stub._checkpointer_pool is None

    def test_constructs_pool_with_required_kwargs(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINT_POSTGRES_HOST", "db.example.com")
        monkeypatch.setenv("CHECKPOINT_POSTGRES_PORT", "5433")
        monkeypatch.setenv("CHECKPOINT_POSTGRES_DB", "ckpt")
        monkeypatch.setenv("CHECKPOINT_POSTGRES_USER", "ckpt_user")
        monkeypatch.setenv("CHECKPOINT_POSTGRES_PASSWORD", "s3cret")

        with patch("psycopg_pool.AsyncConnectionPool") as mock_pool_cls:
            sentinel = MagicMock(name="pool_instance")
            mock_pool_cls.return_value = sentinel

            stub = _Stub()
            result = stub._create_checkpointer()

        mock_pool_cls.assert_called_once()
        call_kwargs = mock_pool_cls.call_args.kwargs
        assert call_kwargs["conninfo"] == "postgresql://ckpt_user:s3cret@db.example.com:5433/ckpt"
        assert call_kwargs["open"] is False
        assert call_kwargs["kwargs"] == {"autocommit": True, "prepare_threshold": 0}

        assert stub._checkpointer_pool is sentinel
        # Placeholder is a MemorySaver until _setup_checkpointer swaps it.
        assert isinstance(result, MemorySaver)


# ─── _verify_postgres_version ─────────────────────────────────────────────────


class TestVerifyPostgresVersion:
    @pytest.mark.asyncio
    async def test_rejects_postgres_10(self):
        # 100023 = PG 10.23. The mixin formats minor as (n % 10000) // 100,
        # which gives 0 for PG 10.x (pre-PG-10 layout); we only care that
        # the RuntimeError carries the actual server_version_num.
        pool = _fake_pool_with_version(100023)
        with pytest.raises(RuntimeError, match=r"server_version_num=100023"):
            await _verify_postgres_version(pool)

    @pytest.mark.asyncio
    async def test_accepts_postgres_11_exact(self):
        pool = _fake_pool_with_version(110000)  # PG 11.0 (minimum)
        await _verify_postgres_version(pool)  # should not raise

    @pytest.mark.asyncio
    async def test_accepts_postgres_16(self):
        pool = _fake_pool_with_version(160003)  # PG 16.3
        await _verify_postgres_version(pool)  # should not raise


# ─── _setup_checkpointer ──────────────────────────────────────────────────────


class TestSetupCheckpointer:
    @pytest.mark.asyncio
    async def test_no_op_when_pool_is_none(self):
        stub = _Stub()
        stub._checkpointer_pool = None
        stub._checkpointer = MemorySaver()

        await stub._setup_checkpointer()

        # MemorySaver still in place.
        assert isinstance(stub._checkpointer, MemorySaver)

    @pytest.mark.asyncio
    async def test_full_lifecycle_without_s3(self, monkeypatch):
        monkeypatch.delenv("CHECKPOINT_S3_BUCKET_NAME", raising=False)

        pool = _fake_pool_with_version(160003)
        pool._opened = False
        pool.open = AsyncMock()

        fake_saver = MagicMock()
        fake_saver.setup = AsyncMock()

        with patch("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", return_value=fake_saver) as saver_cls:
            stub = _Stub()
            stub._checkpointer_pool = pool
            stub._checkpointer = MemorySaver()

            await stub._setup_checkpointer()

        pool.open.assert_awaited_once()
        saver_cls.assert_called_once()
        # serde kwarg is None when S3 offloading is disabled.
        assert saver_cls.call_args.kwargs.get("serde") is None
        fake_saver.setup.assert_awaited_once()
        # MemorySaver placeholder has been swapped for the real saver.
        assert stub._checkpointer is fake_saver

    @pytest.mark.asyncio
    async def test_passes_s3_serde_when_bucket_set(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINT_S3_BUCKET_NAME", "ckpt-bucket")
        monkeypatch.setenv("CHECKPOINT_S3_THRESHOLD_MB", "1")

        pool = _fake_pool_with_version(160003)
        pool._opened = False
        pool.open = AsyncMock()

        fake_saver = MagicMock()
        fake_saver.setup = AsyncMock()

        with patch("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", return_value=fake_saver) as saver_cls:
            stub = _Stub()
            stub._checkpointer_pool = pool
            stub._checkpointer = MemorySaver()

            await stub._setup_checkpointer()

        passed_serde = saver_cls.call_args.kwargs.get("serde")
        assert isinstance(passed_serde, S3OffloadingSerde)
        assert passed_serde._bucket == "ckpt-bucket"
        assert passed_serde._threshold == 1 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_skips_open_when_already_opened(self, monkeypatch):
        monkeypatch.delenv("CHECKPOINT_S3_BUCKET_NAME", raising=False)

        pool = _fake_pool_with_version(160003)
        pool._opened = True
        pool.open = AsyncMock()

        fake_saver = MagicMock()
        fake_saver.setup = AsyncMock()

        with patch("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", return_value=fake_saver):
            stub = _Stub()
            stub._checkpointer_pool = pool
            stub._checkpointer = MemorySaver()

            await stub._setup_checkpointer()

        pool.open.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_propagates_old_postgres_error(self, monkeypatch):
        pool = _fake_pool_with_version(100023)  # PG 10.23
        pool._opened = False
        pool.open = AsyncMock()

        with patch("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver") as saver_cls:
            stub = _Stub()
            stub._checkpointer_pool = pool
            stub._checkpointer = MemorySaver()

            with pytest.raises(RuntimeError, match="not supported"):
                await stub._setup_checkpointer()

        # AsyncPostgresSaver must not be constructed when the version check fails.
        saver_cls.assert_not_called()


# ─── _teardown_checkpointer ───────────────────────────────────────────────────


class TestTeardownCheckpointer:
    @pytest.mark.asyncio
    async def test_no_op_when_pool_is_none(self):
        stub = _Stub()
        stub._checkpointer_pool = None
        await stub._teardown_checkpointer()  # must not raise

    @pytest.mark.asyncio
    async def test_no_op_when_pool_not_opened(self):
        pool = MagicMock()
        pool._opened = False
        pool.close = AsyncMock()

        stub = _Stub()
        stub._checkpointer_pool = pool

        await stub._teardown_checkpointer()

        pool.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_closes_pool_when_opened(self):
        pool = MagicMock()
        pool._opened = True
        pool.close = AsyncMock()

        stub = _Stub()
        stub._checkpointer_pool = pool

        await stub._teardown_checkpointer()

        pool.close.assert_awaited_once()


# Ensure no test mutated module-level state.
def test_module_constants_unchanged():
    assert mod._MIN_PG_VERSION == 110000
