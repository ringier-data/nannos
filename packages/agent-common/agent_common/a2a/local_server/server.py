"""The request-handling side of the in-process server, and the shared task store."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable, Optional

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.server.agent_execution.active_task import ActiveTask
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    SendMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskNotCancelableError,
    TaskStatusUpdateEvent,
    TaskNotFoundError,
)

from ..extensions import ALL_EXTENSIONS
from .context_builder import ProposedTaskIdContextBuilder
from .executor import PARENT_CONFIG_KEY, LocalSubAgentExecutor

if TYPE_CHECKING:
    from ..base import LocalA2ARunnable

logger = logging.getLogger(__name__)

#: The transport label of the in-process interface. A2A keeps this a string so
#: closed ecosystems can name their own transports; nothing dials it.
IN_PROCESS_PROTOCOL_BINDING = "INPROCESS"

#: How long ``release`` waits for a task's producer and consumer to drain after
#: their request queue is shut, before cancelling them outright.
_SHUTDOWN_TIMEOUT_S = 5.0

_shared_task_store: TaskStore | None = None
#: The default in-memory store and the event loop it was created under. An
#: ``InMemoryTaskStore`` holds an ``asyncio.Lock``, which binds to the first loop
#: that uses it; a process that runs several loops in turn (a test suite) needs a
#: fresh default per loop, while an INSTALLED store is the host's business.
_default_task_store: tuple[asyncio.AbstractEventLoop, TaskStore] | None = None


def set_local_task_store(store: TaskStore) -> None:
    """Install the task store every in-process server in this process uses.

    The orchestrator calls this at startup with the same (persistent) store its
    HTTP request handler uses, so a local sub-agent's tasks are stored and
    survive a restart exactly like the orchestrator's own.
    """
    global _shared_task_store
    _shared_task_store = store


def get_local_task_store() -> TaskStore:
    """The installed store, or an in-memory default bound to the running loop."""
    global _default_task_store
    if _shared_task_store is not None:
        return _shared_task_store
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _default_task_store is None or (loop is not None and _default_task_store[0] is not loop):
        logger.info("No local A2A task store installed; using an in-memory store")
        _default_task_store = (loop, InMemoryTaskStore())  # type: ignore[assignment]
    return _default_task_store[1]


class LocalA2AServer:
    """One local sub-agent, served through the A2A SDK's request handler in-process.

    Owns the ``DefaultRequestHandler`` (executor + task store) and the
    per-conversation lock the executor takes around a graph run, so two messages
    for one conversation never write the same checkpoint thread at once.

    The handler keeps a live ``ActiveTask`` (two asyncio tasks and their event
    queues) per task it has touched, in a process-local registry. That object is
    a cache, never the truth: the task record lives in the task store and the
    graph's position in its checkpoint, and the executor rebuilds a resume from
    those alone. So a task that parks on a question is *released* here as soon
    as its stream ends (``release``), exactly as if the follow-up were to land on
    another replica, instead of idling until garbage collection finalises it off
    the event loop.
    """

    def __init__(self, runnable: "LocalA2ARunnable", task_store: Optional[TaskStore] = None) -> None:
        self._runnable = runnable
        self._task_store = task_store or get_local_task_store()
        self._locks: dict[str, asyncio.Lock] = {}
        self.agent_card = AgentCard(
            name=runnable.name,
            description=(runnable.description or "")[:1000],
            version="in-process",
            capabilities=AgentCapabilities(streaming=True, extensions=[]),
            supported_interfaces=[
                AgentInterface(url=f"inprocess://{runnable.tracking_key}", protocol_binding=IN_PROCESS_PROTOCOL_BINDING)
            ],
        )
        self._handler = DefaultRequestHandler(
            agent_executor=LocalSubAgentExecutor(runnable),
            task_store=self._task_store,
            agent_card=self.agent_card,
            request_context_builder=ProposedTaskIdContextBuilder(task_store=self._task_store),
        )

    @property
    def task_store(self) -> TaskStore:
        return self._task_store

    def thread_lock(self, context_id: str) -> asyncio.Lock:
        """The lock serialising graph runs on ``context_id``'s conversation thread."""
        lock = self._locks.get(context_id)
        if lock is None:
            lock = self._locks[context_id] = asyncio.Lock()
        return lock

    async def send(
        self,
        message: Message,
        *,
        parent_config: dict[str, Any],
        requested_extensions: Optional[Iterable[str]] = None,
    ) -> AsyncIterator[Any]:
        """``message/stream`` in-process: yields the SDK's events as they are produced.

        The caller's ``RunnableConfig`` rides the call context so the graph runs
        with the caller's callbacks, tags and checkpointer. Every extension is
        negotiated by default: the consumer is our own translator, which
        understands all of them.

        However the stream ends — the executor returned, the consumer stopped
        early, an error — the SDK's subscription is closed (so its reference
        count drops and a finished task leaves the registry) and the task's
        ``ActiveTask`` is released. Nothing about the task is lost: a later
        message to it goes through the task store and the checkpoint.
        """
        call_context = ServerCallContext(
            state={PARENT_CONFIG_KEY: parent_config},
            requested_extensions=set(requested_extensions) if requested_extensions is not None else set(ALL_EXTENSIONS),
        )
        task_id: str | None = message.task_id or None
        stream = self._handler.on_message_send_stream(SendMessageRequest(message=message), call_context)
        try:
            async with aclosing(stream) as events:
                async for event in events:
                    if isinstance(event, Task):
                        task_id = event.id
                    elif isinstance(event, (TaskStatusUpdateEvent, TaskArtifactUpdateEvent)):
                        task_id = event.task_id
                    yield event
        finally:
            if task_id is not None:
                await self.release(task_id)

    async def release(self, task_id: str) -> None:
        """Drop the live ``ActiveTask`` for ``task_id``; the stored task is untouched.

        Shuts the request queue the producer waits on, which winds the producer
        and consumer down through the SDK's own ``finally`` blocks and prunes the
        registry entry. A request the producer is executing right now finishes
        first; only a request queued behind it would be dropped, and nothing in
        the in-process client queues one. Idempotent; a task with no live object
        is a no-op.

        Call it once the task's streams are closed — ``send`` does. The SDK's
        wind-down joins every subscriber's queue, and a subscriber generator
        suspended on an event it has not acknowledged holds that join until the
        timeout cancels the task outright.
        """
        registry = self._handler._active_task_registry
        active = await registry.get(task_id)
        if active is None:
            return
        await _wind_down(active)
        await registry._remove_task(task_id)

    async def aclose(self) -> None:
        """Release every live task, cancelling in-flight runs. For a host tearing the server down.

        Streams the host still holds open are cut off rather than waited for;
        their tasks stay in the store in whatever state was last written.
        """
        registry = self._handler._active_task_registry
        async with registry._lock:
            active_tasks = list(registry._active_tasks.values())
        for active in active_tasks:
            await _cancel(active)
            await registry._remove_task(active.task_id)

    async def get_task(self, task_id: str) -> Task | None:
        """``tasks/get`` in-process; ``None`` for an unknown id."""
        try:
            return await self._handler.on_get_task(GetTaskRequest(id=task_id), ServerCallContext())
        except TaskNotFoundError:
            return None

    async def cancel(self, task_id: str) -> Task | None:
        """``tasks/cancel`` in-process; ``None`` when there is nothing to cancel."""
        try:
            return await self._handler.on_cancel_task(CancelTaskRequest(id=task_id), ServerCallContext())
        except (TaskNotFoundError, TaskNotCancelableError):
            return None


