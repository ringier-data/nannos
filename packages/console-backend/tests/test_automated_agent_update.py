"""Test automated agent update functionality."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.sub_agent import SubAgentCreate, SubAgentType, SubAgentUpdate
from playground_backend.models.user import User
from playground_backend.services.sub_agent_service import SubAgentService


@pytest.mark.asyncio
async def test_update_automated_agent_without_foundry_fields(
    pg_session: AsyncSession,
    test_user_db: User,
    sub_agent_service: SubAgentService,
):
    """Test that updating an AUTOMATED agent does not require Foundry fields."""
    # Create an AUTOMATED agent
    create_data = SubAgentCreate(
        name="Dante Verse Daily Inspiration",
        type=SubAgentType.AUTOMATED,
        is_public=False,
        description="Retrieves Divina Commedia verses",
        model="claude-sonnet-4.6",
        system_prompt="You are a literary bridge-builder.",
        mcp_tools=["docstore_search", "read_file", "write_file"],
        enable_thinking=False,
    )

    agent = await sub_agent_service.create_sub_agent(
        db=pg_session,
        data=create_data,
        actor=test_user_db,
    )

    assert agent is not None
    assert agent.type == SubAgentType.AUTOMATED

    # Update the agent without providing Foundry fields (this was failing before)
    update_data = SubAgentUpdate(
        description="Updated description",
        system_prompt="Updated prompt about data engineering.",
        change_summary="Updated configuration from playground",
    )

    updated_agent = await sub_agent_service.update_sub_agent(
        db=pg_session,
        sub_agent_id=agent.id,
        data=update_data,
        actor=test_user_db,
    )

    assert updated_agent is not None
    assert updated_agent.config_version.system_prompt == "Updated prompt about data engineering."
    # Verify version was incremented
    assert updated_agent.current_version == 2


@pytest.mark.asyncio
async def test_automated_agent_constraints_on_update(
    pg_session: AsyncSession,
    test_user_db: User,
    sub_agent_service: SubAgentService,
):
    """Test that AUTOMATED agent constraints are enforced on update."""
    # Create an AUTOMATED agent
    create_data = SubAgentCreate(
        name="Test Automated Agent",
        type=SubAgentType.AUTOMATED,
        is_public=False,
        description="Test agent",
        model="claude-sonnet-4.6",
        system_prompt="Short prompt.",
        mcp_tools=["tool1"],
        enable_thinking=False,
    )

    agent = await sub_agent_service.create_sub_agent(
        db=pg_session,
        data=create_data,
        actor=test_user_db,
    )

    # Try to update with too long system_prompt
    update_data = SubAgentUpdate(
        description="Test",
        system_prompt="x" * 501,  # Too long
        change_summary="Update",
    )

    with pytest.raises(ValueError, match="system_prompt must be ≤ 500 characters"):
        await sub_agent_service.update_sub_agent(
            db=pg_session,
            sub_agent_id=agent.id,
            data=update_data,
            actor=test_user_db,
        )

    # Try to update with too many MCP tools
    update_data = SubAgentUpdate(
        description="Test",
        mcp_tools=["tool1", "tool2", "tool3", "tool4"],  # Too many
        change_summary="Update",
    )

    with pytest.raises(ValueError, match="may reference at most 3 MCP tools"):
        await sub_agent_service.update_sub_agent(
            db=pg_session,
            sub_agent_id=agent.id,
            data=update_data,
            actor=test_user_db,
        )

    # Try to make it public
    update_data = SubAgentUpdate(
        description="Test",
        is_public=True,
        change_summary="Update",
    )

    with pytest.raises(ValueError, match="must be private"):
        await sub_agent_service.update_sub_agent(
            db=pg_session,
            sub_agent_id=agent.id,
            data=update_data,
            actor=test_user_db,
        )
