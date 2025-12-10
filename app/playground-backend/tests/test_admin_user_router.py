"""Integration tests for admin user management endpoints with real database."""

import os
from datetime import datetime, timezone

# Ensure code chooses auto credentials path during imports (avoid boto3 local credentials)
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException

from playground_backend.db.session import get_db_session
from playground_backend.dependencies import require_admin
from playground_backend.models.user import User, UserStatus
from playground_backend.services.user_service import UserService


@pytest.fixture
def user_service():
    """Create UserService instance."""
    return UserService()


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
async def users_in_db(db_session, user_service):
    """Create test users in the database."""
    # Create admin user
    admin = await user_service.upsert_user(
        db=db_session,
        sub="admin-user-id",
        email="admin@example.com",
        first_name="Admin",
        last_name="User",
    )
    # Create target user to be modified
    target = await user_service.upsert_user(
        db=db_session,
        sub="target-user-id",
        email="target@example.com",
        first_name="Target",
        last_name="User",
    )
    await db_session.commit()
    return {"admin": admin, "target": target}


@pytest_asyncio.fixture
async def app_with_admin_db(db_session, users_in_db, admin_user_model):
    """Create FastAPI app with real database and admin auth."""
    from fastapi import FastAPI

    from playground_backend.routers.admin_user_router import router as admin_user_router

    app = FastAPI()

    # Override get_db_session to use test database
    async def override_get_db():
        yield db_session

    # Override require_admin to return admin user
    def override_require_admin():
        return admin_user_model

    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[require_admin] = override_require_admin

    app.include_router(admin_user_router)
    return app


@pytest_asyncio.fixture
async def admin_client(app_with_admin_db):
    """Create async HTTP client with admin auth."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_admin_db),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
class TestAdminUserPatchEndpoint:
    """Test PATCH /admin/users/{user_id} endpoint."""

    async def test_patch_user_set_administrator_true(self, admin_client):
        """Test setting is_administrator to true."""
        response = await admin_client.patch(
            "/api/v1/admin/users/target-user-id",
            json={"is_administrator": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["is_administrator"] is True

    async def test_patch_user_set_administrator_false(self, admin_client, db_session):
        """Test setting is_administrator to false."""
        from sqlalchemy import text

        # First make the user an admin via direct DB
        await db_session.execute(
            text("UPDATE users SET is_administrator = TRUE WHERE id = :user_id"),
            {"user_id": "target-user-id"},
        )
        await db_session.commit()

        # Now set to false via API
        response = await admin_client.patch(
            "/api/v1/admin/users/target-user-id",
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

    async def test_patch_user_empty_body(self, admin_client):
        """Test patching with empty body (no changes)."""
        response = await admin_client.patch(
            "/api/v1/admin/users/target-user-id",
            json={},
        )

        assert response.status_code == 200
        # Should still return the user data
        data = response.json()
        assert "data" in data

    async def test_get_user_after_admin_update(self, admin_client):
        """Test GET user after PATCH shows updated values."""
        # Update user
        await admin_client.patch(
            "/api/v1/admin/users/target-user-id",
            json={"is_administrator": True},
        )

        # Get user
        response = await admin_client.get("/api/v1/admin/users/target-user-id")

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["is_administrator"] is True


@pytest.mark.asyncio
class TestAdminUserPatchAuthorization:
    """Test authorization for PATCH endpoint."""

    async def test_non_admin_cannot_patch_user(self, db_session, users_in_db):
        """Test that non-admin users cannot access the PATCH endpoint."""
        from fastapi import FastAPI

        from playground_backend.routers.admin_user_router import router as admin_user_router

        app = FastAPI()

        async def override_get_db():
            yield db_session

        def override_require_admin():
            # This simulates the real require_admin behavior for non-admins
            raise HTTPException(status_code=403, detail="Not an administrator")

        app.dependency_overrides[get_db_session] = override_get_db
        app.dependency_overrides[require_admin] = override_require_admin

        app.include_router(admin_user_router)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.patch(
                "/api/v1/admin/users/target-user-id",
                json={"is_administrator": True},
            )

        assert response.status_code == 403
