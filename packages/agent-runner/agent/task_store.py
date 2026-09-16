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


def schema_connect_args(schema: str | None) -> dict[str, str]:
    """Pin the connection's ``search_path`` to the service's own schema.

    The same schema the checkpointer places its tables in. Left unset, the table lands in
    whatever the role's default happens to be, so two replicas — or the same service
    across two environments — can each create an ``agent_runner_tasks`` in which neither
    can see the other's parked tasks. ADR-0008's durability constraint is that the task
    record is reachable from EVERY replica, which a role-default search_path does not give.
    """
    return {"options": f"-csearch_path={schema}"} if schema else {}


def create_task_store() -> tuple[TaskStore, AsyncEngine | None]:
    """Create the A2A task store: PostgreSQL-backed when configured, in-memory otherwise.

    Returns the store and the engine backing it (None for the in-memory fallback);
    the caller owns disposal. The in-memory fallback exists for local runs without a
    database — a parked run cannot survive a restart there, which is a limitation of
    running without Postgres and not a mode to deploy.
    """
    # Gated on the HOST alone, exactly as the checkpointer in ``core.py`` is. Requiring a
    # password too would split the two: a deployment authenticating by IAM, trust or
    # .pgpass would get a durable checkpointer and a volatile task store — precisely the
    # configuration ADR-0009 decision 3 says must not exist, and one that fails only at
    # the moment it matters, when a restart between the park and the answer leaves the
    # graph resumable and the task it is addressed by gone.
    host = os.getenv("POSTGRES_HOST")
    if not host:
        logger.warning(
            "POSTGRES_HOST not set — using in-memory A2A task store. Tasks are lost on "
            "restart, so a run parked on its owner's authorization cannot be resumed after one."
        )
        return InMemoryTaskStore(), None

    url = URL.create(
        drivername="postgresql+psycopg",
        username=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        host=host,
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "postgres"),
    )
    schema = os.getenv("POSTGRES_SCHEMA")
    engine = create_async_engine(
        url, pool_size=5, max_overflow=5, pool_pre_ping=True, connect_args=schema_connect_args(schema)
    )
    logger.info(
        "Using PostgreSQL-backed A2A task store (table=%s, schema=%s)",
        TASK_TABLE_NAME,
        schema or "<role default>",
    )
    return DatabaseTaskStore(engine, create_table=True, table_name=TASK_TABLE_NAME), engine
