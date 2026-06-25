"""Web-search model resolution + result formatting for the ``console_web_search`` MCP tool.

Web search is delivered as a console-backend MCP tool (see routers/web_search_mcp_tools.py):
the orchestrator and any sub-agent that selects it call ``console_web_search``, which runs one
isolated, function-tool-free gateway completion with ``web_search_options`` against a
web-search-capable model and returns the grounded answer + sources.

The active model is the admin's ``search`` default when it's registered and web-search-capable,
else the cheapest web-search-capable model (auto). ``pick_web_search_model`` is the single source
of that decision, shared with the System Status row (feature_status._web_search_feature) so the
status page and the tool can never disagree.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .model_gateway_service import ModelGatewayError

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Cap sources echoed back so a many-citation answer doesn't flood the agent's context.
_MAX_CITATIONS = 10


def pick_web_search_model(
    search_default: str | None, model_info_by_name: dict[str, dict] | None
) -> tuple[str | None, str | None]:
    """Resolve the web-search model → ``(model_name, source)`` where source is
    ``"selected"`` (admin's ``search`` default) or ``"auto"`` (cheapest capable), or
    ``(None, None)`` when no web-search-capable model is available.

    When the gateway snapshot is unreadable (``model_info_by_name is None``), trust an explicit
    default if set (fail open) rather than declaring web search off on a transient blip.
    """
    if model_info_by_name is None:
        return (search_default, "selected") if search_default else (None, None)

    capable = {n: i for n, i in model_info_by_name.items() if i.get("supports_web_search")}
    if search_default and search_default in capable:
        return search_default, "selected"
    if capable:
        cheapest = min(
            capable,
            key=lambda n: (capable[n].get("input_cost_per_token") is None, capable[n].get("input_cost_per_token") or 0.0),
        )
        return cheapest, "auto"
    return None, None


async def resolve_web_search_model(request: "Request", db: "AsyncSession") -> str | None:
    """The model that backs ``console_web_search`` right now, or ``None`` when web search is
    unavailable (gateway unreadable, or no web-search-capable model registered)."""
    try:
        raw = await request.app.state.model_gateway_service.list_models()
    except ModelGatewayError as e:
        logger.warning("Web search: gateway model list unreadable (%s)", e)
        return None
    model_info_by_name = {m["model_name"]: (m.get("model_info") or {}) for m in raw if m.get("model_name")}
    defaults = await request.app.state.model_defaults_service.get_all(db)
    model, _ = pick_web_search_model(defaults.get("search"), model_info_by_name)
    return model


def format_web_search_result(answer: str, citations: list[dict]) -> str:
    """Render the grounded answer with a numbered ``Sources`` list the agent can cite."""
    body = (answer or "").strip() or "(the web search returned no text answer)"
    if not citations:
        return body
    lines = [body, "", "Sources:"]
    lines += [
        f"{i}. {c.get('title') or c.get('url')} — {c.get('url')}"
        for i, c in enumerate(citations[:_MAX_CITATIONS], 1)
    ]
    return "\n".join(lines)
