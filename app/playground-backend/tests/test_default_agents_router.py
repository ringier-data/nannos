"""Integration tests for group default agent endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_group_accessible_agents_empty(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test getting accessible agents when there are none."""
    # Create group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, :user_id, 'manager')
        """),
        {"user_id": test_user_model.id},
    )
    await pg_session.commit()

    response = await client_with_db.get("/api/v1/groups/1/accessible-agents")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_group_accessible_agents(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test getting accessible agents for a group with default status indicators."""
    # Create group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, :user_id, 'manager')
        """),
        {"user_id": test_user_model.id},
    )

    # Create sub-agents with approved versions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id, default_version, current_version)
            VALUES 
                (123, 'Agent 1', 'remote', 'active', :owner_user_id, 1, 1),
                (456, 'Agent 2', 'remote', 'active', :owner_user_id, 1, 1),
                (789, 'Agent 3', 'remote', 'active', :owner_user_id, 1, 1)
        """),
        {"owner_user_id": test_user_model.id},
    )

    # Add config versions (approved)
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions 
                (sub_agent_id, version, description, status, created_at, release_number, agent_url)
            VALUES 
                (123, 1, 'Agent 1 description', 'approved', NOW(), 1, 'http://agent1.url'),
                (456, 1, 'Agent 2 description', 'approved', NOW(), 1, 'http://agent2.url'),
                (789, 1, 'Agent 3 description', 'approved', NOW(), 1, 'http://agent3.url')
        """)
    )

    # Grant group permissions to all three agents
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (user_group_id, sub_agent_id, permissions)
            VALUES 
                (1, 123, ARRAY['read', 'write']),
                (1, 456, ARRAY['read', 'write']),
                (1, 789, ARRAY['read', 'write'])
        """)
    )

    # Set only agents 123 and 456 as defaults
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 123, :created_by_user_id), (1, 456, :created_by_user_id)
        """),
        {"created_by_user_id": test_user_model.id},
    )
    await pg_session.commit()

    response = await client_with_db.get("/api/v1/groups/1/accessible-agents")

    assert response.status_code == 200
    data = response.json()
    # Should return ALL 3 accessible agents
    assert len(data) == 3
    assert {agent["id"] for agent in data} == {123, 456, 789}

    # Verify status fields for all agents
    for agent in data:
        assert "approval_status" in agent
        assert agent["approval_status"] == "approved"
        assert "is_activated" in agent
        assert "activated_by_groups" in agent
        assert "is_default" in agent

    # Verify is_default flag correctly indicates which are defaults
    agents_by_id = {agent["id"]: agent for agent in data}
    assert agents_by_id[123]["is_default"] is True  # Default
    assert agents_by_id[456]["is_default"] is True  # Default
    assert agents_by_id[789]["is_default"] is False  # Not default


@pytest.mark.asyncio
async def test_get_group_accessible_agents_requires_membership(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test that getting accessible agents requires group membership."""
    # Create group without adding current user
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.commit()

    response = await client_with_db.get("/api/v1/groups/1/accessible-agents")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_set_group_default_agents(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test setting default agents for a group."""
    # Create group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, :user_id, 'manager')
        """),
        {"user_id": test_user_model.id},
    )

    # Create sub-agents with approved versions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id, default_version)
            VALUES 
                (123, 'Agent 1', 'remote', 'active', :owner_user_id, 1),
                (456, 'Agent 2', 'remote', 'active', :owner_user_id, 1)
        """),
        {"owner_user_id": test_user_model.id},
    )

    # Add approved config versions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions 
                (sub_agent_id, version, description, agent_url, status, approved_at, release_number)
            VALUES 
                (123, 1, 'Agent 1 v1', 'https://example.com/agent1', 'approved', NOW(), 1),
                (456, 1, 'Agent 2 v1', 'https://example.com/agent2', 'approved', NOW(), 1)
        """)
    )

    # Grant group permissions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (user_group_id, sub_agent_id, permissions)
            VALUES 
                (1, 123, ARRAY['read', 'write']),
                (1, 456, ARRAY['read', 'write'])
        """)
    )
    await pg_session.commit()

    # Set default agents
    response = await client_with_db.put(
        "/api/v1/groups/1/default-agents",
        json={"sub_agent_ids": [123, 456]},
    )

    assert response.status_code == 200

    # Verify in database
    result = await pg_session.execute(
        text("""
            SELECT sub_agent_id FROM user_group_default_agents
            WHERE user_group_id = 1
            ORDER BY sub_agent_id
        """)
    )
    agent_ids = [row[0] for row in result.fetchall()]
    assert agent_ids == [123, 456]


@pytest.mark.asyncio
async def test_set_group_default_agents_activates_for_existing_members(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test that setting default agents activates them for all existing members."""
    # Create another user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('other-user', 'other-sub', 'other@test.com', 'Other', 'User', 'member')
        """)
    )

    # Create group with two members
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES 
                (1, :user_id, 'manager'),
                (1, 'other-user', 'write')
        """),
        {"user_id": test_user_model.id},
    )

    # Create sub-agent with approved version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id, default_version)
            VALUES (123, 'Agent 1', 'remote', 'active', :owner_user_id, 1)
        """),
        {"owner_user_id": test_user_model.id},
    )

    # Add approved config version
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions 
                (sub_agent_id, version, description, agent_url, status, approved_at, release_number)
            VALUES (123, 1, 'Agent 1 v1', 'https://example.com/agent', 'approved', NOW(), 1)
        """)
    )

    # Grant group permissions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (user_group_id, sub_agent_id, permissions)
            VALUES (1, 123, ARRAY['read', 'write'])
        """)
    )
    await pg_session.commit()

    # Set default agents
    response = await client_with_db.put(
        "/api/v1/groups/1/default-agents",
        json={"sub_agent_ids": [123]},
    )

    assert response.status_code == 200

    # Verify activations for both members
    result = await pg_session.execute(
        text("""
            SELECT user_id, activated_by_groups FROM user_sub_agent_activations
            WHERE sub_agent_id = 123
            ORDER BY user_id
        """)
    )
    rows = result.fetchall()

    assert len(rows) == 2
    assert rows[0][0] == "other-user"
    assert rows[0][1] == [1]
    assert rows[1][0] == test_user_model.id
    assert rows[1][1] == [1]


@pytest.mark.asyncio
async def test_set_group_default_agents_requires_manager_role(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test that setting default agents requires manager role."""
    # Create group with read role
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, :user_id, 'read')
        """),
        {"user_id": test_user_model.id},
    )

    # Create sub-agent
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id)
            VALUES (123, 'Agent 1', 'remote', 'active', :owner_user_id)
        """),
        {"owner_user_id": test_user_model.id},
    )
    await pg_session.commit()

    # Try to set default agents
    response = await client_with_db.put(
        "/api/v1/groups/1/default-agents",
        json={"sub_agent_ids": [123]},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_add_default_agents(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test adding default agents to a group."""
    # Create group
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, :user_id, 'manager')
        """),
        {"user_id": test_user_model.id},
    )

    # Create sub-agents
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id)
            VALUES 
                (123, 'Agent 1', 'remote', 'active', :owner_user_id),
                (456, 'Agent 2', 'remote', 'active', :owner_user_id)
        """),
        {"owner_user_id": test_user_model.id},
    )

    # Grant group permissions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (user_group_id, sub_agent_id, permissions)
            VALUES 
                (1, 123, ARRAY['read', 'write']),
                (1, 456, ARRAY['read', 'write'])
        """)
    )

    # Set initial default agent
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 123, :created_by_user_id)
        """),
        params={"created_by_user_id": test_user_model.id},
    )
    await pg_session.commit()

    # Add another default agent
    response = await client_with_db.post(
        "/api/v1/groups/1/default-agents/456",
    )
    assert response.status_code == 200

    # Verify in database
    result = await pg_session.execute(
        text("""
            SELECT sub_agent_id FROM user_group_default_agents
            WHERE user_group_id = 1
            ORDER BY sub_agent_id
        """)
    )
    agent_ids = [row[0] for row in result.fetchall()]
    assert agent_ids == [123, 456]


@pytest.mark.asyncio
async def test_remove_default_agents(client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model):
    """Test removing default agents from a group."""
    # Create another user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('other-user', 'other-sub', 'other@test.com', 'Other', 'User', 'member')
        """)
    )

    # Create group with members
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES 
                (1, :user_id, 'manager'),
                (1, 'other-user', 'write')
        """),
        {"user_id": test_user_model.id},
    )

    # Create sub-agents
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id)
            VALUES 
                (123, 'Agent 1', 'remote', 'active', :owner_user_id),
                (456, 'Agent 2', 'remote', 'active', :owner_user_id)
        """),
        {"owner_user_id": test_user_model.id},
    )

    # Set default agents
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 123, :created_by_user_id), (1, 456, :created_by_user_id)
        """),
        params={"created_by_user_id": test_user_model.id},
    )

    # Activate for both members
    await pg_session.execute(
        text("""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_by, activated_by_groups)
            VALUES 
                (:user_id, 123, 'group', '[1]'::jsonb),
                (:user_id, 456, 'group', '[1]'::jsonb),
                ('other-user', 123, 'group', '[1]'::jsonb),
                ('other-user', 456, 'group', '[1]'::jsonb)
        """),
        {"user_id": test_user_model.id},
    )
    await pg_session.commit()

    # Remove one default agent
    response = await client_with_db.delete("/api/v1/groups/1/default-agents/123")

    assert response.status_code == 200

    # Verify deactivated for both members
    result = await pg_session.execute(
        text("""
            SELECT user_id FROM user_sub_agent_activations
            WHERE sub_agent_id = 123
        """)
    )
    assert result.fetchall() == []  # Should be deleted for both users


@pytest.mark.asyncio
async def test_remove_default_agents_preserves_multi_group_activations(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test that removing default agent only removes the group from activated_by_groups."""
    # Create two groups
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES 
                (1, 'Test Group 1', 'Description 1'),
                (2, 'Test Group 2', 'Description 2')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES 
                (1, :user_id, 'manager'),
                (2, :user_id, 'manager')
        """),
        {"user_id": test_user_model.id},
    )

    # Create sub-agent
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id)
            VALUES (123, 'Agent 1', 'remote', 'active', :owner_user_id)
        """),
        {"owner_user_id": test_user_model.id},
    )

    # Set as default in both groups
    await pg_session.execute(
        text("""
            INSERT INTO user_group_default_agents (user_group_id, sub_agent_id, created_by_user_id)
            VALUES (1, 123, :created_by_user_id), (2, 123, :created_by_user_id)
        """),
        params={"created_by_user_id": test_user_model.id},
    )

    # Activate for both groups
    await pg_session.execute(
        text("""
            INSERT INTO user_sub_agent_activations (user_id, sub_agent_id, activated_by, activated_by_groups)
            VALUES (:user_id, 123, 'group', '[1,2]'::jsonb)
        """),
        {"user_id": test_user_model.id},
    )
    await pg_session.commit()

    # Remove from group-1
    response = await client_with_db.delete("/api/v1/groups/1/default-agents/123")

    assert response.status_code == 200

    # Verify still activated by group-2
    result = await pg_session.execute(
        text("""
            SELECT activated_by_groups FROM user_sub_agent_activations
            WHERE user_id = :user_id AND sub_agent_id = 123
        """),
        {"user_id": test_user_model.id},
    )
    row = result.first()
    assert row is not None
    assert row[0] == [2]


