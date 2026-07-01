"""Model-call middleware that stamps gateway cost-attribution from LangGraph tags.

Gateway cost attribution is carried on request-scoped ContextVars
(``ringier_a2a_sdk.cost_tracking.attribution``) that an httpx event hook stamps
onto the ``x-litellm-spend-logs-metadata`` header of every outbound Model Gateway
call. The proxy's ``CostLogger`` is the single source of cost for all gateway
traffic, so those ContextVars must be correct for *every* model call.

The historical mechanism sets those ContextVars at each boundary where the active
agent changes — the orchestrator turn (executor), a remote request
(``SubAgentIdMiddleware``, ASGI), and a local sub-agent dispatch. That is fragile:
a boundary that forgets to set/restore ``current_sub_agent_id`` silently
misattributes an in-process sub-agent's tokens to the orchestrator.

This middleware removes the boundary dependency. Every model call flows through
the agent middleware stack, so we derive attribution from the call's *own*
LangGraph tags (``user_sub:``, ``conversation:``, ``sub_agent:``,
``scheduled_job:`` — the same tags the app-side cost tags already carry) and set
the ContextVars for the duration of that single call, restoring the caller's
values afterwards. Attribution becomes correct-by-construction: each call
self-attributes from its own config, independent of who dispatched the agent.

Only the fields present in the tags are set; anything absent (e.g. ``installation``
set by the executor, or the orchestrator's own calls which carry no ``sub_agent:``
tag) falls through to whatever the caller already set. ``attribution_scope`` is a
plain context manager and ``[a]wrap_model_call`` are ordinary (non-generator)
callables, so ``Token.reset()`` runs in the same context it was created in — no
async-generator restore hazard.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


def _parse_attribution_from_tags(tags: list[str] | None) -> dict[str, Any]:
    """Extract attribution fields from LangGraph tags.

    Mirrors the ``user_sub:``/``conversation:``/``sub_agent:``/``scheduled_job:``
    tag scheme produced by ``create_runnable_config`` and
    ``LocalA2ARunnable.extend_config_for_subagent``. Integer fields that fail to
    parse are dropped rather than raising.
    """
    if not tags:
        return {}
    fields: dict[str, Any] = {}
    for tag in tags:
        if tag.startswith("user_sub:"):
            fields["user_sub"] = tag.split(":", 1)[1]
        elif tag.startswith("conversation:"):
            fields["conversation_id"] = tag.split(":", 1)[1]
        elif tag.startswith("sub_agent_config_version:"):
            try:
                fields["sub_agent_config_version_id"] = int(tag.split(":", 1)[1])
            except ValueError:
                logger.debug("Could not parse sub_agent_config_version id from tag %r", tag)
        elif tag.startswith("sub_agent:"):
            try:
                fields["sub_agent_id"] = int(tag.split(":", 1)[1])
            except ValueError:
                logger.debug("Could not parse sub_agent id from tag %r", tag)
        elif tag.startswith("scheduled_job:"):
            try:
                fields["scheduled_job_id"] = int(tag.split(":", 1)[1])
            except ValueError:
                logger.debug("Could not parse scheduled_job id from tag %r", tag)
    return fields


class GatewayAttributionMiddleware(AgentMiddleware):
    """Set gateway cost-attribution ContextVars from the model call's own tags."""

    def _attribution_fields(self) -> dict[str, Any]:
        # get_config() reads the RunnableConfig active for this model call; its
        # ``tags`` carry the attribution scheme. Guard defensively — outside a
        # runnable context it raises, and a missing/edge config must not break the
        # model call.
        from langgraph.config import get_config

        try:
            cfg = get_config()
        except Exception:
            return {}
        return _parse_attribution_from_tags((cfg or {}).get("tags"))

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        from ringier_a2a_sdk.cost_tracking.attribution import attribution_scope

        with attribution_scope(**self._attribution_fields()):
            return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        from ringier_a2a_sdk.cost_tracking.attribution import attribution_scope

        with attribution_scope(**self._attribution_fields()):
            return await handler(request)
