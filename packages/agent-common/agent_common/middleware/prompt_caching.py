"""Gateway-aware prompt caching middleware.

All LLM traffic is routed through the LiteLLM gateway as an OpenAI-compatible
``ChatOpenAI`` client, so the provider-specific caching middlewares
shipped by ``langchain-aws`` / ``langchain-anthropic`` never fire here: their
guards reject any client that is not ``ChatBedrockConverse`` / ``ChatAnthropic``,
and they hand the breakpoint to the native client via ``model_settings`` — a
path ``ChatOpenAI`` ignores.

This middleware instead writes the breakpoint where LiteLLM actually reads it:
an OpenAI-format ``cache_control`` marker on the **last content block of the
system message**. ``langchain_openai`` serializes unknown content-block keys
verbatim (``_format_message_content`` passes plain text blocks through), so the
marker reaches the proxy intact; LiteLLM then translates it to the active
provider's native format (Anthropic ``ephemeral`` / Bedrock ``cachePoint`` /
Gemini) and silently drops it for providers that don't cache. No provider gate
is therefore needed — wrong-provider requests just carry an ignored field.

Placement matters: install this immediately after the middleware that finishes
the *static* system prefix (e.g. storage-paths) and before any middleware that
appends *per-request / per-user* content (user preferences, playbooks). The
breakpoint lands at the end of the cacheable prefix; volatile content appended
afterwards stays outside the cache, preserving the prefix match across turns.

The marker is applied to the per-call ``ModelRequest`` only (via
``request.override``) — it never touches the persisted messages, so
``cache_control`` does not accumulate in the checkpoint across turns.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.messages import SystemMessage

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)

logger = logging.getLogger(__name__)


class LiteLLMPromptCachingMiddleware(AgentMiddleware):
    """Inject an Anthropic-style ``cache_control`` breakpoint on the system prefix.

    Modeled on ``langchain_anthropic``'s ``AnthropicPromptCachingMiddleware`` (same
    last-block tagging, same str/list/empty edge-case handling) but provider-agnostic
    at the app layer: it tags message *content* (which round-trips through
    ``ChatOpenAI`` → LiteLLM) rather than routing through ``model_settings`` (which
    only the native Bedrock/Anthropic clients understand).
    """

    def __init__(
        self,
        *,
        ttl: Literal["5m", "1h"] = "5m",
        min_messages_to_cache: int = 0,
    ) -> None:
        """Initialize the middleware.

        Args:
            ttl: Cache time-to-live. ``"5m"`` is the provider default and is sent as a
                bare ``{"type": "ephemeral"}`` marker; ``"1h"`` adds the explicit
                ``ttl`` field (note: Bedrock cachePoint ignores TTL).
            min_messages_to_cache: Minimum message count (system message included)
                before a breakpoint is injected. ``0`` always caches.
        """
        self.ttl = ttl
        self.min_messages_to_cache = min_messages_to_cache

    @property
    def _cache_control(self) -> dict[str, str]:
        # Default 5m TTL is implicit on Anthropic — emit the bare marker the LiteLLM
        # docs show; only spell out a non-default TTL.
        cache_control = {"type": "ephemeral"}
        if self.ttl != "5m":
            cache_control["ttl"] = self.ttl
        return cache_control

    def _should_apply_caching(self, request: ModelRequest) -> bool:
        if request.system_message is None:
            return False
        # +1 for the system message itself, matching the vendored middlewares.
        return len(request.messages) + 1 >= self.min_messages_to_cache

    def _apply_caching(self, request: ModelRequest) -> ModelRequest:
        tagged = _tag_system_message(request.system_message, self._cache_control)
        if tagged is request.system_message:
            return request  # nothing to tag (empty/None/unrecognized) or already tagged
        logger.debug("Injected cache_control breakpoint on system prefix (ttl=%s)", self.ttl)
        return request.override(system_message=tagged)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if not self._should_apply_caching(request):
            return handler(request)
        return handler(self._apply_caching(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if not self._should_apply_caching(request):
            return await handler(request)
        return await handler(self._apply_caching(request))


def _tag_system_message(system_message: Any, cache_control: dict[str, str]) -> Any:
    """Tag the last content block of a system message with ``cache_control``.

    Returns the original ``system_message`` unchanged when there is nothing to tag
    (None, empty content, an already-identical marker, or an unrecognized content
    shape) so callers can cheaply detect a no-op by identity.
    """
    if system_message is None:
        return system_message

    content = system_message.content
    if isinstance(content, str):
        if not content:
            return system_message
        new_content: list[Any] = [
            {"type": "text", "text": content, "cache_control": cache_control}
        ]
    elif isinstance(content, list):
        if not content:
            return system_message
        last = content[-1]
        if isinstance(last, dict):
            if last.get("cache_control") == cache_control:
                return system_message  # idempotent: already tagged this turn
            new_content = [*content[:-1], {**last, "cache_control": cache_control}]
        elif isinstance(last, str):
            new_content = [
                *content[:-1],
                {"type": "text", "text": last, "cache_control": cache_control},
            ]
        else:
            return system_message  # unrecognized block shape — leave untouched
    else:
        return system_message

    return SystemMessage(content=new_content)
