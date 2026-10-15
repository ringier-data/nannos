"""Tool retry with a failure path for every error, not only exhausted retries.

langchain >= 1.3.14 changed ``ToolRetryMiddleware``: an exception its ``retry_on`` declines
is re-raised at once and never reaches ``on_failure``. ``ToolNode``'s default error handler
re-raises anything that is not a ``ToolInvocationError``, so a non-transient tool error (a
``ToolException``, a 4xx, anything ``retry_on`` does not classify as transient) ends the
turn instead of reaching the model as "Tool call failed: ...". Our ``retry_on`` predicates
decline exactly those errors on purpose: they are not worth retrying, but the model must
still see them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import ToolRetryMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command


class FailureHandlingToolRetryMiddleware(ToolRetryMiddleware):
    """``ToolRetryMiddleware`` whose ``on_failure`` also handles errors it does not retry.

    Transient errors are retried as upstream does; an error ``retry_on`` declines goes to
    ``on_failure`` after one attempt, the contract before langchain 1.3.14. Control-flow
    signals (interrupts, parent commands) always propagate, and ``on_failure="error"``
    still re-raises.
    """

    def _tool_name(self, request: ToolCallRequest) -> str:
        return request.tool.name if request.tool else request.tool_call["name"]

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        try:
            return super().wrap_tool_call(request, handler)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            if not self._should_retry_tool(self._tool_name(request)):
                raise
            return self._handle_failure(self._tool_name(request), request.tool_call["id"], exc, 1)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        try:
            return await super().awrap_tool_call(request, handler)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            if not self._should_retry_tool(self._tool_name(request)):
                raise
            return self._handle_failure(self._tool_name(request), request.tool_call["id"], exc, 1)
