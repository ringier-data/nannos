"""Strip plain-text content from AIMessages that carry a FinalResponseSchema tool call.

The orchestrator's contract is that the user-visible final answer comes
exclusively from the ``FinalResponseSchema`` tool call's ``message`` field
(see ``StreamHandler.parse_agent_response``). Models occasionally emit a
plain-text preamble — often a full duplicate of the answer — alongside the
tool call. That text is never used for the final response, but if it stays
in the checkpointed history the model imitates its own pattern on subsequent
turns and the duplication compounds.

This middleware deterministically drops text blocks from any AIMessage that
contains a FinalResponseSchema tool call, keeping the persisted history
aligned with the contract. Thinking/reasoning blocks are preserved: Bedrock
extended thinking requires them intact when the history is replayed.

Note: this cannot un-send text that was already token-streamed to the client;
the streaming loop in ``agent.py`` routes plain text to the thinking channel
for that. The two fixes are complementary.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

# Only FinalResponseSchema: this middleware is installed in the orchestrator
# graph only; sub-agent graphs (which bind SubAgentResponseSchema) build their
# own middleware stacks and never run it.
_RESPONSE_SCHEMA_NAME = "FinalResponseSchema"


def _is_text_block(block: Any) -> bool:
    """Whether a content block is a plain-text block with actual content."""
    if isinstance(block, str):
        return bool(block.strip())
    if isinstance(block, dict) and block.get("type") == "text":
        return bool(str(block.get("text", "")).strip())
    return False


def _strip_text_content(message: AIMessage) -> bool:
    """Remove plain-text content from a message carrying a response-schema tool call.

    Returns True when the message was modified. Thinking/reasoning and
    tool_use blocks are left untouched.
    """
    tool_calls = getattr(message, "tool_calls", None) or []
    if not any(tc.get("name") == _RESPONSE_SCHEMA_NAME for tc in tool_calls):
        return False

    content = message.content
    if isinstance(content, str):
        if content.strip():
            message.content = ""
            return True
        return False

    if isinstance(content, list):
        kept = [block for block in content if not _is_text_block(block)]
        if len(kept) != len(content):
            message.content = kept
            return True

    return False


class FinalResponseTextStripMiddleware(AgentMiddleware[AgentState, Any]):
    """Drops text blocks from AIMessages that also call FinalResponseSchema."""

    state_schema = AgentState
    tools: list[Any] = []

    def _process(self, result: ModelCallResult) -> ModelCallResult:
        if isinstance(result, AIMessage):
            messages: list[Any] = [result]
        elif isinstance(result, ModelResponse) or hasattr(result, "result"):
            messages = result.result
        else:
            return result

        for msg in messages:
            if isinstance(msg, AIMessage) and _strip_text_content(msg):
                logger.info(
                    "[FINAL RESPONSE STRIP] Dropped plain-text content from AIMessage "
                    "carrying a response-schema tool call (id=%s)",
                    getattr(msg, "id", None),
                )
        return result

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return self._process(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return self._process(await handler(request))
