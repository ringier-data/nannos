"""Prompt caching middleware for AWS Bedrock Converse API.

Adds cachePoint markers to system messages so Bedrock caches the system prompt
prefix across turns, reducing input token costs on multi-turn conversations.

See: https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
"""

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelCallResult, ModelRequest, ModelResponse
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import SystemMessage

logger = logging.getLogger(__name__)


class BedrockPromptCachingMiddleware(AgentMiddleware):
    """Adds Bedrock cachePoint markers to system messages.

    Works with ChatBedrockConverse models only. For non-Bedrock models,
    the middleware passes through without modification.
    """

    def __init__(self, unsupported_model_behavior: str = "ignore") -> None:
        self.unsupported_model_behavior = unsupported_model_behavior

    def _should_apply(self, request: ModelRequest) -> bool:
        if isinstance(request.model, ChatBedrockConverse):
            return True
        if self.unsupported_model_behavior == "warn":
            logger.warning(
                "BedrockPromptCachingMiddleware only supports ChatBedrockConverse, "
                f"got {type(request.model).__name__}"
            )
        return False

    def _add_cache_point(self, request: ModelRequest) -> ModelRequest:
        """Add a cachePoint block to the end of the system message content."""
        system_msg = request.system_message
        if system_msg is None:
            return request

        cache_point = {"cachePoint": {"type": "default"}}

        # SystemMessage.content can be a string or list of content blocks
        if isinstance(system_msg.content, str):
            new_content = [{"type": "text", "text": system_msg.content}, cache_point]
        elif isinstance(system_msg.content, list):
            # Already structured content blocks — append cache point if not already present
            if any(isinstance(block, dict) and "cachePoint" in block for block in system_msg.content):
                return request
            new_content = list(system_msg.content) + [cache_point]
        else:
            return request

        new_system = SystemMessage(content=new_content)
        return request.override(system_message=new_system)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if not self._should_apply(request):
            return handler(request)
        return handler(self._add_cache_point(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if not self._should_apply(request):
            return await handler(request)
        return await handler(self._add_cache_point(request))