async def _wind_down(active: ActiveTask) -> None:
    """Wind an ``ActiveTask`` down through the SDK's own paths and wait for it.

    The SDK exposes no close for a task that is parked on a question — its
    producer blocks on ``_request_queue.get()`` until a follow-up arrives or the
    object is garbage-collected. Shutting that queue makes ``get`` raise
    ``QueueShutDown``, which the producer's own ``finally`` turns into closing
    the agent queue, the consumer draining out and cleanup firing. Waiting for
    the two background tasks here is what keeps their finalisation on the loop.
    """
    active._request_queue.shutdown(immediate=True)
    pending = _background_tasks(active)
    if not pending:
        return
    done, still_pending = await asyncio.wait(pending, timeout=_SHUTDOWN_TIMEOUT_S)
    for t in still_pending:
        where = "; ".join(f"{f.f_code.co_name}:{f.f_lineno}" for f in t.get_stack(limit=4))
        logger.warning(
            f"ActiveTask {active.task_id}: {t.get_name()} did not wind down in {_SHUTDOWN_TIMEOUT_S}s "
            f"(at {where}); cancelling"
        )
    if still_pending:
        await _cancel_all(still_pending)
    for t in done:
        if not t.cancelled() and (exc := t.exception()) is not None:
            logger.debug(f"ActiveTask {active.task_id}: {t.get_name()} ended with {exc!r}")


async def _cancel(active: ActiveTask) -> None:
    """Stop an ``ActiveTask`` now: cancel its producer and consumer and wait for them."""
    active._request_queue.shutdown(immediate=True)
    await active._event_queue_subscribers.close(immediate=True)
    await _cancel_all(_background_tasks(active))


def _background_tasks(active: ActiveTask) -> list[asyncio.Task[None]]:
    return [t for t in (active._producer_task, active._consumer_task) if t is not None and not t.done()]


async def _cancel_all(tasks: Iterable[asyncio.Task[None]]) -> None:
    tasks = list(tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
