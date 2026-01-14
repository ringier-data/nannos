"""Integration test for pricing_config read/write operations."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_pricing_config_read_write_integration(pg_session: AsyncSession, sub_agent_service):
    """Test that pricing_config can be written to and read from the database using service layer."""
    from playground_backend.models.sub_agent import SubAgentCreate, SubAgentType

    # Create a user (still need raw SQL for test setup)
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, created_at, updated_at)
            VALUES ('test-user-id', 'test-user-sub', 'test@example.com', 'Test', 'User', false, 'member', NOW(), NOW())
        """)
    )
    await pg_session.commit()

    # Define pricing config
    pricing_config = {
        "rate_card_entries": [
            {"billing_unit": "api_calls", "price_per_million": 2.5},
            {"billing_unit": "database_queries", "price_per_million": 1.0},
        ]
    }

    # Create sub-agent using service layer (tests the write path)
    sub_agent_data = SubAgentCreate(
        name="pricing-test-agent",
        type=SubAgentType.REMOTE,
        description="Test agent with pricing",
        agent_url="http://test.com",
        pricing_config=pricing_config,
    )

    sub_agent = await sub_agent_service.create_sub_agent(
        db=pg_session,
        user_id="test-user-id",
        data=sub_agent_data,
    )

    # Read back using service (tests the read path)
    sub_agent_read = await sub_agent_service.get_sub_agent_by_id(pg_session, sub_agent.id)

    # Verify pricing_config was persisted and read correctly
    assert sub_agent_read is not None
    assert sub_agent_read.config_version is not None
    assert sub_agent_read.config_version.pricing_config is not None
    assert sub_agent_read.config_version.pricing_config == pricing_config
