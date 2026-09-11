"""Embed activation as access grant (ADR-0006), against a real database.

`bind_connection` writes only a `user_sub_agent_activations` row (`activated_by = embed`).
The orchestrator builds a user's sub-agents from `get_accessible_sub_agents(activated_only=True)`,
whose non-admin WHERE clause needs owned OR public OR group-assigned. A private sub-agent
created by the binding admin satisfies none of those for anyone else, so without the
`embed` grant every embedded turn failed for every regular user."""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.sub_agent import (
    ActivationSource,
    SubAgentCreate,
    SubAgentStatus,
    SubAgentType,
)
from console_backend.models.user import User
from console_backend.services.sub_agent_service import SubAgentService


@pytest.mark.asyncio
async def test_embed_activation_makes_a_private_sub_agent_reachable_for_a_non_owner(
    sub_agent_service: SubAgentService,
    pg_session: AsyncSession,
    test_admin_user_db: User,
    test_user_db: User,
):
    # The binding admin creates the private sub-agent (like create_bound_sub_agent does).
    agent = await sub_agent_service.create_sub_agent(
        pg_session,
        SubAgentCreate(
            name="Alloy-AI-Assistant",
            type=SubAgentType.LOCAL,
            description="Helps with campaigns.",
            model="gpt-4o",
            system_prompt="Domain guidance.",
            is_public=False,
        ),
        test_admin_user_db,
    )
    assert agent.default_version == 1 and agent.is_public is False

    async def activated_for(user: User) -> list[int]:
        rows = await sub_agent_service.get_accessible_sub_agents(
            pg_session,
            user.id,
            is_admin=False,
            status_filter=SubAgentStatus.APPROVED,
            activated_only=True,
        )
        return [sa.id for sa in rows]

    # Not owned, not public, no group: invisible to a regular user, activated or not.
    assert agent.id not in await activated_for(test_user_db)

    # What bind_connection does on the user's first embedded connect.
    await sub_agent_service.repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_admin_user_db,
        user_ids=[test_user_db.id],
        sub_agent_id=agent.id,
        activated_by=ActivationSource.EMBED,
    )

    assert agent.id in await activated_for(test_user_db)
    (visible,) = [
        sa
        for sa in await sub_agent_service.get_accessible_sub_agents(
            pg_session, test_user_db.id, is_admin=False, activated_only=True
        )
        if sa.id == agent.id
    ]
    assert visible.is_activated is True
    assert visible.activated_by == ActivationSource.EMBED
    assert visible.effective_permission == "read"

    # The grant follows the row: in the console list too (activated_only=False), and
    # never for a third user who did not arrive through the host.
    console_list = await sub_agent_service.get_accessible_sub_agents(
        pg_session, test_user_db.id, is_admin=False
    )
    assert agent.id in [sa.id for sa in console_list]
    assert agent.id not in [
        sa.id
        for sa in await sub_agent_service.get_accessible_sub_agents(
            pg_session, "someone-else", is_admin=False
        )
    ]
