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

    from ..models.model_gateway import WebSearchConfig

logger = logging.getLogger(__name__)

# Cap sources echoed back so a many-citation answer doesn't flood the agent's context.
_MAX_CITATIONS = 10


def model_info_by_name(raw: list[dict]) -> dict[str, dict]:
    """Reshape the gateway's raw ``list_models`` payload into ``{model_name: model_info}``
    (``model_info`` defaulted to ``{}``, names without a ``model_name`` dropped).

    Single source of that reshape, shared by the web-search resolver and the System Status row
    (feature_status.collect_system_status) so the two read the gateway snapshot identically.
    """
    return {m["model_name"]: (m.get("model_info") or {}) for m in raw if m.get("model_name")}


def _cost_key(name: str, info: dict) -> tuple:
    """Sort key for 'cheapest web-search-capable': by input cost, then output cost, with a
    missing cost sorting last and the model name as a final deterministic tie-break (so the
    chosen model — and thus what the tool bills against — never flips on equal costs)."""
    in_cost = info.get("input_cost_per_token")
    out_cost = info.get("output_cost_per_token")
    return (in_cost is None, in_cost or 0.0, out_cost is None, out_cost or 0.0, name)


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
        cheapest = min(capable, key=lambda n: _cost_key(n, capable[n]))
        return cheapest, "auto"
    return None, None


def resolve_web_search_config(raw: list[dict], search_default: str | None) -> "WebSearchConfig":
    """Fully-resolved Web Search picker state from a gateway model list + the ``search`` default.

    The single backend-owned source of the pick (capable models cheapest-first, which one is active,
    and whether it's selected vs auto), so the console renders it instead of re-deriving the choice.
    """
    from ..models.model_gateway import WebSearchConfig, WebSearchModelOption

    info_by_name = model_info_by_name(raw)
    active_name, source = pick_web_search_model(search_default, info_by_name)

    capable = sorted(
        ((n, i) for n, i in info_by_name.items() if i.get("supports_web_search")),
        key=lambda ni: _cost_key(ni[0], ni[1]),
    )
    options = [
        WebSearchModelOption(
            model_id=info.get("id"),
            model_name=name,
            is_cheapest=(idx == 0),
            is_active=(name == active_name),
        )
        for idx, (name, info) in enumerate(capable)
    ]
    active_id = next((o.model_id for o in options if o.is_active), None)
    return WebSearchConfig(
        available=bool(options),
        source=source,
        active_model_id=active_id,
        active_model_name=active_name,
        models=options,
    )


async def resolve_web_search_model(request: "Request", db: "AsyncSession") -> str | None:
    """The model that backs ``console_web_search`` right now, or ``None`` when web search is
    unavailable (gateway unreadable, or no web-search-capable model registered)."""
    try:
        raw = await request.app.state.model_gateway_service.list_models()
    except ModelGatewayError as e:
        logger.warning("Web search: gateway model list unreadable (%s)", e)
        return None
    defaults = await request.app.state.model_defaults_service.get_all(db)
    model, _ = pick_web_search_model(defaults.get("search"), model_info_by_name(raw))
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
