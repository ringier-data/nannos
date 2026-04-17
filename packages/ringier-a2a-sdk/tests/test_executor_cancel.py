"""Tests for BaseAgentExecutor.cancel() — A2A tasks/cancel support."""

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskState

from ringier_a2a_sdk.server.executor import BaseAgentExecutor


class _DummyAgent:
    """Minimal stub satisfying BaseAgentExecutor.__init__ signature."""

    pass


class ConcreteExecutor(BaseAgentExecutor):
    """Concrete subclass for testing (execute is covered elsewhere)."""

    pass


@pytest.fixture
def executor():
    return ConcreteExecutor(agent=_DummyAgent())


@pytest.mark.asyncio
async def test_cancel_emits_canceled_event(executor):
    """cancel() should enqueue a TaskStatusUpdateEvent with state=canceled."""
    queue = EventQueue()
    context = RequestContext(
        request=None,
        task_id="task-123",
        context_id="ctx-456",
    )

    await executor.cancel(context, queue)

    # Drain the queue
    events = []
    while not queue.queue.empty():
        events.append(await queue.queue.get())

    assert len(events) == 1
    event = events[0]
    assert event.status.state == TaskState.canceled
    assert event.task_id == "task-123"
    assert event.context_id == "ctx-456"
    assert event.final is True


@pytest.mark.asyncio
async def test_cancel_does_not_raise(executor):
    """cancel() should not raise UnsupportedOperationError anymore."""
    queue = EventQueue()
    context = RequestContext(
        request=None,
        task_id="task-1",
        context_id="ctx-1",
    )
    # Should NOT raise
    await executor.cancel(context, queue)
