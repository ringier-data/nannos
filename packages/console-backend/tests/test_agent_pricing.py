"""Tests for agent-specific pricing configuration."""

import json
from decimal import Decimal

import pytest
from console_backend.services.rate_card_service import RateCardService
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_agent_pricing_detailed_format(pg_session: AsyncSession):
    """Test that agent-specific detailed pricing is used correctly."""
    # Create a sub_agent_config_version with detailed pricing
    from sqlalchemy import text

    # First create a user (foreign key requirement)
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, created_at, updated_at)
            VALUES ('test-user-id', 'test-user', 'test@example.com', 'Test', 'User', false, 'member', NOW(), NOW())
        """)
    )
    await pg_session.commit()

    # Insert sub_agent first (foreign key requirement)
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (name, owner_user_id, type, current_version, created_at, updated_at)
            VALUES ('test-agent', 'test-user-id', 'remote', 1, NOW(), NOW())
        """)
    )

    # Get sub_agent_id
    result = await pg_session.execute(text("SELECT id FROM sub_agents WHERE name = 'test-agent'"))
    sub_agent_id = result.scalar_one()

    # Insert config version with pricing
    pricing_config = {
        "rate_card_entries": [
            {"billing_unit": "premium_api_calls", "price_per_million": 10.0},
            {"billing_unit": "vector_searches", "price_per_million": 5.0},
        ]
    }
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions 
            (sub_agent_id, version, version_hash, description, created_at, pricing_config, system_prompt)
            VALUES (:sub_agent_id, 1, 'abc123456789', 'Test agent', NOW(), CAST(:pricing AS jsonb), 'test prompt')
        """),
        {"sub_agent_id": sub_agent_id, "pricing": json.dumps(pricing_config)},
    )
    await pg_session.commit()

    # Get the ID of the created version
    result = await pg_session.execute(
        text("SELECT id FROM sub_agent_config_versions WHERE version_hash = 'abc123456789'")
    )
    config_version_id = result.scalar_one()

    # Create service and test
    from console_backend.repositories.rate_card_repository import RateCardRepository

    service = RateCardService()
    service.set_repository(RateCardRepository())

    # Test with agent pricing
    billing_unit_breakdown = {
        "premium_api_calls": 1_000_000,  # 1M calls
        "vector_searches": 500_000,  # 500K searches
    }

    cost = await service.calculate_cost(
        db=pg_session,
        provider="remote",
        model_name="custom",
        billing_unit_breakdown=billing_unit_breakdown,
        sub_agent_config_version_id=config_version_id,
    )

    # Expected: (1M * $10/M) + (500K * $5/M) = $10 + $2.5 = $12.50
    assert cost == Decimal("12.50")


@pytest.mark.asyncio
async def test_agent_pricing_rate_card_entries_format(pg_session: AsyncSession):
    """Test that agent-specific rate_card_entries pricing is used correctly."""
    from sqlalchemy import text

    # First create a user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, created_at, updated_at)
            VALUES ('test-user-id-2', 'test-user', 'test2@example.com', 'Test', 'User', false, 'member', NOW(), NOW())
        """)
    )
    await pg_session.commit()

    # Insert sub_agent first
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (name, owner_user_id, type, current_version, created_at, updated_at)
            VALUES ('test-agent-2', 'test-user-id-2', 'remote', 1, NOW(), NOW())
        """)
    )
    result = await pg_session.execute(text("SELECT id FROM sub_agents WHERE name = 'test-agent-2'"))
    sub_agent_id = result.scalar_one()

    # Insert config version with rate_card_entries pricing format
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions
            (sub_agent_id, version, version_hash, description, created_at, pricing_config, system_prompt)
            VALUES (:sub_agent_id, 1, 'def123456789', 'Test agent', NOW(), CAST(:pricing AS jsonb), 'test prompt')
        """),
        {
            "sub_agent_id": sub_agent_id,
            "pricing": json.dumps(
                {
                    "rate_card_entries": [
                        {"billing_unit": "api_requests", "price_per_million": 0.05, "flow_direction": "input"}
                    ]
                }
            ),
        },
    )
    await pg_session.commit()

    result = await pg_session.execute(
        text("SELECT id FROM sub_agent_config_versions WHERE version_hash = 'def123456789'")
    )
    config_version_id = result.scalar_one()

    from console_backend.repositories.rate_card_repository import RateCardRepository

    service = RateCardService()
    service.set_repository(RateCardRepository())

    # Test with rate_card_entries pricing (applies per billing unit)
    billing_unit_breakdown = {
        "api_requests": 2_000_000,  # 2M requests
    }

    cost = await service.calculate_cost(
        db=pg_session,
        provider="remote",
        model_name="custom",
        billing_unit_breakdown=billing_unit_breakdown,
        sub_agent_config_version_id=config_version_id,
    )

    # Expected: 2M * $0.05/M = $0.10
    assert cost == Decimal("0.10")


