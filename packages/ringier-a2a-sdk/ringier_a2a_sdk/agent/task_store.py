"""A2A task store selection, shared by every service that serves A2A tasks.

The A2A SDK's ``InMemoryTaskStore`` is an unbounded dict: every task ever handled —
with its full message history and artifacts — stays in process memory until the pod
dies, which on a long-lived replica is a slow-motion OOM. It also loses every task on
restart, and a task that is deliberately left non-terminal is the only thing an answer
can be addressed to later (see the scheduler's parked runs, ADR-0009). So when
PostgreSQL is configured the tasks are persisted there instead.

Lives beside :mod:`postgres_checkpointer_mixin` because it is the same concern: a
Postgres-backed store built from the service's own ``POSTGRES_*`` connection, gated the
same way, falling back to memory the same way. Two services had grown near-identical
copies of this function that disagreed about the gate; one copy is the point.
"""

from __future__ import annotations

import logging

from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore, TaskStore
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)


def schema_connect_args(schema: str | None) -> dict[str, str]:
    """Pin a connection's ``search_path`` to *schema*, or leave the role default alone.

    Exposed separately because it is the one part worth asserting in a unit test without
    standing up an engine.
    """
    return {"options": f"-csearch_path={schema}"} if schema else {}


def create_task_store(
    *,
    host: str | None,
    port: int | str = 5432,
    database: str = "postgres",
    user: str = "postgres",
    password: str = "",
    table_name: str | None = None,
    schema: str | None = None,
    service: str = "this service",
) -> tuple[TaskStore, AsyncEngine | None]:
    """The A2A task store: PostgreSQL-backed when *host* is set, in-memory otherwise.

    Returns ``(store, engine)``; the engine is None for the in-memory fallback and the
    caller owns disposing it on shutdown.

    **Gated on the host alone**, which is the convention
    :mod:`postgres_checkpointer_mixin` already documents ("POSTGRES_HOST … Gates
    persistence"). Requiring a password too — as one caller used to — splits a
    deployment that authenticates by IAM, trust or ``.pgpass``: it gets a durable
    checkpointer beside a volatile task store, which is the configuration ADR-0009
    decision 3 says must not exist, and it fails only at the moment the store exists
    for, when a restart between a park and its answer leaves the graph resumable and the
    task it is addressed by gone.

    *table_name* separates services that share a database. Left unset, the A2A SDK's
    default table is used — which is correct only for the single service that owns it: a
    second service defaulting to the same table would let either ``tasks/get`` and
    ``tasks/cancel`` the other's tasks. ADR-0008's shared-store constraint is about
    replicas of ONE service; extending it across two is a trust boundary nothing needs.

    *schema* pins the connection's ``search_path``. Leave it None for a store whose
    table already exists somewhere: setting it would not move that table, it would
    create a new empty one in the configured schema and stop the service seeing the
    tasks it already had — a data-visibility change, not a refactor. Pass it for a new
    store, where pinning is free and stops two replicas creating a table neither can see
    the other's tasks in.
    """
    if not host:
        logger.warning(
            "POSTGRES_HOST not set — using in-memory A2A task store for %s. Tasks are lost on "
            "restart and accumulate in memory, so a task left open for a later answer "
            "cannot be answered after one.",
            service,
        )
        return InMemoryTaskStore(), None

    url = URL.create(
        drivername="postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=database,
    )
    engine = create_async_engine(
        url, pool_size=5, max_overflow=5, pool_pre_ping=True, connect_args=schema_connect_args(schema)
    )
    logger.info(
        "Using PostgreSQL-backed A2A task store for %s (table=%s, schema=%s)",
        service,
        table_name or "<sdk default>",
        schema or "<role default>",
    )
    store = (
        DatabaseTaskStore(engine, create_table=True, table_name=table_name)
        if table_name
        else DatabaseTaskStore(engine, create_table=True)
    )
    return store, engine