@pytest.mark.asyncio
async def test_default_agent_endpoints_require_authentication(client: AsyncClient):
    """Test that all default agent endpoints require authentication."""
    # Get accessible agents
    response = await client.get("/api/v1/groups/1/accessible-agents")
    assert response.status_code == 401

    # Set default agents
    response = await client.put(
        "/api/v1/groups/1/default-agents",
        json={"sub_agent_ids": [123]},
    )
    assert response.status_code == 401

    # Remove default agent
    response = await client.delete("/api/v1/groups/1/default-agents/123")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_set_non_approved_agent_as_default_deferred_activation(
    client_with_db: AsyncClient, pg_session: AsyncSession, test_user_model
):
    """Test that non-approved agents can be set as defaults but activation is deferred until approval."""
    # Create group with member
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name, description)
            VALUES (1, 'Test Group', 'Description')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_group_members (user_group_id, user_id, group_role)
            VALUES (1, :user_id, 'manager')
        """),
        {"user_id": test_user_model.id},
    )

    # Create non-approved sub-agent (draft status, no default_version)
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_status, owner_user_id, current_version)
            VALUES (123, 'Draft Agent', 'remote', 'active', :owner_user_id, 1)
        """),
        {"owner_user_id": test_user_model.id},
    )

    # Add draft config version (not approved)
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions 
                (sub_agent_id, version, description, status, created_at, agent_url)
            VALUES (123, 1, 'Draft version', 'draft', NOW(), 'http://draft.agent.url')
        """)
    )

    # Grant group permissions
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_permissions (user_group_id, sub_agent_id, permissions)
            VALUES (1, 123, ARRAY['read', 'write'])
        """)
    )
    await pg_session.commit()

    # Set non-approved agent as default - should succeed
    response = await client_with_db.put(
        "/api/v1/groups/1/default-agents",
        json={"sub_agent_ids": [123]},
    )

    assert response.status_code == 200

    # Verify it's set as default in database
    result = await pg_session.execute(
        text("""
            SELECT sub_agent_id FROM user_group_default_agents
            WHERE user_group_id = 1
        """)
    )
    assert result.scalar() == 123

    # Verify it's NOT activated for the user
    activation_result = await pg_session.execute(
        text("""
            SELECT user_id FROM user_sub_agent_activations
            WHERE sub_agent_id = 123 AND user_id = :user_id
        """),
        {"user_id": test_user_model.id},
    )
    assert activation_result.first() is None  # No activation record
