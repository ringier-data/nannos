"""Recover a model turn that was cut off (``finish_reason == "length"``) mid-generation.

When reasoning is on, thinking tokens are drawn from the same output budget as the
visible response. A deep-reasoning turn can spend the entire ``max_tokens`` budget
thinking and get cut off before it emits any content or the terminal
``FinalResponseSchema`` tool call. The gateway's OpenAI-compatible surface reports this as
``finish_reason == "length"`` on an ``AIMessage`` that has empty content and no tool calls
— and, downstream, the orchestrator's ``parse_agent_response`` finds no structured response
and falls back to the hardcoded "Task completed successfully". A truncated turn is silently
laundered into a fake success.

``model_factory.max_tokens_for_effort`` gives reasoning turns headroom so this is rare, but
a pathological turn can still exhaust even a generous budget. This middleware makes such a
turn *recoverable in place*: it detects the truncation, discards the poisoned generation
(so the partial/empty thinking block never enters the message history — replaying a
truncated thinking block is fragile and would just resume the overthinking), and re-invokes
the model with (a) a nudge instructing it to stop reasoning and produce its answer now, and
(b) a raised ``max_tokens`` for the retry. Only the final, complete response is returned to
the graph. If every retry still truncates, the last (truncated) response is returned
unchanged so the downstream truncation guard can surface a failure rather than a fake
success.

Added to the common middleware stack, so both the orchestrator and in-process sub-agents
get the behavior.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)

# Retry ceilings, escalating per attempt. Opus 4.8 (the fleet default) supports 128k output,
# so these are safe headroom; they only ever apply on a turn that already truncated, i.e. one
# the model has shown it can produce long output for.
_RETRY_MAX_TOKENS: tuple[int, ...] = (32000, 48000)
_DEFAULT_MAX_RETRIES = 2

_WRAP_UP = (
    "You have already reasoned more than enough. Do NOT think further. Produce your final "
    "answer now by calling the FinalResponseSchema tool (or, if no tool applies, respond "
    "directly). Be decisive and concise."
)

_NUDGE_TRUNCATED = (
    "SYSTEM: Your previous response was cut off — it exhausted the output token budget while "
    f"thinking, before producing any answer. {_WRAP_UP}"
)

_NUDGE_EMPTY = (
    "SYSTEM: Your previous response was EMPTY — you spent the turn thinking and returned no "
    f"answer and no tool call. The user saw nothing. {_WRAP_UP}"
)


def _last_ai_message(messages: list[BaseMessage]) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _has_text(msg: AIMessage) -> bool:
    """True when the message carries something the user could actually read.

    Reasoning is not an answer: it arrives as ``reasoning_content`` in
    ``additional_kwargs`` or as reasoning blocks, and the user never sees it.
    """
    content = msg.content
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip():
                return True
    return False


def _dead_turn(response: ModelResponse) -> str | None:
    """Why this turn produced nothing usable — ``"truncated"``, ``"empty"``, or None.

    A turn that produced a tool call is a normal agentic continuation (the graph runs the
    tool and loops), and one that produced text or a structured response is an answer.
    What is left are the two dead ends, both of which reach the user as the hardcoded
    "Task completed successfully":

    - **truncated** — cut off mid-thought (``finish_reason == "length"``);
    - **empty** — a clean stop that spent the whole turn on reasoning and returned no
      content and no tool call. Observed with 69/69 output tokens billed as reasoning.
    """
    if getattr(response, "structured_response", None) is not None:
        return None
    msg = _last_ai_message(response.result or [])
    if msg is None or getattr(msg, "tool_calls", None):
        return None
    if msg.response_metadata.get("finish_reason") == "length":
        return "truncated"
    return None if _has_text(msg) else "empty"


class ContinueOnTruncationMiddleware(AgentMiddleware):
    """Re-run a turn that produced no answer, with a wrap-up nudge.

    Two shapes qualify: cut off mid-thought (``finish_reason == "length"``, which also gets
    more budget) and stopped cleanly with nothing to show (all output tokens spent on
    reasoning). Both otherwise reach the user as "Task completed successfully".
    """

    def __init__(self, max_retries: int = _DEFAULT_MAX_RETRIES) -> None:
        self._max_retries = max_retries

    def _nudged(self, request: ModelRequest, attempt: int, reason: str) -> ModelRequest:
        """Append the wrap-up nudge for ``reason`` and, if truncated, raise ``max_tokens``.

        The nudge goes in ``messages`` (not ``system_message``, which would change the cached
        prefix). ``model_settings`` is bound over the model at invocation, so the raised
        ``max_tokens`` reaches the gateway without rebuilding the model — but only for a
        turn that actually ran out of room. An empty turn stopped cleanly inside its budget,
        so more budget is not the remedy and would only buy more thinking.
        """
        if reason == "truncated":
            ceiling = _RETRY_MAX_TOKENS[min(attempt, len(_RETRY_MAX_TOKENS) - 1)]
            settings = {**request.model_settings, "max_tokens": ceiling}
            nudge = _NUDGE_TRUNCATED
        else:
            settings = request.model_settings
            nudge = _NUDGE_EMPTY
        return request.override(
            messages=[*request.messages, HumanMessage(content=nudge)],
            model_settings=settings,
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        response = handler(request)
        for attempt in range(self._max_retries):
            reason = _dead_turn(response)
            if reason is None:
                return response
            logger.warning(
                "Model turn produced no answer (%s, no tool call); nudging to wrap up (retry %d/%d)",
                reason,
                attempt + 1,
                self._max_retries,
            )
            response = handler(self._nudged(request, attempt, reason))
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        response = await handler(request)
        for attempt in range(self._max_retries):
            reason = _dead_turn(response)
            if reason is None:
                return response
            logger.warning(
                "Model turn produced no answer (%s, no tool call); nudging to wrap up (retry %d/%d)",
                reason,
                attempt + 1,
                self._max_retries,
            )
            response = await handler(self._nudged(request, attempt, reason))
        return response
