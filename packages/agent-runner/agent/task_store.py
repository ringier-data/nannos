"""The A2A task store agent-runner serves its scheduled sub-agents from.

A scheduled run that parks on its owner's authorization leaves its task non-terminal on
purpose: the answer is a ``message/send`` addressed to that task, which the request
handler accepts only while it has not reached a terminal state. That makes the task
record the thing the resume depends on, so it has to outlive the process — and
agent-runner is the service that gets OOMKilled mid-run. An ``InMemoryTaskStore`` would
turn every restart into a job stopped forever with nothing left to address. See
docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.

The store itself is :func:`ringier_a2a_sdk.agent.task_store.create_task_store`, shared
with the orchestrator. Only the two decisions that differ between the services live
here: which table, and whether the schema is pinned.
"""

from __future__ import annotations

import os

from ringier_a2a_sdk.agent.task_store import AsyncEngine, TaskStore
from ringier_a2a_sdk.agent.task_store import create_task_store as _create_task_store

#: Separate from the orchestrator's default ``tasks`` table. Both services default to the
#: same ``POSTGRES_SCHEMA`` against the same database, so sharing the SDK's default table
#: would let either service ``tasks/get`` and ``tasks/cancel`` the other's tasks.
#: ADR-0008's shared-store constraint is about replicas of ONE service; extending it
#: across two is a trust boundary this decision does not need.
TASK_TABLE_NAME = "agent_runner_tasks"


def create_task_store() -> tuple[TaskStore, AsyncEngine | None]:
    """agent-runner's durable task store, or the in-memory fallback without Postgres.

    The schema IS pinned here, unlike the orchestrator's: this table is new, so there is
    nothing to move and pinning stops two replicas creating an ``agent_runner_tasks``
    neither can see the other's parked tasks in. ADR-0008's durability constraint is that
    the task record is reachable from EVERY replica, which a role-default ``search_path``
    does not give.
    """
    return _create_task_store(
        host=os.getenv("POSTGRES_HOST"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        database=os.getenv("POSTGRES_DB", "postgres"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        table_name=TASK_TABLE_NAME,
        schema=os.getenv("POSTGRES_SCHEMA"),
        service="agent-runner",
    )
