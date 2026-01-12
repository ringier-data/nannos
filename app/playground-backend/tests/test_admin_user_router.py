"""Integration tests for admin user management endpoints with real database."""

import os
from datetime import datetime, timezone

# Ensure code chooses auto credentials path during imports (avoid boto3 local credentials)
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
import pytest_asyncio
from sqlalchemy import text

from playground_backend.dependencies import require_admin
from playground_backend.models.user import User, UserStatus


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
class TestAdminUserPatchAuthorization:
    """Test authorization for PATCH endpoint."""

    async def test_non_admin_cannot_patch_user(self, client_with_db, inserted_user):
        """Test that non-admin users cannot access the PATCH endpoint."""

        response = await client_with_db.patch(
            "/api/v1/admin/users/target-user-id",
            json={"is_administrator": True},
        )

        assert response.status_code == 401
