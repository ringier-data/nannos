"""Tests for bulk activation/deactivation methods."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.models.sub_agent import ActivationSource
from playground_backend.models.user import User
from playground_backend.repositories.sub_agent_repository import SubAgentRepository
from playground_backend.services.audit_service import AuditService


@pytest.mark.asyncio
async def test_bulk_activate_sub_agent_group_activation(pg_session: AsyncSession, test_user: User):
    """Test bulk activation with group tracking."""
    repo = SubAgentRepository()
    audit_service = AuditService()
    repo.set_audit_service(audit_service)

    # Create test users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES
                ('owner-123', 'owner-123', 'owner@test.com', 'Test', 'Owner', 'member'),
                ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member'),
                ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member'),
                ('user-3', 'user-3', 'user3@test.com', 'User', 'Three', 'member'),
                ('admin-123', 'admin-123', 'admin@test.com', 'Admin', 'User', 'admin')
        """)
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_user_id, current_version, default_version)
            VALUES (99, 'Test Agent', 'remote', 'owner-123', 1, 1)
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO sub_agent_config_versions (sub_agent_id, version, version_hash, status, description, change_summary, system_prompt)
            VALUES (99, 1, 'hash123', 'approved', 'Test agent description', 'Initial version', 'Test prompt')
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name)
            VALUES (1, 'Test Group')
        """)
    )
    await pg_session.commit()

    # Bulk activate for multiple users
    user_ids = ["user-1", "user-2", "user-3"]
    user_ids = await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=99,
        activated_by=ActivationSource.GROUP,
        group_id=1,
    )
    await pg_session.commit()

    # Verify activations
    assert len(user_ids) == 3

    result = await pg_session.execute(
        text("""
            SELECT user_id, activated_by, activated_by_groups
            FROM user_sub_agent_activations
            WHERE sub_agent_id = 99
            ORDER BY user_id
        """)
    )
    rows = result.fetchall()

    assert len(rows) == 3
    for i, row in enumerate(rows):
        assert row[0] == user_ids[i]
        assert row[1] == "group"
        assert row[2] == [1]  # JSONB array with group_id as integer


@pytest.mark.asyncio
async def test_bulk_activate_sub_agent_user_activation(pg_session: AsyncSession, test_user: User):
    """Test bulk activation for user-initiated activation."""
    repo = SubAgentRepository()
    audit_service = AuditService()
    repo.set_audit_service(audit_service)

    # Create test users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES 
                ('owner-123', 'owner-123', 'owner@test.com', 'Test', 'Owner', 'member'),
                ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member')
        """)
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_user_id, current_version, default_version)
            VALUES (98, 'Test Agent 2', 'remote', 'owner-123', 1, 1)
        """)
    )
    await pg_session.commit()

    # Bulk activate for single user (common case from API)
    user_ids = await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=["user-1"],
        sub_agent_id=98,
        activated_by=ActivationSource.USER,
    )
    await pg_session.commit()

    # Verify activation
    assert len(user_ids) == 1

    result = await pg_session.execute(
        text("""
            SELECT activated_by, activated_by_groups
            FROM user_sub_agent_activations
            WHERE user_id = 'user-1' AND sub_agent_id = 98
        """)
    )
    row = result.fetchone()

    assert row[0] == "user"
    assert row[1] is None  # No groups for user activation


@pytest.mark.asyncio
async def test_bulk_activate_handles_duplicates(pg_session: AsyncSession, test_user: User):
    """Test bulk activation handles conflicts (idempotency)."""
    repo = SubAgentRepository()
    audit_service = AuditService()
    repo.set_audit_service(audit_service)

    # Create test users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES 
                ('owner-123', 'owner-123', 'owner@test.com', 'Test', 'Owner', 'member'),
                ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member'),
                ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member'),
                ('admin-123', 'admin-123', 'admin@test.com', 'Admin', 'User', 'admin')
        """)
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_user_id, current_version, default_version)
            VALUES (97, 'Test Agent 3', 'remote', 'owner-123', 1, 1)
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name)
            VALUES (2, 'Test Group 2')
        """)
    )
    await pg_session.commit()

    user_ids = ["user-1", "user-2"]

    # First activation
    user_ids_activated1 = await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=97,
        activated_by=ActivationSource.GROUP,
        group_id=2,
    )
    await pg_session.commit()

    # Second activation (should update, not fail)
    user_ids_activated2 = await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=97,
        activated_by=ActivationSource.GROUP,
        group_id=2,
    )
    await pg_session.commit()

    assert len(user_ids_activated1) == 2
    assert len(user_ids_activated2) == 0  # No affected rows on second call


@pytest.mark.asyncio
async def test_bulk_deactivate_sub_agent_removes_group(pg_session: AsyncSession, test_user: User):
    """Test bulk deactivation removes group from JSONB array."""
    repo = SubAgentRepository()
    audit_service = AuditService()
    repo.set_audit_service(audit_service)

    # Create test users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES
                ('owner-123', 'owner-123', 'owner@test.com', 'Test', 'Owner', 'member'),
                ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member'),
                ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member'),
                ('admin-123', 'admin-123', 'admin@test.com', 'Admin', 'User', 'admin')
        """)
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_user_id, current_version, default_version)
            VALUES (4, 'Test Agent 4', 'remote', 'owner-123', 1, 1)
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name)
            VALUES (3, 'Test Group 3'),
                   (4, 'Test Group 4')
        """)
    )
    await pg_session.commit()

    user_ids = ["user-1", "user-2"]

    # Activate from group 3
    await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=4,
        activated_by=ActivationSource.GROUP,
        group_id=3,
    )

    # Activate from group 4 (adds to array)
    await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=4,
        activated_by=ActivationSource.GROUP,
        group_id=4,
    )
    await pg_session.commit()

    # Verify both groups in array
    result = await pg_session.execute(
        text("""
            SELECT activated_by_groups
            FROM user_sub_agent_activations
            WHERE user_id = 'user-1' AND sub_agent_id = 4
        """)
    )
    groups = result.scalar()
    assert set(groups) == {3, 4}  # JSONB stores as integers

    # Deactivate from group 3
    user_ids_deactivated = await repo.bulk_deactivate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=4,
        group_id=3,
    )
    await pg_session.commit()

    assert len(user_ids_deactivated) == 2  # Updated 2 rows

    # Verify group 3 removed, group 4 remains
    result = await pg_session.execute(
        text("""
            SELECT activated_by_groups
            FROM user_sub_agent_activations
            WHERE user_id = 'user-1' AND sub_agent_id = 4
        """)
    )
    groups = result.scalar()
    assert groups == [4]  # JSONB stores as integers


@pytest.mark.asyncio
async def test_bulk_deactivate_deletes_when_last_group(pg_session: AsyncSession, test_user: User):
    """Test bulk deactivation deletes row when removing last group."""
    repo = SubAgentRepository()
    audit_service = AuditService()
    repo.set_audit_service(audit_service)

    # Create test users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES
                ('owner-123', 'owner-123', 'owner@test.com', 'Test', 'Owner', 'member'),
                ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member'),
                ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member'),
                ('admin-123', 'admin-123', 'admin@test.com', 'Admin', 'User', 'admin')
        """)
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_user_id, current_version, default_version)
            VALUES (5, 'Test Agent 5', 'remote', 'owner-123', 1, 1)
        """)
    )
    await pg_session.execute(
        text("""
            INSERT INTO user_groups (id, name)
            VALUES (5, 'Test Group 5')
        """)
    )
    await pg_session.commit()

    user_ids = ["user-1", "user-2"]

    # Activate from single group
    await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=5,
        activated_by=ActivationSource.GROUP,
        group_id=5,
    )
    await pg_session.commit()

    # Deactivate (should delete)
    user_ids_deactivated = await repo.bulk_deactivate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=5,
        group_id=5,
    )
    await pg_session.commit()

    assert len(user_ids_deactivated) == 2

    # Verify deleted
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_sub_agent_activations
            WHERE sub_agent_id = 5
        """)
    )
    assert result.scalar() == 0


