"""Docstore database connection for reading/writing LangGraph store data.

Provides a separate connection pool to the orchestrator's docstore database
where playbook files (AGENTS.md, skills) are stored in the LangGraph store table.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import config

logger = logging.getLogger(__name__)

_docstore_engine: AsyncEngine | None = None
_docstore_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_docstore_session_factory() -> async_sessionmaker[AsyncSession] | None:
    """Get the docstore async session factory. Returns None if not configured."""
    return _docstore_session_factory


async def init_docstore() -> None:
    """Initialize the docstore database connection pool.

    Should be called during application startup, after init_db().
    No-op if docstore is not configured.
    """
    global _docstore_engine, _docstore_session_factory

    if not config.docstore.is_configured:
        logger.info("Docstore not configured — playbook API will be unavailable")
        return

    if _docstore_engine is not None:
        return

    _docstore_engine = create_async_engine(
        config.docstore.connection_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=3,
        pool_timeout=30,
    )
    _docstore_session_factory = async_sessionmaker(
        bind=_docstore_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    logger.info("Docstore database connection initialized")


async def close_docstore() -> None:
    """Close the docstore database connection pool."""
    global _docstore_engine, _docstore_session_factory

    if _docstore_engine is not None:
        await _docstore_engine.dispose()
        _docstore_engine = None
        _docstore_session_factory = None
        logger.info("Docstore database connection closed")
