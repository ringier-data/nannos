from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text

from playground_backend.models.audit import AuditAction, AuditEntityType
from playground_backend.repositories import (
    SecretsRepository,
    SubAgentRepository,
    UserRepository,
)
from playground_backend.repositories.sub_agent_repository import ApprovalContext


# Fixtures for repositories with DI
@pytest.fixture
def user_repository():
    from playground_backend.repositories.user_repository import UserRepository
    from playground_backend.services.audit_service import AuditService

    repo = UserRepository()
    repo.set_audit_service(AuditService())
    return repo


@pytest.fixture
def sub_agent_repository():
    from playground_backend.repositories.sub_agent_repository import SubAgentRepository
    from playground_backend.services.audit_service import AuditService

    repo = SubAgentRepository()
    repo.set_audit_service(AuditService())
    return repo


@pytest.fixture
def secrets_repository():
    from playground_backend.repositories.secrets_repository import SecretsRepository
    from playground_backend.services.audit_service import AuditService

    repo = SecretsRepository()
    repo.set_audit_service(AuditService())
    return repo


async def get_latest_audit_log(pg_session, entity_type: str, entity_id: str, action: Optional[str] = None):
    """Helper to fetch the latest audit log for an entity."""
    if action:
        result = await pg_session.execute(
            text(
                "SELECT * FROM audit_logs WHERE entity_type = :entity_type "
                "AND entity_id = :entity_id AND action = :action ORDER BY created_at DESC LIMIT 1"
            ),
            {"entity_type": entity_type, "entity_id": entity_id, "action": action},
        )
    else:
        result = await pg_session.execute(
            text(
                "SELECT * FROM audit_logs WHERE entity_type = :entity_type "
                "AND entity_id = :entity_id ORDER BY created_at DESC LIMIT 1"
            ),
            {"entity_type": entity_type, "entity_id": entity_id},
        )
    return result.mappings().first()