@pytest.mark.asyncio
async def test_bulk_deactivate_full_deactivation(pg_session: AsyncSession, test_user: User):
    """Test bulk deactivation without group_id (full delete)."""
    repo = SubAgentRepository()
    audit_service = AuditService()
    repo.set_audit_service(audit_service)

    # Create test users
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES 
                ('owner-123', 'owner-123', 'owner@test.com', 'Test', 'Owner', 'member'),
                ('user-1', 'user-1', 'user1@test.com', 'User', 'One', 'member'),
                ('user-2', 'user-2', 'user2@test.com', 'User', 'Two', 'member')
        """)
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_user_id, current_version, default_version)
            VALUES (6, 'Test Agent 6', 'remote', 'owner-123', 1, 1)
        """)
    )
    await pg_session.commit()

    user_ids = ["user-1", "user-2"]

    # Activate as user
    await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=6,
        activated_by=ActivationSource.USER,
    )
    await pg_session.commit()

    # Full deactivation
    user_ids_deactivated = await repo.bulk_deactivate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=user_ids,
        sub_agent_id=6,
        group_id=None,  # Full delete
    )
    await pg_session.commit()

    assert len(user_ids_deactivated) == 2

    # Verify deleted
    result = await pg_session.execute(
        text("""
            SELECT COUNT(*) FROM user_sub_agent_activations
            WHERE sub_agent_id = 6
        """)
    )
    assert result.scalar() == 0


@pytest.mark.asyncio
async def test_bulk_methods_with_empty_list(pg_session: AsyncSession, test_user: User):
    """Test bulk methods handle empty user list gracefully."""
    repo = SubAgentRepository()

    # Create test user
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role)
            VALUES ('owner-123', 'owner-123', 'owner@test.com', 'Test', 'Owner', 'member')
        """)
    )

    # Create test data
    await pg_session.execute(
        text("""
            INSERT INTO sub_agents (id, name, type, owner_user_id, current_version, default_version)
            VALUES (7, 'Test Agent 7', 'remote', 'owner-123', 1, 1)
        """)
    )
    await pg_session.commit()

    # Empty activation
    user_ids_activated = await repo.bulk_activate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=[],
        sub_agent_id=7,
        activated_by=ActivationSource.GROUP,
        group_id=1,
    )
    assert len(user_ids_activated) == 0

    # Empty deactivation
    user_ids_deactivated = await repo.bulk_deactivate_sub_agent(
        db=pg_session,
        actor=test_user,
        user_ids=[],
        sub_agent_id=7,
        group_id=1,
    )
    assert len(user_ids_deactivated) == 0
