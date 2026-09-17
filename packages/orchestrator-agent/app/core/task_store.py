"""A2A task store selection for the orchestrator.

The store itself is :func:`ringier_a2a_sdk.agent.task_store.create_task_store`, shared
with agent-runner, which grew a near-identical copy of this function. Only what is
specific to this service stays here: where its settings come from, and the two knobs it
deliberately does not set.
"""

import logging

from ringier_a2a_sdk.agent.task_store import AsyncEngine, TaskStore, create_task_store as _create_task_store

from app.models.config import AgentSettings

logger = logging.getLogger(__name__)


def create_task_store() -> tuple[TaskStore, AsyncEngine | None]:
    """The orchestrator's A2A task store: PostgreSQL-backed when configured.

    Two knobs are left at their defaults on purpose.

    ``table_name`` is unset, so this keeps the SDK's default ``tasks`` table — the one
    this service already owns and has rows in. agent-runner is the service that had to
    name its own.

    ``schema`` is unset, so the table stays wherever the role's ``search_path`` resolves
    it today. Pinning it to ``POSTGRES_SCHEMA`` would not move the existing table; it
    would create a new empty one in the configured schema and stop this service seeing
    the tasks it already had. Moving it is a migration, not a refactor, and is not part
    of sharing this code.

    Note the gate changed with the move: it is now ``POSTGRES_HOST`` alone, matching the
    checkpointer, rather than host AND password. A deployment authenticating by IAM,
    trust or ``.pgpass`` used to get a durable checkpointer beside a volatile task store
    — the split ADR-0009 decision 3 forbids — and now gets a durable store like every
    other Postgres-backed thing this service builds.
    """
    return _create_task_store(
        host=AgentSettings.POSTGRES_HOST,
        port=AgentSettings.POSTGRES_PORT,
        database=AgentSettings.POSTGRES_DB,
        user=AgentSettings.POSTGRES_USER,
        password=AgentSettings.POSTGRES_PASSWORD,
        service="orchestrator-agent",
    )