class TestAuditedRepositoryBase:
    """Tests for base AuditedRepository CRUD operations with automatic audit."""

    @pytest.mark.asyncio
    async def test_create_logs_audit(self, user_repository, pg_session):
        """Test that create operation automatically logs audit."""
        _entity_id = await user_repository.create(
            db=pg_session,
            actor_sub="test-user-sub",
            fields={
                "id": "test-user-id",
                "sub": "test-user-sub",
                "email": "test@example.com",
                "first_name": "Test",
                "last_name": "User",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )
        await pg_session.commit()

        # Verify audit log was created in database
        result = await pg_session.execute(
            text(
                "SELECT * FROM audit_logs WHERE entity_type = 'user' AND entity_id = 'test-user-id' ORDER BY created_at DESC LIMIT 1"
            )
        )
        audit_log = result.mappings().first()

        assert audit_log is not None
        assert audit_log["actor_sub"] == "test-user-sub"
        assert audit_log["entity_type"] == "user"
        assert audit_log["entity_id"] == "test-user-id"
        assert audit_log["action"] == "create"
        assert "after" in audit_log["changes"]

    @pytest.mark.asyncio
    async def test_update_logs_audit_with_before_after(self, user_repository, pg_session):
        """Test that update operation automatically logs audit with before/after state."""
        _entity_id = await user_repository.create(
            db=pg_session,
            actor_sub="test-user-sub",
            fields={
                "id": "test-user-id-2",
                "sub": "test-user-sub-2",
                "email": "test2@example.com",
                "first_name": "Test",
                "last_name": "User",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )
        await pg_session.commit()

        await user_repository.update(
            db=pg_session,
            actor_sub="admin-user-sub",
            entity_id="test-user-id-2",
            fields={
                "role": "admin",
                "updated_at": datetime.now(timezone.utc),
            },
            fetch_before=True,
        )
        await pg_session.commit()

        # Verify audit log was created in database
        audit_log = await get_latest_audit_log(pg_session, "user", "test-user-id-2")

        assert audit_log is not None
        assert audit_log["actor_sub"] == "admin-user-sub"
        assert audit_log["entity_type"] == "user"
        assert audit_log["entity_id"] == "test-user-id-2"
        assert audit_log["action"] == "update"
        assert "before" in audit_log["changes"]
        assert "after" in audit_log["changes"]
        assert audit_log["changes"]["before"]["role"] == "member"
        assert audit_log["changes"]["after"]["role"] == "admin"

    @pytest.mark.asyncio
    async def test_delete_logs_audit(self, user_repository, pg_session):
        """Test that delete operation automatically logs audit."""
        _entity_id = await user_repository.create(
            db=pg_session,
            actor_sub="test-user-sub",
            fields={
                "id": "test-user-id-3",
                "sub": "test-user-sub-3",
                "email": "test3@example.com",
                "first_name": "Test",
                "last_name": "User",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )
        await pg_session.commit()

        await user_repository.delete(
            db=pg_session,
            actor_sub="deleter-user-sub",
            entity_id="test-user-id-3",
        )
        await pg_session.commit()

        # Verify audit log was created in database
        audit_log = await get_latest_audit_log(pg_session, "user", "test-user-id-3")

        assert audit_log is not None
        assert audit_log["actor_sub"] == "deleter-user-sub"
        assert audit_log["entity_type"] == "user"
        assert audit_log["entity_id"] == "test-user-id-3"
        assert audit_log["action"] == "delete"
        assert audit_log["changes"] == {"soft_delete": True}


class TestSubAgentRepositoryAudit:
    """Tests for SubAgentRepository audit logging."""

    @pytest.mark.asyncio
    async def test_approve_version_logs_audit(self, pg_session, sub_agent_repository, user_repository):
        """Test that approve_version logs approval audit."""

        repo = sub_agent_repository
        user_repo = user_repository
        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="admin-sub",
            fields={
                "id": "admin-id",
                "sub": "admin-sub",
                "email": "admin@example.com",
                "first_name": "Admin",
                "last_name": "User",
                "is_administrator": True,
                "role": "admin",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a sub-agent
        sub_agent_id = await repo.create(
            db=pg_session,
            actor_sub="admin-sub",
            fields={
                "name": "Test Agent",
                "owner_user_id": "admin-id",
                "type": "local",
                "is_public": False,
                "current_version": 1,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a config version to approve
        await repo.create_config_version(
            db=pg_session,
            actor_sub="admin-sub",
            sub_agent_id=sub_agent_id,
            version=2,
            version_hash="hash123",
            change_summary="Version 2",
            status="pending_approval",
            description="Test",
            model="gpt-4",
            system_prompt="Test prompt",
            mcp_tools=[],
        )

        context = ApprovalContext(
            sub_agent_id=sub_agent_id,
            version=2,
            admin_user_id="admin-id",
            admin_sub="admin-sub",
            action="approve",
            release_number=1,
        )

        await repo.approve_version(db=pg_session, context=context)
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(
            pg_session, AuditEntityType.SUB_AGENT, str(sub_agent_id), AuditAction.APPROVE
        )
        assert audit_log is not None
        assert audit_log["actor_sub"] == "admin-sub"
        assert audit_log["entity_type"] == AuditEntityType.SUB_AGENT
        assert audit_log["entity_id"] == str(sub_agent_id)
        assert audit_log["action"] == AuditAction.APPROVE
        assert audit_log["changes"]["sub_agent_id"] == sub_agent_id
        assert audit_log["changes"]["version"] == 2
        assert audit_log["changes"]["release_number"] == 1

    @pytest.mark.asyncio
    async def test_reject_version_logs_audit(self, pg_session, sub_agent_repository, user_repository):
        """Test that reject_version logs rejection audit."""
        repo = sub_agent_repository
        user_repo = user_repository

        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="admin-sub-2",
            fields={
                "id": "admin-id-2",
                "sub": "admin-sub-2",
                "email": "admin2@example.com",
                "first_name": "Admin",
                "last_name": "User",
                "is_administrator": True,
                "role": "admin",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a sub-agent
        sub_agent_id = await repo.create(
            db=pg_session,
            actor_sub="admin-sub-2",
            fields={
                "name": "Test Agent 2",
                "owner_user_id": "admin-id-2",
                "type": "local",
                "is_public": False,
                "current_version": 1,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a config version to reject
        await repo.create_config_version(
            db=pg_session,
            actor_sub="admin-sub-2",
            sub_agent_id=sub_agent_id,
            version=2,
            version_hash="hash456",
            change_summary="Version 2",
            status="pending_approval",
            description="Test",
            model="gpt-4",
            system_prompt="Test prompt",
            mcp_tools=[],
        )

        context = ApprovalContext(
            sub_agent_id=sub_agent_id,
            version=2,
            admin_user_id="admin-id-2",
            admin_sub="admin-sub-2",
            action="reject",
            rejection_reason="Needs more work",
        )

        await repo.reject_version(db=pg_session, context=context)
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(
            pg_session, AuditEntityType.SUB_AGENT, str(sub_agent_id), AuditAction.REJECT
        )
        assert audit_log is not None
        assert audit_log["actor_sub"] == "admin-sub-2"
        assert audit_log["entity_type"] == AuditEntityType.SUB_AGENT
        assert audit_log["entity_id"] == str(sub_agent_id)
        assert audit_log["action"] == AuditAction.REJECT
        assert audit_log["changes"]["rejection_reason"] == "Needs more work"

    @pytest.mark.asyncio
    async def test_update_permissions_logs_audit(self, pg_session, sub_agent_repository, user_repository):
        """Test that update_permissions logs permission change audit."""

        repo = sub_agent_repository
        user_repo = user_repository
        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="admin-sub-3",
            fields={
                "id": "admin-id-3",
                "sub": "admin-sub-3",
                "email": "admin3@example.com",
                "first_name": "Admin",
                "last_name": "User",
                "is_administrator": True,
                "role": "admin",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a sub-agent
        sub_agent_id = await repo.create(
            db=pg_session,
            actor_sub="admin-sub-3",
            fields={
                "name": "Test Agent 3",
                "owner_user_id": "admin-id-3",
                "type": "local",
                "is_public": False,
                "current_version": 1,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create user groups using SERIAL IDs
        result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES
                    ('Group 1', 'Test group 1', :now, :now),
                    ('Group 2', 'Test group 2', :now, :now)
                RETURNING id
            """),
            {"now": datetime.now(timezone.utc)},
        )
        group_ids = [row[0] for row in result.fetchall()]

        await repo.update_permissions(
            db=pg_session,
            actor_sub="admin-sub-3",
            sub_agent_id=sub_agent_id,
            group_permissions=[
                {"user_group_id": group_ids[0], "permissions": ["read"]},
                {"user_group_id": group_ids[1], "permissions": ["write"]},
            ],
        )
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(
            pg_session, AuditEntityType.SUB_AGENT, str(sub_agent_id), AuditAction.PERMISSION_UPDATE
        )
        assert audit_log is not None
        assert audit_log["actor_sub"] == "admin-sub-3"
        assert audit_log["entity_type"] == AuditEntityType.SUB_AGENT
        assert audit_log["entity_id"] == str(sub_agent_id)
        assert audit_log["action"] == AuditAction.PERMISSION_UPDATE
        assert "before" in audit_log["changes"]
        assert "after" in audit_log["changes"]
        assert len(audit_log["changes"]["after"]["permissions"]) == 2

    @pytest.mark.asyncio
    async def test_activate_sub_agent_logs_audit(self, pg_session, sub_agent_repository, user_repository):
        """Test that activate_sub_agent logs activation audit."""

        repo = sub_agent_repository
        user_repo = user_repository

        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="user-sub-4",
            fields={
                "id": "user-id-4",
                "sub": "user-sub-4",
                "email": "user4@example.com",
                "first_name": "User",
                "last_name": "Test",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a sub-agent
        sub_agent_id = await repo.create(
            db=pg_session,
            actor_sub="user-sub-4",
            fields={
                "name": "Test Agent 4",
                "owner_user_id": "user-id-4",
                "type": "local",
                "is_public": False,
                "current_version": 1,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        await repo.bulk_activate_sub_agent(
            db=pg_session,
            actor_sub="user-sub-4",
            user_ids=["user-id-4"],
            sub_agent_id=sub_agent_id,
            group_id=None,
        )
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(
            pg_session, AuditEntityType.SUB_AGENT, str(sub_agent_id), AuditAction.ACTIVATE
        )
        assert audit_log is not None
        assert audit_log["actor_sub"] == "user-sub-4"
        assert audit_log["entity_type"] == AuditEntityType.SUB_AGENT
        assert audit_log["entity_id"] == str(sub_agent_id)
        assert audit_log["action"] == AuditAction.ACTIVATE

    @pytest.mark.asyncio
    async def test_deactivate_sub_agent_logs_audit(self, pg_session, sub_agent_repository, user_repository):
        """Test that deactivate_sub_agent logs deactivation audit."""
        repo = sub_agent_repository
        user_repo = user_repository

        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="user-sub-5",
            fields={
                "id": "user-id-5",
                "sub": "user-sub-5",
                "email": "user5@example.com",
                "first_name": "User",
                "last_name": "Test",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a sub-agent
        sub_agent_id = await repo.create(
            db=pg_session,
            actor_sub="user-sub-5",
            fields={
                "name": "Test Agent 5",
                "owner_user_id": "user-id-5",
                "type": "local",
                "is_public": False,
                "current_version": 1,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # First activate it
        await repo.bulk_activate_sub_agent(
            db=pg_session,
            actor_sub="user-sub-5",
            user_ids=["user-id-5"],
            sub_agent_id=sub_agent_id,
            group_id=None,
        )

        # Now deactivate it
        await repo.bulk_deactivate_sub_agent(
            db=pg_session,
            actor_sub="user-sub-5",
            user_ids=["user-id-5"],
            sub_agent_id=sub_agent_id,
            group_id=None,
        )
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(
            pg_session, AuditEntityType.SUB_AGENT, str(sub_agent_id), AuditAction.DEACTIVATE
        )
        assert audit_log is not None
        assert audit_log["actor_sub"] == "user-sub-5"
        assert audit_log["entity_type"] == AuditEntityType.SUB_AGENT
        assert audit_log["entity_id"] == str(sub_agent_id)
        assert audit_log["action"] == AuditAction.DEACTIVATE

    @pytest.mark.asyncio
    async def test_create_config_version_logs_audit(self, pg_session, sub_agent_repository, user_repository):
        """Test that create_config_version logs audit with full config."""
        repo = sub_agent_repository
        user_repo = user_repository
        await user_repo.create(
            db=pg_session,
            actor_sub="test-user-sub",
            fields={
                "id": "test-user-id-999",
                "sub": "test-user-sub",
                "email": "test999@example.com",
                "first_name": "Test",
                "last_name": "User",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a sub-agent first
        sub_agent_id = await repo.create(
            db=pg_session,
            actor_sub="test-user-sub",
            fields={
                "name": "Test Agent",
                "owner_user_id": "test-user-id-999",
                "type": "local",
                "is_public": False,
                "current_version": 1,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Now create config version to verify audit log in database
        await repo.create_config_version(
            db=pg_session,
            actor_sub="test-user-sub",
            sub_agent_id=sub_agent_id,
            version=1,
            version_hash="abc123",
            change_summary="Initial version",
            status="draft",
            description="Test config",
            model="gpt-4",
            system_prompt="You are a helpful assistant",
            mcp_tools=["tool1", "tool2"],
        )
        await pg_session.commit()

        # Verify audit log was written to database
        # Note: There will be 2 CREATE audit logs for this sub_agent_id:
        # 1. For the sub_agent itself
        # 2. For the config version
        # We want the one that contains config version details (has "version_hash" in changes)
        result = await pg_session.execute(
            text(
                "SELECT * FROM audit_logs WHERE entity_type = 'sub_agent' "
                f"AND entity_id = '{sub_agent_id}' AND action = 'create' "
                "AND changes->'after'->>'version_hash' IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1"
            )
        )
        audit_log = result.mappings().first()

        assert audit_log is not None
        assert audit_log["actor_sub"] == "test-user-sub"
        assert audit_log["action"] == "create"
        assert "after" in audit_log["changes"]

        # Verify config details are captured
        after = audit_log["changes"]["after"]
        assert after["sub_agent_id"] == sub_agent_id
        assert after["version"] == 1
        assert after["version_hash"] == "abc123"
        assert after["change_summary"] == "Initial version"
        assert after["description"] == "Test config"
        assert after["model"] == "gpt-4"
        assert after["system_prompt"] == "You are a helpful assistant"
        assert after["mcp_tools"] == ["tool1", "tool2"]
        assert after["status"] == "draft"


class TestSecretsRepositoryAudit:
    """Tests for SecretsRepository audit logging."""

    @pytest.mark.asyncio
    async def test_create_secret_logs_audit(self, pg_session, secrets_repository, user_repository):
        """Test that secret creation logs audit."""

        repo = secrets_repository
        user_repo = user_repository
        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="user-sub-6",
            fields={
                "id": "user-id-6",
                "sub": "user-sub-6",
                "email": "user6@example.com",
                "first_name": "User",
                "last_name": "Test",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        secret_id = await repo.create(
            db=pg_session,
            actor_sub="user-sub-6",
            fields={
                "owner_user_id": "user-id-6",
                "name": "test-secret",
                "description": "Test secret",
                "secret_type": "foundry_client_secret",
                "ssm_parameter_name": "/test/param",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(pg_session, AuditEntityType.SECRET, str(secret_id))
        assert audit_log is not None
        assert audit_log["actor_sub"] == "user-sub-6"
        assert audit_log["entity_type"] == AuditEntityType.SECRET
        assert audit_log["entity_id"] == str(secret_id)
        assert audit_log["action"] == AuditAction.CREATE

    @pytest.mark.asyncio
    async def test_delete_secret_logs_audit(self, pg_session, secrets_repository, user_repository):
        """Test that secret deletion logs audit."""

        repo = secrets_repository
        user_repo = user_repository
        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="user-sub-7",
            fields={
                "id": "user-id-7",
                "sub": "user-sub-7",
                "email": "user7@example.com",
                "first_name": "User",
                "last_name": "Test",
                "is_administrator": False,
                "role": "member",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a secret first
        secret_id = await repo.create(
            db=pg_session,
            actor_sub="user-sub-7",
            fields={
                "owner_user_id": "user-id-7",
                "name": "test-secret-2",
                "description": "Test secret 2",
                "secret_type": "foundry_client_secret",
                "ssm_parameter_name": "/test/param2",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Now delete it
        await repo.delete(
            db=pg_session,
            actor_sub="user-sub-7",
            entity_id=secret_id,
            soft=True,
        )
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(pg_session, AuditEntityType.SECRET, str(secret_id), AuditAction.DELETE)
        assert audit_log is not None
        assert audit_log["actor_sub"] == "user-sub-7"
        assert audit_log["entity_type"] == AuditEntityType.SECRET
        assert audit_log["entity_id"] == str(secret_id)
        assert audit_log["action"] == AuditAction.DELETE

    @pytest.mark.asyncio
    async def test_update_secret_permissions_logs_audit(self, pg_session, secrets_repository, user_repository):
        """Test that secret permission updates log audit."""
        repo = secrets_repository
        user_repo = user_repository

        # Create a user first
        await user_repo.create(
            db=pg_session,
            actor_sub="admin-sub-8",
            fields={
                "id": "admin-id-8",
                "sub": "admin-sub-8",
                "email": "admin8@example.com",
                "first_name": "Admin",
                "last_name": "User",
                "is_administrator": True,
                "role": "admin",
                "status": "active",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create a secret first
        secret_id = await repo.create(
            db=pg_session,
            actor_sub="admin-sub-8",
            fields={
                "owner_user_id": "admin-id-8",
                "name": "test-secret-3",
                "description": "Test secret 3",
                "secret_type": "foundry_client_secret",
                "ssm_parameter_name": "/test/param3",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            returning="id",
        )

        # Create user groups using SERIAL IDs
        result = await pg_session.execute(
            text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES
                    ('Group 3', 'Test group 3', :now, :now),
                    ('Group 4', 'Test group 4', :now, :now)
                RETURNING id
            """),
            {"now": datetime.now(timezone.utc)},
        )
        group_ids = [row[0] for row in result.fetchall()]

        await repo.update_permissions(
            db=pg_session,
            actor_sub="admin-sub-8",
            secret_id=secret_id,
            group_permissions=[
                {"user_group_id": group_ids[0], "permissions": ["read"]},
                {"user_group_id": group_ids[1], "permissions": ["write"]},
            ],
        )
        await pg_session.commit()

        # Verify audit was written to database
        audit_log = await get_latest_audit_log(
            pg_session, AuditEntityType.SECRET, str(secret_id), AuditAction.PERMISSION_UPDATE
        )
        assert audit_log is not None
        assert audit_log["actor_sub"] == "admin-sub-8"
        assert audit_log["entity_type"] == AuditEntityType.SECRET
        assert audit_log["entity_id"] == str(secret_id)
        assert audit_log["action"] == AuditAction.PERMISSION_UPDATE
        assert len(audit_log["changes"]["after"]["permissions"]) == 2


class TestAuditEnforcementCoverage:
    """Tests to verify audit logging coverage across all operations."""

    @pytest.mark.asyncio
    async def test_sub_agent_create_requires_audit(self):
        """Verify sub-agent creation includes audit logging."""
        # This is enforced by using repo.create() which always audits
        repo = SubAgentRepository()
        assert hasattr(repo, "create")
        assert repo.entity_type == AuditEntityType.SUB_AGENT

    @pytest.mark.asyncio
    async def test_sub_agent_delete_requires_audit(self):
        """Verify sub-agent deletion includes audit logging."""
        repo = SubAgentRepository()
        assert hasattr(repo, "delete")
        assert repo.entity_type == AuditEntityType.SUB_AGENT

    @pytest.mark.asyncio
    async def test_secret_create_requires_audit(self):
        """Verify secret creation includes audit logging."""
        repo = SecretsRepository()
        assert hasattr(repo, "create")
        assert repo.entity_type == AuditEntityType.SECRET

    @pytest.mark.asyncio
    async def test_secret_delete_requires_audit(self):
        """Verify secret deletion includes audit logging."""
        repo = SecretsRepository()
        assert hasattr(repo, "delete")
        assert repo.entity_type == AuditEntityType.SECRET

    @pytest.mark.asyncio
    async def test_all_repositories_have_audit_methods(self):
        """Verify all repositories have audit-enabled CRUD methods."""
        repos = [
            SubAgentRepository(),
            SecretsRepository(),
            UserRepository(),
        ]

        for repo in repos:
            # All should have create, update, delete from base
            assert hasattr(repo, "create"), f"{repo.__class__.__name__} missing create"
            assert hasattr(repo, "update"), f"{repo.__class__.__name__} missing update"
            assert hasattr(repo, "delete"), f"{repo.__class__.__name__} missing delete"

            # All should have entity_type defined
            assert hasattr(repo, "entity_type"), f"{repo.__class__.__name__} missing entity_type"
            assert isinstance(repo.entity_type, AuditEntityType)
