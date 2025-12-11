"""Integration tests for user settings endpoints in auth router with real database."""

import os
from datetime import datetime, timezone

# Ensure code chooses auto credentials path during imports (avoid boto3 local credentials)
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import httpx
import pytest
import pytest_asyncio
from fastapi import Request

from playground_backend.db.session import get_db_session
from playground_backend.dependencies import require_auth, require_auth_or_bearer_token
from playground_backend.models.user import User, UserStatus
from playground_backend.services.user_service import UserService
from playground_backend.services.user_settings_service import UserSettingsService


@pytest.fixture
def user_service():
    """Create UserService instance."""
    return UserService()


@pytest.fixture
def user_settings_service():
    """Create UserSettingsService instance."""
    return UserSettingsService()


@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session."""
    yield pg_session


@pytest_asyncio.fixture
async def test_user_in_db(db_session, user_service):
    """Create a test user in the database."""
    user = await user_service.upsert_user(
        db=db_session,
        sub="test-user-id",
        email="test@example.com",
        first_name="Test",
        last_name="User",
    )
    await db_session.commit()
    return user


@pytest.fixture
def test_user_model():
    """Create a test user model for auth override."""
    return User(
        id="test-user-id",
        sub="test-user-id",
        email="test@example.com",
        first_name="Test",
        last_name="User",
        is_administrator=False,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def app_with_db(db_session, test_user_in_db, test_user_model):
    """Create FastAPI app with real database and mocked auth."""
    from fastapi import FastAPI

    from playground_backend.routers.auth_router import router as auth_router

    app = FastAPI()

    # Override get_db_session to use test database
    async def override_get_db():
        yield db_session

    # Override require_auth to return test user
    def override_require_auth():
        return test_user_model

    # Override require_auth_or_bearer_token to return test user
    async def override_require_auth_or_bearer_token(request: Request):
        return test_user_model

    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[require_auth] = override_require_auth
    app.dependency_overrides[require_auth_or_bearer_token] = override_require_auth_or_bearer_token

    app.include_router(auth_router)
    return app


@pytest_asyncio.fixture
async def async_client(app_with_db):
    """Create async HTTP client for testing."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_with_db),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
class TestUserSettingsEndpoints:
    """Test user settings endpoints with real database."""

    async def test_get_settings_returns_defaults(self, async_client):
        """Test GET /me/settings returns default values when no settings exist."""
        response = await async_client.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["user_id"] == "test-user-id"
        assert data["data"]["language"] == "en"
        assert data["data"]["custom_prompt"] is None

    async def test_patch_settings_update_language(self, async_client):
        """Test PATCH /me/settings updates language."""
        response = await async_client.patch(
            "/api/v1/auth/me/settings",
            json={"language": "de"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "de"
        assert data["data"]["custom_prompt"] is None

    async def test_patch_settings_update_custom_prompt(self, async_client):
        """Test PATCH /me/settings updates custom_prompt."""
        response = await async_client.patch(
            "/api/v1/auth/me/settings",
            json={"custom_prompt": "You are a helpful assistant."},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "en"
        assert data["data"]["custom_prompt"] == "You are a helpful assistant."

    async def test_patch_settings_update_both(self, async_client):
        """Test PATCH /me/settings updates both language and custom_prompt."""
        response = await async_client.patch(
            "/api/v1/auth/me/settings",
            json={
                "language": "fr",
                "custom_prompt": "Vous êtes un assistant utile.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "fr"
        assert data["data"]["custom_prompt"] == "Vous êtes un assistant utile."

    async def test_get_settings_after_update(self, async_client):
        """Test GET /me/settings returns updated values after PATCH."""
        # First update settings
        await async_client.patch(
            "/api/v1/auth/me/settings",
            json={"language": "de", "custom_prompt": "My custom prompt"},
        )

        # Then get settings
        response = await async_client.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "de"
        assert data["data"]["custom_prompt"] == "My custom prompt"

    async def test_patch_settings_partial_update_preserves_existing(self, async_client):
        """Test PATCH /me/settings preserves fields not being updated."""
        # Set initial values
        await async_client.patch(
            "/api/v1/auth/me/settings",
            json={"language": "de", "custom_prompt": "Initial prompt"},
        )

        # Update only language
        response = await async_client.patch(
            "/api/v1/auth/me/settings",
            json={"language": "fr"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "fr"
        assert data["data"]["custom_prompt"] == "Initial prompt"  # Preserved

    async def test_get_settings_returns_default_timezone(self, async_client):
        """Test GET /me/settings returns default timezone when no settings exist."""
        response = await async_client.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["timezone"] == "Europe/Zurich"  # Default timezone

    async def test_patch_settings_update_timezone(self, async_client):
        """Test PATCH /me/settings updates timezone."""
        response = await async_client.patch(
            "/api/v1/auth/me/settings",
            json={"timezone": "America/New_York"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["timezone"] == "America/New_York"
        assert data["data"]["language"] == "en"  # Default preserved

    async def test_patch_settings_update_all_fields(self, async_client):
        """Test PATCH /me/settings updates language, timezone, and custom_prompt."""
        response = await async_client.patch(
            "/api/v1/auth/me/settings",
            json={
                "language": "de",
                "timezone": "Europe/Berlin",
                "custom_prompt": "Du bist ein hilfreicher Assistent.",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "de"
        assert data["data"]["timezone"] == "Europe/Berlin"
        assert data["data"]["custom_prompt"] == "Du bist ein hilfreicher Assistent."

    async def test_patch_settings_partial_update_preserves_timezone(self, async_client):
        """Test PATCH /me/settings preserves timezone when not updated."""
        # Set initial values including timezone
        await async_client.patch(
            "/api/v1/auth/me/settings",
            json={
                "language": "en",
                "timezone": "Asia/Tokyo",
                "custom_prompt": "Initial prompt",
            },
        )

        # Update only language
        response = await async_client.patch(
            "/api/v1/auth/me/settings",
            json={"language": "fr"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "fr"
        assert data["data"]["timezone"] == "Asia/Tokyo"  # Preserved
        assert data["data"]["custom_prompt"] == "Initial prompt"  # Preserved
