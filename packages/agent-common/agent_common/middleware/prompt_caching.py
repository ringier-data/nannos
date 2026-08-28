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

In addition to the system prefix, this middleware places a **second breakpoint on
the last message of the conversation** (``cache_conversation``, on by default).
The system breakpoint alone only caches the static prefix (~a few thousand tokens),
leaving the growing conversation history — frequently 10k–20k tokens/turn on this
orchestrator — reprocessed uncached on every turn. The conversation breakpoint
caches that history incrementally: each turn reads the prior turn's cached prefix
and writes only the delta, which is the dominant time-to-first-token lever (warm
TTFT tracks cache coverage almost linearly — ~50% cached ≈ 5s, ~90% cached ≈ 2s).
This is safe because ``request.messages`` is an append-only prefix here: the
post-cache middlewares (user-preferences, playbook) append to the *system message*,
and steering appends persisted ``HumanMessage``s — none rewrite earlier history, so
the cached prefix stays byte-identical across turns. Because this middleware runs
before steering, the conversation breakpoint lands on stable pre-steering history;
anything appended afterwards simply stays outside the cache and is cached next turn.

The markers are applied to the per-call ``ModelRequest`` only (via
``request.override``) — they never touch the persisted messages, so
``cache_control`` does not accumulate in the checkpoint across turns.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.messages import SystemMessage

from .utils import VOLATILE_CONTEXT_KEY

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
        cache_conversation: bool = True,
    ) -> None:
        """Initialize the middleware.

        Args:
            ttl: Cache time-to-live. ``"5m"`` is the provider default and is sent as a
                bare ``{"type": "ephemeral"}`` marker; ``"1h"`` adds the explicit
                ``ttl`` field (note: Bedrock cachePoint ignores TTL).
            min_messages_to_cache: Minimum message count (system message included)
                before a breakpoint is injected. ``0`` always caches.
            cache_conversation: When ``True`` (default), also tag the last message of
                the conversation so the append-only history is cached incrementally,
                not just the static system prefix. Providers that don't support
                multiple cache points silently ignore the extra marker.
        """
        self.ttl = ttl
        self.min_messages_to_cache = min_messages_to_cache
        self.cache_conversation = cache_conversation

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
        new_request = request

        tagged_system = _tag_system_message(request.system_message, self._cache_control)
        if tagged_system is not request.system_message:
            new_request = new_request.override(system_message=tagged_system)
            logger.debug("Injected cache_control breakpoint on system prefix (ttl=%s)", self.ttl)

        if self.cache_conversation and request.messages:
            tagged_messages = _tag_last_message(request.messages, self._cache_control)
            if tagged_messages is not request.messages:
                new_request = new_request.override(messages=tagged_messages)
                logger.debug("Injected cache_control breakpoint on last conversation message")

        return new_request

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


def _tag_last_block(content: Any, cache_control: dict[str, str]) -> list[Any] | None:
    """Return new message content with ``cache_control`` on its last block.

    Returns ``None`` (a no-op signal) when there is nothing to tag: empty content,
    an already-identical marker on the last block, or an unrecognized content shape.
    A plain-string content is normalized to a single text block carrying the marker.
    """
    if isinstance(content, str):
        if not content:
            return None
        return [{"type": "text", "text": content, "cache_control": cache_control}]
    if isinstance(content, list):
        if not content:
            return None
        last = content[-1]
        if isinstance(last, dict):
            if last.get("cache_control") == cache_control:
                return None  # idempotent: already tagged this turn
            return [*content[:-1], {**last, "cache_control": cache_control}]
        if isinstance(last, str):
            return [
                *content[:-1],
                {"type": "text", "text": last, "cache_control": cache_control},
            ]
    return None  # unrecognized block shape — leave untouched


def _tag_system_message(system_message: Any, cache_control: dict[str, str]) -> Any:
    """Tag the last content block of a system message with ``cache_control``.

    Returns the original ``system_message`` unchanged when there is nothing to tag
    so callers can cheaply detect a no-op by identity.
    """
    if system_message is None:
        return system_message
    new_content = _tag_last_block(system_message.content, cache_control)
    if new_content is None:
        return system_message
    return SystemMessage(content=new_content)


def _tag_last_message(messages: list[Any], cache_control: dict[str, str]) -> list[Any]:
    """Tag the last content block of the conversation's last message.

    Reconstructs only that one message via ``model_copy`` (preserving its type,
    tool calls, ids, etc.) and returns a new list. Returns the original list
    unchanged (same identity) when the last message has nothing taggable — e.g. a
    tool-call-only assistant message with empty content — so the caller can detect a
    no-op by identity. At model-call time the last message is normally a human turn
    or a tool result (both carry content), so a breakpoint is placed on virtually
    every turn.

    Trailing *volatile context* messages (``additional_kwargs[VOLATILE_CONTEXT_KEY]``,
    the per-call ``<current_page>``/``<client_objects>`` block) are skipped: they
    are not checkpointed and change between calls, so a breakpoint on them would
    never be hit. The breakpoint goes on the last persisted message in front.
    """
    idx = len(messages) - 1
    while idx >= 0 and (getattr(messages[idx], "additional_kwargs", None) or {}).get(VOLATILE_CONTEXT_KEY):
        idx -= 1
    if idx < 0:
        return messages
    new_content = _tag_last_block(messages[idx].content, cache_control)
    if new_content is None:
        return messages
    new_messages = list(messages)
    new_messages[idx] = messages[idx].model_copy(update={"content": new_content})
    return new_messages
