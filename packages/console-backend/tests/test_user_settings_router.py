"""Integration tests for user settings endpoints in auth router with real database."""

import os

# Ensure code chooses auto credentials path during imports (avoid boto3 local credentials)
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from unittest.mock import patch

import pytest
import pytest_asyncio


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
def mock_user():
    """Return a mock user object."""
    from playground_backend.models.user import User, UserRole, UserStatus

    return User(
        id="test-user-id",
        sub="test-user-id",
        email="test@example.com",
        first_name="Test",
        last_name="User",
        is_administrator=False,
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


@pytest.mark.asyncio
class TestUserSettingsEndpoints:
    """Test user settings endpoints with real database."""

    async def test_get_settings_returns_defaults(self, client_with_db):
        """Test GET /me/settings returns default values when no settings exist."""
        response = await client_with_db.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["user_id"] == "test-user-id"
        assert data["data"]["language"] == "en"
        assert data["data"]["custom_prompt"] is None

    async def test_patch_settings_update_language(self, client_with_db):
        """Test PATCH /me/settings updates language."""
        response = await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={"language": "de"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "de"
        assert data["data"]["custom_prompt"] is None

    async def test_patch_settings_update_custom_prompt(self, client_with_db):
        """Test PATCH /me/settings updates custom_prompt."""
        response = await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={"custom_prompt": "You are a helpful assistant."},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "en"
        assert data["data"]["custom_prompt"] == "You are a helpful assistant."

    async def test_patch_settings_update_both(self, client_with_db):
        """Test PATCH /me/settings updates both language and custom_prompt."""
        response = await client_with_db.patch(
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

    async def test_get_settings_after_update(self, client_with_db):
        """Test GET /me/settings returns updated values after PATCH."""
        # First update settings
        await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={"language": "de", "custom_prompt": "My custom prompt"},
        )

        # Then get settings
        response = await client_with_db.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "de"
        assert data["data"]["custom_prompt"] == "My custom prompt"

    async def test_patch_settings_partial_update_preserves_existing(self, client_with_db):
        """Test PATCH /me/settings preserves fields not being updated."""
        # Set initial values
        await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={"language": "de", "custom_prompt": "Initial prompt"},
        )

        # Update only language
        response = await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={"language": "fr"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "fr"
        assert data["data"]["custom_prompt"] == "Initial prompt"  # Preserved

    async def test_get_settings_returns_default_timezone(self, client_with_db):
        """Test GET /me/settings returns default timezone when no settings exist."""
        response = await client_with_db.get("/api/v1/auth/me/settings")

        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert data["data"]["timezone"] == "Europe/Zurich"  # Default timezone

    async def test_patch_settings_update_timezone(self, client_with_db):
        """Test PATCH /me/settings updates timezone."""
        response = await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={"timezone": "America/New_York"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["timezone"] == "America/New_York"
        assert data["data"]["language"] == "en"  # Default preserved

    async def test_patch_settings_update_all_fields(self, client_with_db):
        """Test PATCH /me/settings updates language, timezone, and custom_prompt."""
        response = await client_with_db.patch(
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

    async def test_patch_settings_partial_update_preserves_timezone(self, client_with_db):
        """Test PATCH /me/settings preserves timezone when not updated."""
        # Set initial values including timezone
        await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={
                "language": "en",
                "timezone": "Asia/Tokyo",
                "custom_prompt": "Initial prompt",
            },
        )

        # Update only language
        response = await client_with_db.patch(
            "/api/v1/auth/me/settings",
            json={"language": "fr"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["language"] == "fr"
        assert data["data"]["timezone"] == "Asia/Tokyo"  # Preserved
        assert data["data"]["custom_prompt"] == "Initial prompt"  # Preserved

    async def test_patch_settings_phone_number_override(self, client_with_db):
        """Test PATCH /me/settings with phone_number_override (when Verify not configured)."""
        with patch("playground_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = False

            response = await client_with_db.patch(
                "/api/v1/auth/me/settings",
                json={"phone_number_override": "+41791234567"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["phone_number_override"] == "+41791234567"

    async def test_get_settings_includes_phone_override(self, client_with_db):
        """Test GET /me/settings returns phone_number_override in response."""
        with patch("playground_backend.routers.auth_router._phone_verification_service") as mock_svc:
            mock_svc.is_configured = False

            # Set phone override
            await client_with_db.patch(
                "/api/v1/auth/me/settings",
                json={"phone_number_override": "+41795555555"},
            )

        response = await client_with_db.get("/api/v1/auth/me/settings")
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["phone_number_override"] == "+41795555555"
