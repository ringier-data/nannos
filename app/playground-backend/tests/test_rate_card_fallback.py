"""Test rate card service fallback logic for billing units."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.repositories.rate_card_repository import RateCardRepository
from playground_backend.services.rate_card_service import RateCardService


@pytest.mark.asyncio
async def test_fallback_to_base_input_tokens(pg_session: AsyncSession):
    """Test that cache_read_input_tokens falls back to base_input_tokens rate when no specific rate exists."""
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # Mock: No specific rate for cache_read_input_tokens, but base_input_tokens exists
    async def mock_get_active_rate(db, provider, model_name, billing_unit, as_of):
        if billing_unit == "base_input_tokens":
            return Decimal("3.00")
        return None  # No specific rate for cache_read_input_tokens

    mock_repo.get_active_rate = mock_get_active_rate

    # Calculate cost for cache_read_input_tokens
    cost = await service.calculate_cost(
        db=pg_session,
        provider="bedrock_converse",
        model_name="claude-sonnet-4.5",
        billing_unit_breakdown={"cache_read_input_tokens": 5248},
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    # Expected: (5248 / 1,000,000) * 3.00 = 0.015744 USD
    expected = Decimal("0.015744")
    assert cost == expected


@pytest.mark.asyncio
async def test_fallback_to_base_output_tokens(pg_session: AsyncSession):
    """Test that reasoning_output_tokens falls back to base_output_tokens rate when no specific rate exists."""
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # Mock: No specific rate for reasoning_output_tokens, but base_output_tokens exists
    async def mock_get_active_rate(db, provider, model_name, billing_unit, as_of):
        if billing_unit == "base_output_tokens":
            return Decimal("15.00")
        return None

    mock_repo.get_active_rate = mock_get_active_rate

    # Calculate cost for reasoning_output_tokens
    cost = await service.calculate_cost(
        db=pg_session,
        provider="openai",
        model_name="gpt-4o",
        billing_unit_breakdown={"reasoning_output_tokens": 200},
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    # Expected: (200 / 1,000,000) * 15.00 = 0.003 USD
    expected = Decimal("0.00300000")
    assert cost == expected


@pytest.mark.asyncio
async def test_exact_match_preferred_over_fallback(pg_session: AsyncSession):
    """Test that exact rate match is used when available, not fallback."""
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # Mock: Specific rate for cache_read_input_tokens exists (should NOT use fallback)
    async def mock_get_active_rate(db, provider, model_name, billing_unit, as_of):
        if billing_unit == "cache_read_input_tokens":
            return Decimal("0.30")  # Specific cache read rate
        if billing_unit == "base_input_tokens":
            return Decimal("3.00")  # Base rate
        return None

    mock_repo.get_active_rate = mock_get_active_rate

    # Calculate cost for cache_read_input_tokens
    cost = await service.calculate_cost(
        db=pg_session,
        provider="bedrock_converse",
        model_name="claude-sonnet-4.5",
        billing_unit_breakdown={"cache_read_input_tokens": 5248},
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    # Expected: Uses exact rate (0.30), not fallback (3.00)
    # (5248 / 1,000,000) * 0.30 = 0.0015744 USD
    expected = Decimal("0.0015744")
    assert cost == expected


@pytest.mark.asyncio
async def test_mixed_billing_units_with_fallback(pg_session: AsyncSession):
    """Test cost calculation with multiple billing units, some using fallback."""
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # Mock: Only base rates exist, cache tokens fall back
    async def mock_get_active_rate(db, provider, model_name, billing_unit, as_of):
        if billing_unit == "base_input_tokens":
            return Decimal("3.00")
        if billing_unit == "base_output_tokens":
            return Decimal("15.00")
        # No specific rates for cache or audio
        return None

    mock_repo.get_active_rate = mock_get_active_rate

    # Calculate cost for mix of base and cache tokens
    billing_breakdown = {
        "base_input_tokens": 2631,
        "cache_read_input_tokens": 5248,
        "base_output_tokens": 54,
        "audio_output_tokens": 10,
    }

    cost = await service.calculate_cost(
        db=pg_session,
        provider="openai",
        model_name="gpt-4o",
        billing_unit_breakdown=billing_breakdown,
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    # Expected:
    # base_input: (2631 / 1M) * 3.00 = 0.007893
    # cache_read_input (fallback to base_input): (5248 / 1M) * 3.00 = 0.015744
    # base_output: (54 / 1M) * 15.00 = 0.00081
    # audio_output (fallback to base_output): (10 / 1M) * 15.00 = 0.00015
    # Total = 0.024597
    expected = Decimal("0.02459700")
    assert cost == expected


@pytest.mark.asyncio
async def test_no_fallback_for_request_based_billing(pg_session: AsyncSession):
    """Test that non-token billing units (requests, api_calls) have no fallback."""
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # Mock: Only base_output_tokens exists, no rate for 'requests'
    async def mock_get_active_rate(db, provider, model_name, billing_unit, as_of):
        if billing_unit == "base_output_tokens":
            return Decimal("15.00")
        return None  # No rate for 'requests'

    mock_repo.get_active_rate = mock_get_active_rate

    # Calculate cost for requests billing unit
    cost = await service.calculate_cost(
        db=pg_session,
        provider="foundry",
        model_name="custom-agent",
        billing_unit_breakdown={"requests": 10},
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    # Expected: No fallback for 'requests', cost should be 0
    assert cost == Decimal("0.00000000")


@pytest.mark.asyncio
async def test_fallback_billing_unit_method():
    """Test _get_fallback_billing_unit helper method."""
    service = RateCardService()

    # Input token variants
    assert service._get_fallback_billing_unit("cache_read_input_tokens") == "base_input_tokens"
    assert service._get_fallback_billing_unit("cache_creation_input_tokens") == "base_input_tokens"
    assert service._get_fallback_billing_unit("audio_input_tokens") == "base_input_tokens"
    assert service._get_fallback_billing_unit("custom_input_tokens") == "base_input_tokens"

    # Output token variants
    assert service._get_fallback_billing_unit("audio_output_tokens") == "base_output_tokens"
    assert service._get_fallback_billing_unit("custom_output_tokens") == "base_output_tokens"
    assert service._get_fallback_billing_unit("reasoning_output_tokens") == "base_output_tokens"

    # Base units have no fallback
    assert service._get_fallback_billing_unit("base_input_tokens") is None
    assert service._get_fallback_billing_unit("base_output_tokens") is None

    # Non-token billing units have no fallback (including generic *_tokens patterns)
    assert service._get_fallback_billing_unit("requests") is None
    assert service._get_fallback_billing_unit("api_calls") is None
    assert service._get_fallback_billing_unit("premium_api_calls") is None
    assert service._get_fallback_billing_unit("hypothetical_tokens") is None  # Not _input_tokens or _output_tokens


@pytest.mark.asyncio
async def test_agent_specific_pricing_no_fallback(pg_session: AsyncSession):
    """Test that agent-specific pricing does not use system rate card fallback."""
    service = RateCardService()
    mock_repo = Mock(spec=RateCardRepository)
    service.set_repository(mock_repo)

    # Mock agent pricing config with only 'requests' billing unit
    agent_pricing_config = {"rate_card_entries": [{"billing_unit": "requests", "price_per_million": 50000}]}

    async def mock_fetch_pricing_config(db, config_version_id):
        return agent_pricing_config

    service._fetch_agent_pricing_config = mock_fetch_pricing_config

    # Mock system rate cards (should not be used)
    async def mock_get_active_rate(db, provider, model_name, billing_unit, as_of):
        if billing_unit in ("base_input_tokens", "base_output_tokens"):
            return Decimal("999.00")  # Should NOT be used
        return None

    mock_repo.get_active_rate = mock_get_active_rate

    # Calculate cost with billing unit not in agent config
    # Since provider/model_name are None, should not fall back to system rates
    cost = await service.calculate_cost(
        db=pg_session,
        provider=None,
        model_name=None,
        billing_unit_breakdown={"base_input_tokens": 1000},
        sub_agent_config_version_id=123,
        as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    # Expected: No rate found, cost = 0
    assert cost == Decimal("0.00000000")
