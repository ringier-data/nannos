"""``structured_response`` belongs to the turn that wrote it.

langchain's routing ends a run as soon as ``structured_response`` holds a value, in every
graph, but clears the channel on each model call only in graphs built with a
``response_format``. A graph with the response schema bound as a plain tool (thinking on,
Gemini, ``forced_tool_choice=false`` aliases) never writes it, so the only value it can hold
is one an earlier turn left on the same thread under another model. Read at that point, it
ended runs before the model saw its tool results. Clearing it when a turn starts makes the
channel per-turn on every path, so the routing needs no workaround downstream.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState


class TurnScopedResponseMiddleware(AgentMiddleware):
    """Clear ``structured_response`` at the start of each turn (``before_agent``)."""

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return {"structured_response": None} if state.get("structured_response") is not None else None

    async def abefore_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)
