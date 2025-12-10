"""PostgreSQL database connection management using async SQLAlchemy."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import config

# Module-level engine and session factory
_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Get the current async engine instance."""
    if _engine is None:
        raise RuntimeError('Database not initialized. Call init_db() first.')
    return _engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get the current async session factory."""
    if _async_session_factory is None:
        raise RuntimeError('Database not initialized. Call init_db() first.')
    return _async_session_factory


async def init_db() -> None:
    """Initialize the database connection pool.

    Should be called during application startup.
    """
    global _engine, _async_session_factory

    if _engine is not None:
        return  # Already initialized

    # Create async engine with connection pooling
    # Using NullPool for serverless environments, switch to default pool for traditional servers
    _engine = create_async_engine(
        config.postgres.connection_url,
        echo=config.is_local(),  # Log SQL in local development
        pool_pre_ping=True,  # Verify connections before using
        # For traditional servers, you might want:
        # pool_size=5,
        # max_overflow=10,
        # pool_timeout=30,
    )

    # Create async session factory
    _async_session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,  # Prevent expired object access after commit
        autoflush=False,
    )


async def close_db() -> None:
    """Close the database connection pool.

    Should be called during application shutdown.
    """
    global _engine, _async_session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None
