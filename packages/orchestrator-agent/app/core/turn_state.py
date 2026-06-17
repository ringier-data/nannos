"""Per-turn state carrier shared between the orchestrator agent and its executor.

Created fresh inside ``OrchestratorDeepAgentExecutor.execute()`` for each stream
round and passed into ``OrchestratorDeepAgent.stream()``. The agent already reads
the final graph state once at end-of-stream (``graph.aget_state``); it stores that
result here so the executor can reuse it for the phantom / feedback / terminal
checks WITHOUT issuing its own ``graph.aget_state()`` re-reads — each of which is a
~5s checkpoint fetch (PostgreSQL + optional S3 offload). Nothing mutates the graph between
the agent's end-of-stream read and the executor's post-stream checks, so the
carrier IS what a re-read would return — equivalence by construction.

The executor and agent are module-level singletons shared across concurrent
requests, so this carrier MUST be a per-``execute()`` local — never stored on
``self``.

This replaced four redundant ``get_state()`` re-reads per completed turn (the
executor's phantom / feedback / terminal checks plus a per-status-item read).
Equivalence was confirmed via a shadow phase that logged carrier-vs-re-read
agreement across happy-path / auth / HITL / phantom / feedback turns before the
reads were removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TurnState:
    """Final graph state captured from the stream for one orchestrator turn."""

    # ``StateSnapshot.values`` from the agent's end-of-stream ``aget_state`` —
    # a dict with "messages". Same object the executor would otherwise re-read.
    final_values: dict[str, Any] | None = None
    # ``StateSnapshot.interrupts`` from that same read.
    interrupts: tuple = ()
    # True once the agent has populated this carrier from its end-of-stream read.
    captured: bool = False

    @property
    def has_interrupts(self) -> bool:
        return bool(self.interrupts)


def count_tool_messages(values: Any) -> int:
    """Count ToolMessages in state values (feedback-threshold input at executor.py:724)."""
    if not isinstance(values, dict):
        return 0
    return sum(1 for m in (values.get("messages") or []) if hasattr(m, "tool_call_id"))
