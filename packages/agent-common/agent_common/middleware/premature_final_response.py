"""Hold back a final response the model sent together with tool calls.

With a tool-strategy response format the model ends its turn by calling the response
schema "tool". Nothing stops it from doing that in the SAME message as real tool
calls — and langchain accepts it: the structured response is set right away, the
real calls run, and the tools→model edge then ends the run because "a structured
output tool was executed". The model never reads the results of its own calls. In
practice that is a model announcing "I filled and saved it" next to the very call
that would fill it, and the turn ending before the save it promised: the user is
told something that did not happen.

This middleware drops the premature response from such a message, keeping the real
calls: they run, the model reads their results, and it answers again — alone, the
way the response format expects.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)


def hold_back_premature_response(response: ModelResponse) -> ModelResponse:
    """The response without a structured answer that shares its message with tool calls.

    Langchain answers each structured-output call with an artificial ToolMessage in the
    same response; those ids identify the structured calls, whatever the schema's name.
    """
    if response.structured_response is None:
        return response
    ai = next((m for m in response.result if isinstance(m, AIMessage)), None)
    if ai is None or not ai.tool_calls:
        return response
    structured_ids = {m.tool_call_id for m in response.result if isinstance(m, ToolMessage)}
    real_calls = [c for c in ai.tool_calls if c.get("id") not in structured_ids]
    if not real_calls or len(real_calls) == len(ai.tool_calls):
        return response

    content = ai.content
    if isinstance(content, list):
        # Provider-native tool_use blocks carry the same calls; a dropped call must not
        # survive in the content the provider sees on the next request.
        content = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") in structured_ids)
        ]
    held_back = ai.model_copy(update={"tool_calls": real_calls, "content": content})
    logger.info(
        "Model sent its final response together with %d tool call(s); holding the response back "
        "until it has read their results",
        len(real_calls),
    )
    return ModelResponse(
        result=[
            held_back if m is ai else m
            for m in response.result
            if not (isinstance(m, ToolMessage) and m.tool_call_id in structured_ids)
        ],
        structured_response=None,
    )


class PrematureFinalResponseMiddleware(AgentMiddleware):
    """See the module docstring."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return hold_back_premature_response(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return hold_back_premature_response(await handler(request))
