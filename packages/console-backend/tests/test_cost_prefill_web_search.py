"""Cost-prefill seeding for the web_search billing unit.

A web-search-capable model carries its per-query grounding fee in model_info as
``search_context_cost_per_query`` (keyed by context size). Registration should seed the
``web_search`` rate-card unit from the ``medium`` tier (the size gateway_web_search uses),
so the search fee is billable on save instead of silently $0 until hand-entered.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import console_backend.routers.admin_model_gateway_router as router


def _request(model_info: dict, *, stored_rates: dict | None = None):
    """A request whose gateway returns one model with the given model_info, and a rate card with
    `stored_rates` (default empty → prefill falls through to the gateway-cost path)."""
    state = SimpleNamespace(
        model_gateway_service=SimpleNamespace(
            get_model=AsyncMock(return_value={"model_info": model_info, "litellm_params": {}})
        ),
        rate_card_service=SimpleNamespace(
            repository=SimpleNamespace(get_all_active_rates=AsyncMock(return_value=stored_rates or {}))
        ),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


_SEARCH_COSTS = {
    "search_context_size_low": 0.014,
    "search_context_size_medium": 0.014,
    "search_context_size_high": 0.014,
}


@pytest.mark.asyncio
async def test_seeds_web_search_unit_from_medium_tier():
    info = {
        "litellm_provider": "vertex_ai",
        "input_cost_per_token": 5e-7,
        "supports_web_search": True,
        "search_context_cost_per_query": _SEARCH_COSTS,
    }
    out = await router.cost_prefill("gemini-3-flash", _request(info), db=None, user=SimpleNamespace())
    ws = out.pricing["web_search"]
    # 0.014 $/query × 1e6 = 14_000 per-1M; placed under output to match the screenshot UI.
    assert ws.price_per_million == Decimal("0.014") * Decimal(1_000_000)
    assert ws.flow_direction == "output"
    # token costs still seeded alongside it
    assert "base_input_tokens" in out.pricing


@pytest.mark.asyncio
async def test_no_web_search_unit_when_model_has_no_search_cost():
    info = {"litellm_provider": "bedrock", "input_cost_per_token": 1e-6}
    out = await router.cost_prefill("claude", _request(info), db=None, user=SimpleNamespace())
    assert "web_search" not in out.pricing


@pytest.mark.asyncio
async def test_free_search_tier_is_left_unpriced():
    # The rate card requires a positive price, so a 0.0 (free) medium tier is not seeded.
    info = {
        "litellm_provider": "vertex_ai",
        "search_context_cost_per_query": {"search_context_size_medium": 0.0},
    }
    out = await router.cost_prefill("free-search", _request(info), db=None, user=SimpleNamespace())
    assert "web_search" not in out.pricing


@pytest.mark.asyncio
async def test_falls_back_to_low_tier_when_medium_absent():
    info = {
        "litellm_provider": "vertex_ai",
        "search_context_cost_per_query": {"search_context_size_low": 0.01, "search_context_size_high": 0.05},
    }
    out = await router.cost_prefill("gemini-x", _request(info), db=None, user=SimpleNamespace())
    assert out.pricing["web_search"].price_per_million == Decimal("0.01") * Decimal(1_000_000)