@pytest.mark.asyncio
async def test_agent_pricing_fallback_to_system(pg_session: AsyncSession, caplog):
    """Test that system rate is used when agent pricing is missing a billing unit."""
    from sqlalchemy import text

    # First create a user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, created_at, updated_at)
            VALUES ('test-user-id-3', 'test-user', 'test3@example.com', 'Test', 'User', false, 'member', NOW(), NOW())
        """)
    )
    await pg_session.commit()

    # Insert sub_agent first
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (name, owner_user_id, type, current_version, created_at, updated_at)
            VALUES ('test-agent-3', 'test-user-id-3', 'remote', 1, NOW(), NOW())
        """)
    )
    result = await pg_session.execute(text("SELECT id FROM sub_agents WHERE name = 'test-agent-3'"))
    sub_agent_id = result.scalar_one()

    # Create system rate card
    await pg_session.execute(
        text("""
            INSERT INTO rate_cards (provider, model_name, created_at, updated_at)
            VALUES ('bedrock', 'claude-sonnet-4.5', NOW(), NOW())
            RETURNING id
        """)
    )
    rate_card_result = await pg_session.execute(
        text("SELECT id FROM rate_cards WHERE provider = 'bedrock' AND model_name = 'claude-sonnet-4.5'")
    )
    rate_card_id = rate_card_result.scalar_one()

    await pg_session.execute(
        text("""
            INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from)
            VALUES (:rate_card_id, 'input_tokens', 'input', 3.0, NOW() - INTERVAL '1 day'),
                   (:rate_card_id, 'output_tokens', 'output', 15.0, NOW() - INTERVAL '1 day')
        """),
        {"rate_card_id": rate_card_id},
    )

    # Insert config version with partial pricing (only covers input_tokens)
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions 
            (sub_agent_id, version, version_hash, description, created_at, pricing_config, system_prompt)
            VALUES (:sub_agent_id, 1, 'ghi123456789', 'Test agent', NOW(), CAST(:pricing AS jsonb), 'test prompt')
        """),
        {
            "sub_agent_id": sub_agent_id,
            "pricing": json.dumps(
                {
                    "rate_card_entries": [
                        {"billing_unit": "input_tokens", "price_per_million": 2.0},
                    ]
                }
            ),
        },
    )
    await pg_session.commit()

    result = await pg_session.execute(
        text("SELECT id FROM sub_agent_config_versions WHERE version_hash = 'ghi123456789'")
    )
    config_version_id = result.scalar_one()

    from console_backend.repositories.rate_card_repository import RateCardRepository

    service = RateCardService()
    service.set_repository(RateCardRepository())

    billing_unit_breakdown = {
        "input_tokens": 1_000_000,  # 1M input
        "output_tokens": 500_000,  # 500K output
    }

    cost = await service.calculate_cost(
        db=pg_session,
        provider="bedrock",
        model_name="claude-sonnet-4.5",
        billing_unit_breakdown=billing_unit_breakdown,
        sub_agent_config_version_id=config_version_id,
    )

    # Expected:
    # - input_tokens: 1M * $2/M (agent price) = $2.00
    # - output_tokens: 500K * $15/M (system price) = $7.50
    # Total: $9.50
    assert cost == Decimal("9.50")

    # Verify warning was logged about fallback
    assert "no rate found for billing_unit=output_tokens" in caplog.text


@pytest.mark.asyncio
async def test_no_agent_pricing_uses_system(pg_session: AsyncSession):
    """Test that system rates are used when no agent pricing is configured."""
    from sqlalchemy import text

    # Create system rate card
    await pg_session.execute(
        text("""
            INSERT INTO rate_cards (provider, model_name, created_at, updated_at)
            VALUES ('bedrock', 'claude-sonnet-4.5', NOW(), NOW())
            RETURNING id
        """)
    )
    rate_card_result = await pg_session.execute(
        text("SELECT id FROM rate_cards WHERE provider = 'bedrock' AND model_name = 'claude-sonnet-4.5'")
    )
    rate_card_id = rate_card_result.scalar_one()

    await pg_session.execute(
        text("""
            INSERT INTO rate_card_entries (rate_card_id, billing_unit, flow_direction, price_per_million, effective_from)
            VALUES (:rate_card_id, 'input_tokens', 'input', 3.0, NOW() - INTERVAL '1 day')
        """),
        {"rate_card_id": rate_card_id},
    )
    await pg_session.commit()

    from console_backend.repositories.rate_card_repository import RateCardRepository

    service = RateCardService()
    service.set_repository(RateCardRepository())

    billing_unit_breakdown = {"input_tokens": 1_000_000}

    # Call without sub_agent_config_version_id
    cost = await service.calculate_cost(
        db=pg_session,
        provider="bedrock",
        model_name="claude-sonnet-4.5",
        billing_unit_breakdown=billing_unit_breakdown,
    )

    # Expected: 1M * $3/M = $3.00 (system rate)
    assert cost == Decimal("3.00")


@pytest.mark.asyncio
async def test_agent_pricing_multiple_billing_units(pg_session: AsyncSession):
    """Test complex scenario with multiple custom billing units."""
    from sqlalchemy import text

    # First create a user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, created_at, updated_at)
            VALUES ('test-user-id-4', 'test-user', 'test4@example.com', 'Test', 'User', false, 'member', NOW(), NOW())
        """)
    )
    await pg_session.commit()

    # Insert sub_agent first
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (name, owner_user_id, type, current_version, created_at, updated_at)
            VALUES ('test-agent-4', 'test-user-id-4', 'remote', 1, NOW(), NOW())
        """)
    )
    result = await pg_session.execute(text("SELECT id FROM sub_agents WHERE name = 'test-agent-4'"))
    sub_agent_id = result.scalar_one()

    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions 
            (sub_agent_id, version, version_hash, description, created_at, pricing_config, system_prompt)
            VALUES (:sub_agent_id, 1, 'jkl123456789', 'Test agent', NOW(), CAST(:pricing AS jsonb), 'test prompt')
        """),
        {
            "sub_agent_id": sub_agent_id,
            "pricing": json.dumps(
                {
                    "rate_card_entries": [
                        {"billing_unit": "database_queries", "price_per_million": 1.0},
                        {"billing_unit": "api_calls", "price_per_million": 0.5},
                        {"billing_unit": "cache_hits", "price_per_million": 0.01},
                    ]
                }
            ),
        },
    )
    await pg_session.commit()

    result = await pg_session.execute(
        text("SELECT id FROM sub_agent_config_versions WHERE version_hash = 'jkl123456789'")
    )
    config_version_id = result.scalar_one()

    from console_backend.repositories.rate_card_repository import RateCardRepository

    service = RateCardService()
    service.set_repository(RateCardRepository())

    billing_unit_breakdown = {
        "database_queries": 5_000_000,  # 5M queries
        "api_calls": 10_000_000,  # 10M calls
        "cache_hits": 50_000_000,  # 50M hits
    }

    cost = await service.calculate_cost(
        db=pg_session,
        provider="remote",
        model_name="custom",
        billing_unit_breakdown=billing_unit_breakdown,
        sub_agent_config_version_id=config_version_id,
    )

    # Expected: (5M * $1/M) + (10M * $0.5/M) + (50M * $0.01/M) = $5 + $5 + $0.5 = $10.50
    assert cost == Decimal("10.50")
