"""Cost-prefill source selection.

Opening the edit dialog (``source=auto``) seeds the form from the stored rate card. The
"Pre-fill from gateway" button (``source=gateway``) must skip that card and return the gateway's
cost — otherwise a card registered without cache rates can never be repaired from the button,
and cache tokens keep billing at the base-input fallback rate.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import console_backend.routers.admin_model_gateway_router as router


def _request(model_info: dict, *, stored_rates: dict):
    """A request whose gateway returns one model with `model_info`, and a rate card with `stored_rates`."""
    state = SimpleNamespace(
        model_gateway_service=SimpleNamespace(
            get_model=AsyncMock(return_value={"model_info": model_info, "litellm_params": {}})
        ),
        rate_card_service=SimpleNamespace(
            repository=SimpleNamespace(get_all_active_rates=AsyncMock(return_value=stored_rates))
        ),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


_GATEWAY_INFO = {
    "litellm_provider": "bedrock_converse",
    "input_cost_per_token": 2.2e-6,
    "output_cost_per_token": 1.1e-5,
    "cache_read_input_token_cost": 2.2e-7,
    "cache_creation_input_token_cost": 2.75e-6,
}
_STORED = {"base_input_tokens": Decimal("2"), "base_output_tokens": Decimal("10")}


@pytest.mark.asyncio
async def test_auto_prefers_stored_rate_card():
    out = await router.cost_prefill(
        "sonnet", _request(_GATEWAY_INFO, stored_rates=_STORED), db=None, user=SimpleNamespace(), source="auto"
    )
    assert out.source == "rate_card"
    assert set(out.pricing) == {"base_input_tokens", "base_output_tokens"}
    assert out.pricing["base_input_tokens"].price_per_million == Decimal("2")


@pytest.mark.asyncio
async def test_gateway_skips_stored_rate_card():
    request = _request(_GATEWAY_INFO, stored_rates=_STORED)
    out = await router.cost_prefill("sonnet", request, db=None, user=SimpleNamespace(), source="gateway")
    assert out.source == "gateway"
    request.app.state.rate_card_service.repository.get_all_active_rates.assert_not_awaited()
    assert out.pricing["base_input_tokens"].price_per_million == Decimal("2.2")
    assert out.pricing["base_output_tokens"].price_per_million == Decimal("11")
    assert out.pricing["cache_read_input_tokens"].price_per_million == Decimal("0.22")
    assert out.pricing["cache_creation_input_tokens"].price_per_million == Decimal("2.75")


@pytest.mark.asyncio
async def test_gateway_unknown_model_is_empty_even_with_stored_card():
    out = await router.cost_prefill(
        "sonnet", _request({}, stored_rates=_STORED), db=None, user=SimpleNamespace(), source="gateway"
    )
    assert out.pricing == {}
