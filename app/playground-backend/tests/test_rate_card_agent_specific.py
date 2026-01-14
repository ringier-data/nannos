"""Test agent-specific rate card pricing."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.repositories.rate_card_repository import RateCardRepository
from playground_backend.services.rate_card_service import RateCardService


@pytest.mark.asyncio
async def test_calculate_cost_with_agent_specific_requests_pricing(pg_session: AsyncSession):
    """Test that cost calculation works with agent-specific 'requests' billing unit."""
    # Setup
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # Create mock pricing config matching the format from the database
    agent_pricing_config = {
        "format": "detailed",
        "rate_card_entries": [
            {
                "billing_unit": "requests",
                "price_per_million": 100000,  # $100,000 per million = $0.10 per request
            }
        ],
    }

    # Mock _fetch_agent_pricing_config to return our config
    async def mock_fetch_pricing_config(db, config_version_id):
        if config_version_id == 123:
            return agent_pricing_config
        return None

    service._fetch_agent_pricing_config = mock_fetch_pricing_config

    # Test: Calculate cost for 1 request
    # Expected: (1 / 1,000,000) * 100,000 = 0.1 USD
    cost = await service.calculate_cost(
        db=pg_session,
        provider=None,  # Not required for agent-specific pricing
        model_name=None,  # Not required for agent-specific pricing
        billing_unit_breakdown={"requests": 1},
        sub_agent_config_version_id=123,
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert cost == Decimal("0.10000000"), f"Expected 0.10000000 but got {cost}"


@pytest.mark.asyncio
async def test_calculate_cost_with_agent_specific_multiple_requests(pg_session: AsyncSession):
    """Test that cost calculation works for multiple requests."""
    # Setup
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    agent_pricing_config = {
        "format": "detailed",
        "rate_card_entries": [
            {
                "billing_unit": "requests",
                "price_per_million": 100000,  # $100,000 per million = $0.10 per request
            }
        ],
    }

    async def mock_fetch_pricing_config(db, config_version_id):
        if config_version_id == 123:
            return agent_pricing_config
        return None

    service._fetch_agent_pricing_config = mock_fetch_pricing_config

    # Test: Calculate cost for 10 requests
    # Expected: (10 / 1,000,000) * 100,000 = 1.0 USD
    cost = await service.calculate_cost(
        db=pg_session,
        provider=None,
        model_name=None,
        billing_unit_breakdown={"requests": 10},
        sub_agent_config_version_id=123,
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert cost == Decimal("1.00000000"), f"Expected 1.00000000 but got {cost}"


@pytest.mark.asyncio
async def test_calculate_cost_without_provider_model_fallback_fails_gracefully(pg_session: AsyncSession):
    """Test that cost calculation returns 0 when no agent pricing and no provider/model."""
    # Setup
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # No agent-specific pricing config
    async def mock_fetch_pricing_config(db, config_version_id):
        return None

    service._fetch_agent_pricing_config = mock_fetch_pricing_config

    # Test: Calculate cost without provider/model and no agent pricing
    # Expected: 0 (cannot look up rate)
    cost = await service.calculate_cost(
        db=pg_session,
        provider=None,
        model_name=None,
        billing_unit_breakdown={"requests": 1},
        sub_agent_config_version_id=123,
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert cost == Decimal("0.00000000"), f"Expected 0.00000000 but got {cost}"


@pytest.mark.asyncio
async def test_get_agent_rate_extracts_correct_billing_unit(pg_session: AsyncSession):
    """Test that _get_agent_rate correctly extracts the rate for a specific billing unit."""
    service = RateCardService()

    pricing_config = {
        "format": "detailed",
        "rate_card_entries": [
            {"billing_unit": "input_tokens", "price_per_million": 3.0},
            {"billing_unit": "output_tokens", "price_per_million": 15.0},
            {"billing_unit": "requests", "price_per_million": 100000},
        ],
    }

    # Test extracting each rate
    input_rate = service._get_agent_rate(pricing_config, "input_tokens")
    assert input_rate == Decimal("3.0")

    output_rate = service._get_agent_rate(pricing_config, "output_tokens")
    assert output_rate == Decimal("15.0")

    requests_rate = service._get_agent_rate(pricing_config, "requests")
    assert requests_rate == Decimal("100000")

    # Test non-existent billing unit
    missing_rate = service._get_agent_rate(pricing_config, "nonexistent")
    assert missing_rate is None
