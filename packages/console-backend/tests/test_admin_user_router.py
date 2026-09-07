"""Integration tests for admin user management endpoints with real database."""

import os
from datetime import datetime, timezone

# Ensure code chooses auto credentials path during imports (avoid boto3 local credentials)
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
import pytest_asyncio
from console_backend.dependencies import require_admin
from console_backend.models.user import User, UserStatus
from sqlalchemy import text


@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session."""
    yield pg_session


@pytest.fixture
def admin_user_model():
    """Create an admin user model for auth override."""
    return User(
        id="admin-user-id",
        sub="admin-user-id",
        email="admin@example.com",
        first_name="Admin",
        last_name="User",
        is_administrator=True,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def non_admin_user_model():
    """Create a non-admin user model for auth override."""
    return User(
        id="non-admin-user-id",
        sub="non-admin-user-id",
        email="user@example.com",
        first_name="Regular",
        last_name="User",
        is_administrator=False,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def inserted_user(pg_session, admin_user_model, non_admin_user_model):
    """Insert a mock user into the database asynchronously."""
    for mock_user in [admin_user_model, non_admin_user_model]:
        await pg_session.execute(
            text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
            """),
            {
                "id": mock_user.id,
                "sub": mock_user.sub,
                "email": mock_user.email,
                "first_name": mock_user.first_name,
                "last_name": mock_user.last_name,
                "is_administrator": mock_user.is_administrator,
                "role": mock_user.role,
                "status": mock_user.status,
            },
        )
        await pg_session.commit()


@pytest_asyncio.fixture
async def admin_client(client_with_db, admin_user_model):
    """HTTP client_with_db with admin auth override."""

    def override_require_admin():
        return admin_user_model

    client_with_db._transport.app.dependency_overrides[require_admin] = override_require_admin
    yield client_with_db
    client_with_db._transport.app.dependency_overrides.pop(require_admin, None)


@pytest.mark.asyncio
class TestAdminUserPatchEndpoint:
    """Test PATCH /admin/users/{user_id} endpoint."""

    @pytest.mark.asyncio
    async def test_patch_user_set_administrator_true(self, admin_client, inserted_user, admin_user_model):
        """Test setting is_administrator to true."""

        response = await admin_client.patch(
            "/api/v1/admin/users/admin-user-id",
            json={"is_administrator": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["is_administrator"] is True

    async def test_patch_user_set_administrator_false(self, admin_client, inserted_user, admin_user_model):
        """Test setting is_administrator to false."""

        # Now set to false via API
        response = await admin_client.patch(
            "/api/v1/admin/users/admin-user-id",
            json={"is_administrator": False},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["is_administrator"] is False

    async def test_patch_user_not_found(self, admin_client):
        """Test patching a non-existent user returns 404."""
        response = await admin_client.patch(
            "/api/v1/admin/users/non-existent-user-id",
            json={"is_administrator": True},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    async def test_patch_user_empty_body(self, admin_client, inserted_user):
        """Test patching with empty body (no changes)."""
        response = await admin_client.patch(
            "/api/v1/admin/users/admin-user-id",
            json={},
        )

        assert response.status_code == 200
        # Should still return the user data
        data = response.json()
        assert "data" in data

    async def test_get_user_after_admin_update(self, admin_client, inserted_user):
        """Test GET user after PATCH shows updated values."""
        # Update user
        await admin_client.patch(
            "/api/v1/admin/users/non-admin-user-id",
            json={"is_administrator": True},
        )

        # Get user
        response = await admin_client.get("/api/v1/admin/users/non-admin-user-id")

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["is_administrator"] is True


@pytest.mark.asyncio
class TestAdminUserGroupsEndpoint:
    """Test PUT /admin/users/{id}/groups."""

    async def test_set_groups_refuses_suspended_user_without_removing_anything(
        self, admin_client, inserted_user, non_admin_user_model, pg_session
    ):
        """A suspended user cannot be added to a group, and the refusal happens before any removal."""
        keep_id, add_id = (
            await pg_session.execute(
                text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Current Group', '', NOW(), NOW()), ('New Group', '', NOW(), NOW())
                RETURNING id
                """)
            )
        ).scalars().all()
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'read', NOW())
            """),
            {"group_id": keep_id, "user_id": non_admin_user_model.id},
        )
        await pg_session.execute(
            text("UPDATE users SET status = 'suspended' WHERE id = :id"),
            {"id": non_admin_user_model.id},
        )
        await pg_session.commit()

        response = await admin_client.put(
            f"/api/v1/admin/users/{non_admin_user_model.id}/groups",
            json={"group_ids": [add_id], "operation": "set"},
        )
        assert response.status_code == 409

        # The membership it would have removed on the way is still there
        remaining = await pg_session.execute(
            text("SELECT user_group_id FROM user_group_members WHERE user_id = :uid"),
            {"uid": non_admin_user_model.id},
        )
        assert remaining.scalars().all() == [keep_id]

    async def test_remove_groups_still_works_for_suspended_user(
        self, admin_client, inserted_user, non_admin_user_model, pg_session
    ):
        """Suspension blocks additions only — an admin can still take groups away."""
        group_id = (
            await pg_session.execute(
                text("""
                INSERT INTO user_groups (name, description, created_at, updated_at)
                VALUES ('Leaving Group', '', NOW(), NOW())
                RETURNING id
                """)
            )
        ).scalar_one()
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role, created_at)
                VALUES (:group_id, :user_id, 'read', NOW())
            """),
            {"group_id": group_id, "user_id": non_admin_user_model.id},
        )
        await pg_session.execute(
            text("UPDATE users SET status = 'suspended' WHERE id = :id"),
            {"id": non_admin_user_model.id},
        )
        await pg_session.commit()

        response = await admin_client.put(
            f"/api/v1/admin/users/{non_admin_user_model.id}/groups",
            json={"group_ids": [group_id], "operation": "remove"},
        )
        assert response.status_code == 200

        remaining = await pg_session.execute(
            text("SELECT COUNT(*) FROM user_group_members WHERE user_id = :uid"),
            {"uid": non_admin_user_model.id},
        )
        assert remaining.scalar() == 0


@pytest.mark.asyncio
class TestAdminUserPatchAuthorization:
    """Test authorization for PATCH endpoint."""

    async def test_non_admin_cannot_patch_user(self, client_with_db, inserted_user):
        """Test that non-admin users cannot access the PATCH endpoint."""

        response = await client_with_db.patch(
            "/api/v1/admin/users/target-user-id",
            json={"is_administrator": True},
        )

        assert response.status_code == 401
