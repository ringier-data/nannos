"""PostgreSQL database connection management using async SQLAlchemy.

Two separate connection pools are maintained:

- **API pool** (`get_async_session_factory`): Used by HTTP request handlers,
  middleware, and lightweight background tasks (scheduler, notifications).
- **Sync pool** (`get_sync_session_factory`): Used exclusively by the catalog
  sync pipeline which opens many concurrent sessions (one per file being
  processed).  Isolating it prevents long-running syncs from starving API
  requests of database connections.
"""

import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import config

# -- API pool (request handlers, middleware, scheduler) --
_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None

# -- Sync pool (catalog sync pipeline) --
_sync_engine: AsyncEngine | None = None
_sync_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Get the current async engine instance."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get the API async session factory."""
    if _async_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _async_session_factory


def get_sync_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get the catalog-sync async session factory (separate pool)."""
    if _sync_session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _sync_session_factory


def _make_engine(*, pool_size: int, max_overflow: int) -> AsyncEngine:
    echo = os.getenv("SQL_ECHO", "false").lower() in ("1", "true", "yes")
    return create_async_engine(
        config.postgres.connection_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=30,
        # Auto-terminate sessions stuck in "idle in transaction" for >5 min.
        # Prevents Ctrl+C during sync from leaving locks that block migrations.
        connect_args={"server_settings": {"idle_in_transaction_session_timeout": "300000"}},
    )


def _make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def init_db() -> None:
    """Initialize both database connection pools.

    Should be called during application startup.
    """
    global _engine, _async_session_factory, _sync_engine, _sync_session_factory

    if _engine is not None:
        return  # Already initialized

    # API pool — serves HTTP handlers, middleware, scheduler, etc.
    _engine = _make_engine(pool_size=5, max_overflow=10)
    _async_session_factory = _make_session_factory(_engine)

    # Sync pool — serves the catalog sync pipeline (high concurrency)
    _sync_engine = _make_engine(pool_size=3, max_overflow=12)
    _sync_session_factory = _make_session_factory(_sync_engine)


async def close_db() -> None:
    """Close both database connection pools.

    Should be called during application shutdown.
    """
    global _engine, _async_session_factory, _sync_engine, _sync_session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None

    if _sync_engine is not None:
        await _sync_engine.dispose()
        _sync_engine = None
        _sync_session_factory = None


def force_reset_db_state() -> None:
    """Clear engine and session factory globals without async disposal.

    Used in tests to prevent 'attached to a different loop' errors when
    re-using the module-level app singleton across test functions that each
    run on their own event loop.
    """
    global _engine, _async_session_factory, _sync_engine, _sync_session_factory
    _engine = None
    _async_session_factory = None
    _sync_engine = None
    _sync_session_factory = None
