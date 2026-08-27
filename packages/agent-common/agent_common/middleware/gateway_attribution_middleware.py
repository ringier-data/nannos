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

import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


def attribution_fields_from_run_config() -> dict[str, Any]:
    """Attribution fields parsed from the active LangGraph run config's tags.

    ``get_config()`` reads the RunnableConfig active for the current runnable
    context; its ``tags`` carry the attribution scheme. Guarded defensively —
    outside a runnable context it raises, and a missing/edge config must never
    break the caller. Returns ``{}`` when nothing can be derived.
    """
    from langgraph.config import get_config
    from ringier_a2a_sdk.cost_tracking.attribution import parse_attribution_tags

    try:
        cfg = get_config()
    except Exception:
        return {}
    return parse_attribution_tags((cfg or {}).get("tags"))


@contextlib.contextmanager
def run_config_attribution_scope() -> Iterator[None]:
    """Set gateway cost-attribution ContextVars from the active run config's tags
    for the duration of the block, restoring the caller's values afterwards.

    For side-channel model calls that do NOT flow through the agent middleware
    stack (HITL tool-call summaries, LLM tool-risk scoring) but still run inside
    the graph's runnable context. Without this they inherit whatever the dispatch
    boundary set — misattributing an in-process sub-agent's spend to the
    orchestrator. Same mechanism as ``GatewayAttributionMiddleware``.
    """
    from ringier_a2a_sdk.cost_tracking.attribution import attribution_scope

    with attribution_scope(**attribution_fields_from_run_config()):
        yield


class GatewayAttributionMiddleware(AgentMiddleware):
    """Set gateway cost-attribution ContextVars from the model call's own tags."""

    def _attribution_fields(self) -> dict[str, Any]:
        return attribution_fields_from_run_config()

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
