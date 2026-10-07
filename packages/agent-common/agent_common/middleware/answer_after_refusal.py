"""After a refused or blocked call, the model's next step can only be its answer.

A refusal (the user clicked Reject or said no, or an identical retry of a refused call
was answered ``NOT RUN``) and a loop block (``BLOCKED: …``) both tell the model to stop
and tell the user. A model that must call a tool on every step — the forced
``tool_choice`` of the ``ToolStrategy`` path (Haiku, GPT) — cannot do that by writing
text, and in QA did not pick the response tool either: it re-sent the refused save six
times, or kept reading the page, until loop detection force-stopped the run with no
answer at all. Offering only the response tool on that step leaves the answer as the
one possible move, whatever the model or strategy:

- ``ToolStrategy``: the request keeps no tools of its own, and langchain binds the
  structured-output tool alone, still forced.
- Bind-as-tool (``FinalResponseSchema`` / ``SubAgentResponseSchema`` among the tools):
  only that tool is kept.

It must sit innermost: the orchestrator injects its tool registry in an outer
``wrap_model_call``, and an outer filter would be overridden.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from agent_common.core.hitl_resume import REFUSAL_LEADS

logger = logging.getLogger(__name__)

#: The response tools a turn ends with, in either structured-output strategy.
RESPONSE_TOOL_NAMES: frozenset[str] = frozenset({"FinalResponseSchema", "SubAgentResponseSchema"})

#: How a refused or blocked call's result starts. ``NOT RUN: the user rejected`` is the
#: refused-retry answer (conditional_hitl ``_REFUSED_AGAIN``); other ``NOT RUN`` results
#: (a save sent next to other calls) ask for a retry and are deliberately not here.
STOP_LEADS: tuple[str, ...] = (*REFUSAL_LEADS, "NOT RUN: the user rejected", "BLOCKED: ")


def must_answer(messages: Sequence[BaseMessage]) -> bool:
    """Whether the last step's results include a refused or blocked call."""
    last_ai = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)), None)
    if last_ai is None:
        return False
    return any(
        isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content.startswith(STOP_LEADS)
        for m in messages[last_ai + 1 :]
    )


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        return (tool.get("function") or {}).get("name") or tool.get("name")
    return getattr(tool, "name", None)


class AnswerAfterRefusalMiddleware(AgentMiddleware):
    """See the module docstring."""

    def _narrowed(self, request: ModelRequest) -> ModelRequest:
        if not must_answer(request.messages):
            return request
        kept = [tool for tool in request.tools if _tool_name(tool) in RESPONSE_TOOL_NAMES]
        logger.info(
            "A call was refused or blocked; offering only the response tool on this step (%d of %d tools kept)",
            len(kept),
            len(request.tools),
        )
        return request.override(tools=kept)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._narrowed(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._narrowed(request))
