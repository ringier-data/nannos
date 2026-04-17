"""Shared steering state for the orchestrator.

This module holds process-wide state used by both the executor (writer) and
the SteeringMiddleware / graph_factory (reader).  It is intentionally kept
free of heavy imports so that any module can import it without triggering
circular dependency chains.

State managed here:
- ``_orchestrator_steering_queues``: per-context_id queues populated by the
  executor when follow-up messages arrive while an agent stream is active.
- ``_active_subagent_dispatches``: per-context_id tracking of which sub-agent
  is currently being streamed, set/cleared by DynamicToolDispatchMiddleware.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from a2a.types import Message

# ---------------------------------------------------------------------------
# Orchestrator steering queues
# ---------------------------------------------------------------------------

# Shared between the executor (which populates them) and SteeringMiddleware
# (which drains them).  Keyed by context_id.  Separate from the SDK's
# _active_streams because OrchestratorDeepAgentExecutor inherits directly
# from AgentExecutor (not BaseAgentExecutor) — it needs orchestrator-specific
# steering with A2A extensions, budget guards, and post-completion re-invocation.
_orchestrator_steering_queues: dict[str, asyncio.Queue[Message]] = {}


def get_orchestrator_pending_messages(context_id: str) -> list[Message]:
    """Drain pending steering messages for the orchestrator (non-blocking).

    Called by SteeringMiddleware in the orchestrator's middleware stack.
    """
    queue = _orchestrator_steering_queues.get(context_id)
    if queue is None or queue.empty():
        return []
    messages: list[Message] = []
    while not queue.empty():
        try:
            messages.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return messages


def get_steering_queue(context_id: str) -> asyncio.Queue[Message] | None:
    """Return the steering queue for *context_id*, or ``None``."""
    return _orchestrator_steering_queues.get(context_id)


def register_steering_queue(context_id: str, queue: asyncio.Queue[Message]) -> None:
    """Register a new steering queue for *context_id*."""
    _orchestrator_steering_queues[context_id] = queue


def remove_steering_queue(context_id: str) -> None:
    """Remove and discard the steering queue for *context_id*."""
    _orchestrator_steering_queues.pop(context_id, None)


# ---------------------------------------------------------------------------
# Active sub-agent dispatch tracking
# ---------------------------------------------------------------------------


@dataclass
class ActiveSubagentDispatch:
    """Tracks a sub-agent dispatch that is currently in-progress.

    Used by the orchestrator's SteeringMiddleware to forward user follow-up
    messages to in-progress sub-agents.
    """

    subagent_name: str
    runnable: Any  # BaseA2ARunnable (remote or local)
    orchestrator_context_id: str
    subagent_context_id: str | None = None
    subagent_task_id: str | None = None


# Active sub-agent dispatches keyed by orchestrator context_id.
# Multiple sub-agents may run in parallel (LangGraph ToolNode uses asyncio.gather),
# so we store a list of dispatches per context_id.
# Set before astream_a2a_agent, cleared per-dispatch in finally.
_active_subagent_dispatches: dict[str, list[ActiveSubagentDispatch]] = {}


def get_all_active_subagent_dispatches(context_id: str) -> list[ActiveSubagentDispatch]:
    """Return all active sub-agent dispatches for a context_id.

    Used by the cancel flow to propagate cancellation to every in-flight
    sub-agent, not just the most recent one.
    """
    return list(_active_subagent_dispatches.get(context_id, []))


def set_active_subagent_dispatch(context_id: str, dispatch: ActiveSubagentDispatch) -> None:
    """Register *dispatch* as an active sub-agent for *context_id*."""
    _active_subagent_dispatches.setdefault(context_id, []).append(dispatch)


def clear_active_subagent_dispatch(context_id: str, dispatch: ActiveSubagentDispatch | None = None) -> None:
    """Remove a specific dispatch (or all dispatches) for *context_id*.

    When *dispatch* is provided, only that entry is removed (used by the
    per-tool-call finally block).  When ``None``, all entries are removed.
    """
    if dispatch is None:
        _active_subagent_dispatches.pop(context_id, None)
        return
    dispatches = _active_subagent_dispatches.get(context_id)
    if dispatches is None:
        return
    try:
        dispatches.remove(dispatch)
    except ValueError:
        pass
    if not dispatches:
        _active_subagent_dispatches.pop(context_id, None)
