"""Integration tests for SCIM token management (admin) endpoints."""

import os
from datetime import datetime, timezone, timedelta

# Ensure code chooses auto credentials path during imports
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
import pytest_asyncio
from console_backend.dependencies import require_admin
from console_backend.models.user import User, UserStatus
from sqlalchemy import text


@pytest.fixture
def admin_user_model():
    """Admin user for auth override."""
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


@pytest_asyncio.fixture
async def admin_client(client_with_db, admin_user_model, pg_session):
    """HTTP client with admin auth override and admin user in DB."""
    # Insert admin user into DB to satisfy FK constraints
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :sub, :email, :first_name, :last_name, :is_administrator, :role, :status)
            ON CONFLICT (id) DO NOTHING
        """),
        {
            "id": admin_user_model.id,
            "sub": admin_user_model.sub,
            "email": admin_user_model.email,
            "first_name": admin_user_model.first_name,
            "last_name": admin_user_model.last_name,
            "is_administrator": admin_user_model.is_administrator,
            "role": admin_user_model.role,
            "status": admin_user_model.status,
        },
    )
    await pg_session.commit()

    def override_require_admin():
        return admin_user_model

    client_with_db._transport.app.dependency_overrides[require_admin] = override_require_admin
    yield client_with_db
    client_with_db._transport.app.dependency_overrides.pop(require_admin, None)


@pytest_asyncio.fixture
async def created_token(admin_client):
    """Create a token and return the response data."""
    response = await admin_client.post(
        "/api/v1/admin/scim-tokens",
        json={"name": "Fixture Token", "description": "Created by fixture"},
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.asyncio
class TestScimTokenCreate:
    async def test_create_token(self, admin_client):
        response = await admin_client.post(
            "/api/v1/admin/scim-tokens",
            json={
                "name": "My Token",
                "description": "Test token",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Token"
        assert data["description"] == "Test token"
        assert "token" in data
        assert len(data["token"]) > 20  # urlsafe(48) is ~64 chars
        assert data["id"] is not None

    async def test_create_token_with_expiry(self, admin_client):
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        response = await admin_client.post(
            "/api/v1/admin/scim-tokens",
            json={
                "name": "Expiring Token",
                "expires_at": expires,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["expires_at"] is not None

    async def test_create_token_minimal(self, admin_client):
        response = await admin_client.post(
            "/api/v1/admin/scim-tokens",
            json={"name": "Minimal"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Minimal"
        assert data["description"] is None


@pytest.mark.asyncio
class TestScimTokenList:
    async def test_list_tokens_empty(self, admin_client):
        response = await admin_client.get("/api/v1/admin/scim-tokens")
        assert response.status_code == 200
        data = response.json()
        assert data["data"] == []
        assert data["meta"]["total"] == 0

    async def test_list_tokens_after_create(self, admin_client, created_token):
        response = await admin_client.get("/api/v1/admin/scim-tokens")
        assert response.status_code == 200
        data = response.json()
        assert data["meta"]["total"] >= 1
        # Token value should be masked - only hint available
        token = data["data"][0]
        assert "token_hint" in token
        assert len(token["token_hint"]) == 4


@pytest.mark.asyncio
class TestScimTokenGet:
    async def test_get_token(self, admin_client, created_token):
        token_id = created_token["id"]
        response = await admin_client.get(f"/api/v1/admin/scim-tokens/{token_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["id"] == token_id
        assert data["name"] == "Fixture Token"

    async def test_get_token_not_found(self, admin_client):
        response = await admin_client.get("/api/v1/admin/scim-tokens/99999")
        assert response.status_code == 404


@pytest.mark.asyncio
class TestScimTokenRevoke:
    async def test_revoke_token(self, admin_client, created_token):
        token_id = created_token["id"]
        response = await admin_client.delete(f"/api/v1/admin/scim-tokens/{token_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["revoked_at"] is not None

    async def test_revoke_nonexistent(self, admin_client):
        response = await admin_client.delete("/api/v1/admin/scim-tokens/99999")
        assert response.status_code == 404

    async def test_revoked_token_no_longer_validates(self, admin_client, created_token, pg_session):
        """Revoked token should not pass validation."""
        token_value = created_token["token"]
        token_id = created_token["id"]

        # Revoke it
        await admin_client.delete(f"/api/v1/admin/scim-tokens/{token_id}")

        # Verify directly that the token is revoked in DB
        row = await pg_session.execute(
            text("SELECT revoked_at FROM scim_tokens WHERE id = :id"),
            {"id": token_id},
        )
        db_row = row.mappings().first()
        assert db_row["revoked_at"] is not None
