"""Prompt caching middleware for AWS Bedrock Converse API.

Adds cachePoint markers to system messages and conversation history so Bedrock
caches the prompt prefix across turns, reducing input token costs.

See: https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
"""

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelCallResult, ModelRequest, ModelResponse
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)

CACHE_POINT = {"cachePoint": {"type": "default"}}


def _has_cache_point(content: list) -> bool:
    """Check if content blocks already contain a cachePoint."""
    return any(isinstance(block, dict) and "cachePoint" in block for block in content)


def _append_cache_point(content: str | list) -> list | None:
    """Append a cachePoint block to message content.

    Returns the new content list, or None if no modification was needed.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}, CACHE_POINT]
    if isinstance(content, list):
        if _has_cache_point(content):
            return None
        return list(content) + [CACHE_POINT]
    return None


class BedrockPromptCachingMiddleware(AgentMiddleware):
    """Adds Bedrock cachePoint markers to system messages, conversation history, and tools.

    Places up to 3 cache points per request:

    1. End of the system message — caches the system prompt prefix.
    2. Second-to-last conversation message — caches the conversation history
       prefix (everything before the latest user message).
    3. End of the tools list — caches tool definitions.

    Works with ChatBedrockConverse models only. For non-Bedrock models,
    the middleware passes through without modification.
    """

    def __init__(
        self,
        unsupported_model_behavior: str = "ignore",
        cache_system_prompt: bool = True,
        cache_conversation: bool = True,
        cache_tools: bool = True,
        min_messages: int = 2,
    ) -> None:
        self.unsupported_model_behavior = unsupported_model_behavior
        self.cache_system_prompt = cache_system_prompt
        self.cache_conversation = cache_conversation
        self.cache_tools = cache_tools
        self.min_messages = min_messages

    def _should_apply(self, request: ModelRequest) -> bool:
        if isinstance(request.model, ChatBedrockConverse):
            return True
        if self.unsupported_model_behavior == "warn":
            logger.warning(
                "BedrockPromptCachingMiddleware only supports ChatBedrockConverse, "
                f"got {type(request.model).__name__}"
            )
        return False

    def _add_system_cache_point(self, request: ModelRequest) -> ModelRequest:
        """Add a cachePoint block to the end of the system message content."""
        system_msg = request.system_message
        if system_msg is None:
            return request

        new_content = _append_cache_point(system_msg.content)
        if new_content is None:
            return request

        return request.override(system_message=SystemMessage(content=new_content))

    def _add_conversation_cache_point(self, request: ModelRequest) -> ModelRequest:
        """Add a cachePoint to the last message before the current user message.

        This caches the conversation history prefix so only new messages
        need to be processed on subsequent turns.
        """
        messages = request.messages
        if len(messages) < self.min_messages:
            return request

        # Cache point goes on the second-to-last message
        # (the one right before the latest user query)
        target_idx = len(messages) - 2
        target_msg = messages[target_idx]

        new_content = _append_cache_point(target_msg.content)
        if new_content is None:
            return request

        new_msg = target_msg.model_copy(update={"content": new_content})
        new_messages = list(messages)
        new_messages[target_idx] = new_msg
        return request.override(messages=new_messages)

    def _add_tools_cache_point(self, request: ModelRequest) -> ModelRequest:
        """Add a cachePoint to the end of the tools list.

        This caches tool definitions so they don't need to be re-processed
        on every model call within an agentic loop.
        """
        tools = request.tools
        if not tools:
            return request

        if any(isinstance(t, dict) and "cachePoint" in t for t in tools):
            return request

        return request.override(tools=list(tools) + [CACHE_POINT])

    def _apply(self, request: ModelRequest) -> ModelRequest:
        """Apply all cache points to the request."""
        if self.cache_system_prompt:
            request = self._add_system_cache_point(request)
        if self.cache_conversation:
            request = self._add_conversation_cache_point(request)
        if self.cache_tools:
            request = self._add_tools_cache_point(request)
        return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if not self._should_apply(request):
            return handler(request)
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if not self._should_apply(request):
            return await handler(request)
        return await handler(self._apply(request))
