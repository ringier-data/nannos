"""A2A task store selection.

The A2A SDK's InMemoryTaskStore is an unbounded dict: every task ever handled
(with its full message history and artifacts) stays in process memory until the
pod dies — on a long-lived single-replica orchestrator this is a slow-motion
OOM. When PostgreSQL is configured (same gating as the document store), tasks
are persisted there instead and survive restarts.
"""

import logging

from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore, TaskStore
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models.config import AgentSettings

logger = logging.getLogger(__name__)


def create_task_store() -> tuple[TaskStore, AsyncEngine | None]:
    """Create the A2A task store: PostgreSQL-backed when configured, in-memory otherwise.

    Returns the store and the SQLAlchemy engine backing it (None for the
    in-memory fallback). The caller owns engine disposal on shutdown.
    """
    if not (AgentSettings.POSTGRES_HOST and AgentSettings.POSTGRES_PASSWORD):
        logger.warning(
            "PostgreSQL not configured – using in-memory A2A task store "
            "(tasks are lost on restart and accumulate in memory). "
            "Set POSTGRES_HOST and POSTGRES_PASSWORD to enable persistence."
        )
        return InMemoryTaskStore(), None

    url = URL.create(
        drivername="postgresql+psycopg",
        username=AgentSettings.POSTGRES_USER,
        password=AgentSettings.POSTGRES_PASSWORD,
        host=AgentSettings.POSTGRES_HOST,
        port=AgentSettings.POSTGRES_PORT,
        database=AgentSettings.POSTGRES_DB,
    )
    engine = create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)
    logger.info("Using PostgreSQL-backed A2A task store")
    return DatabaseTaskStore(engine, create_table=True), engine
