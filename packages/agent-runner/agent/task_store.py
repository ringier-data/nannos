"""The A2A task store agent-runner serves its scheduled sub-agents from.

A scheduled run that parks on its owner's authorization leaves its task
non-terminal on purpose: the answer is a ``message/send`` addressed to that task,
which the request handler accepts only while it has not reached a terminal state.
That makes the task record the thing the resume depends on, so it has to outlive
the process — and agent-runner is the service that gets OOMKilled mid-run. An
``InMemoryTaskStore`` would turn every restart into a job stopped forever with
nothing left to address. See
docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.

**The table is deliberately not the orchestrator's.** Both services default to the
same ``POSTGRES_SCHEMA`` against the same database, and ``DatabaseTaskStore``
defaults to a table called ``tasks`` — which the orchestrator already installs. Left
alone they would silently share one table, and either service could then ``tasks/get``
and ``tasks/cancel`` the other's tasks. ADR-0008's shared-store constraint is about
replicas of ONE service; extending it across two is a trust boundary nothing here
needs, so the name is set explicitly rather than defaulted.
"""

import logging
import os

from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore, TaskStore
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

#: Separate from the orchestrator's ``tasks``. See the module docstring.
TASK_TABLE_NAME = "agent_runner_tasks"


def create_task_store() -> tuple[TaskStore, AsyncEngine | None]:
    """Create the A2A task store: PostgreSQL-backed when configured, in-memory otherwise.

    Returns the store and the engine backing it (None for the in-memory fallback);
    the caller owns disposal. The in-memory fallback exists for local runs without a
    database — a parked run cannot survive a restart there, which is a limitation of
    running without Postgres and not a mode to deploy.
    """
    host = os.getenv("POSTGRES_HOST")
    password = os.getenv("POSTGRES_PASSWORD", "")
    if not (host and password):
        logger.warning(
            "PostgreSQL not configured — using in-memory A2A task store. Tasks are lost on "
            "restart, so a run parked on its owner's authorization cannot be resumed after one. "
            "Set POSTGRES_HOST and POSTGRES_PASSWORD to enable persistence."
        )
        return InMemoryTaskStore(), None

    url = URL.create(
        drivername="postgresql+psycopg",
        username=os.getenv("POSTGRES_USER", "postgres"),
        password=password,
        host=host,
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "postgres"),
    )
    engine = create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)
    logger.info("Using PostgreSQL-backed A2A task store (table=%s)", TASK_TABLE_NAME)
    return DatabaseTaskStore(engine, create_table=True, table_name=TASK_TABLE_NAME), engine
