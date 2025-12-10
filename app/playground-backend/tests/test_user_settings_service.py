"""Tests for UserSettingsService with PostgreSQL."""

import pytest
import pytest_asyncio
from sqlalchemy import text

from playground_backend.services.user_service import UserService
from playground_backend.services.user_settings_service import UserSettingsService


@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session to match test expectations."""
    yield pg_session


@pytest.fixture
def user_settings_service():
    """Create UserSettingsService instance."""
    return UserSettingsService()


@pytest.fixture
def user_service():
    """Create UserService instance."""
    return UserService()


@pytest.mark.asyncio
class TestUserSettingsService:
    """Test UserSettingsService functionality."""

    async def test_get_settings_returns_defaults_when_not_exists(self, user_settings_service, user_service, db_session):
        """Test that get_settings returns default values when no settings exist."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Get settings for user who has no settings record
        settings = await user_settings_service.get_settings(db_session, "test-user-id")

        assert settings is not None
        assert settings.user_id == "test-user-id"
        assert settings.language == "en"  # Default
        assert settings.custom_prompt is None  # Default

    async def test_upsert_settings_creates_new(self, user_settings_service, user_service, db_session):
        """Test creating new settings via upsert."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create settings
        settings = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="de",
            custom_prompt="You are a helpful assistant.",
        )
        await db_session.commit()

        assert settings is not None
        assert settings.user_id == "test-user-id"
        assert settings.language == "de"
        assert settings.custom_prompt == "You are a helpful assistant."
        assert settings.created_at is not None
        assert settings.updated_at is not None

    async def test_upsert_settings_updates_existing(self, user_settings_service, user_service, db_session):
        """Test updating existing settings via upsert."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create initial settings
        settings1 = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="de",
            custom_prompt="Initial prompt",
        )
        await db_session.commit()

        # Update settings
        settings2 = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="fr",
            custom_prompt="Updated prompt",
        )
        await db_session.commit()

        assert settings2.user_id == settings1.user_id
        assert settings2.language == "fr"
        assert settings2.custom_prompt == "Updated prompt"
        assert settings2.created_at == settings1.created_at  # Preserved
        assert settings2.updated_at >= settings1.updated_at  # Updated

    async def test_upsert_settings_partial_update_language_only(self, user_settings_service, user_service, db_session):
        """Test updating only language preserves custom_prompt."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create initial settings with custom_prompt
        await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="en",
            custom_prompt="My custom prompt",
        )
        await db_session.commit()

        # Update only language
        settings = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="de",
            custom_prompt=None,  # Not updating
        )
        await db_session.commit()

        assert settings.language == "de"
        assert settings.custom_prompt == "My custom prompt"  # Preserved

    async def test_upsert_settings_partial_update_custom_prompt_only(
        self, user_settings_service, user_service, db_session
    ):
        """Test updating only custom_prompt preserves language."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create initial settings with language
        await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="de",
            custom_prompt=None,
        )
        await db_session.commit()

        # Update only custom_prompt
        settings = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language=None,  # Not updating
            custom_prompt="New prompt",
        )
        await db_session.commit()

        assert settings.language == "de"  # Preserved
        assert settings.custom_prompt == "New prompt"

    async def test_get_settings_returns_default_timezone(self, user_settings_service, user_service, db_session):
        """Test that get_settings returns default timezone when not set."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Get settings for user who has no settings record
        settings = await user_settings_service.get_settings(db_session, "test-user-id")

        assert settings is not None
        assert settings.timezone == "Europe/Zurich"  # Default timezone

    async def test_upsert_settings_creates_with_timezone(self, user_settings_service, user_service, db_session):
        """Test creating new settings with timezone."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create settings with timezone
        settings = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="en",
            timezone_str="America/New_York",
            custom_prompt=None,
        )
        await db_session.commit()

        assert settings is not None
        assert settings.timezone == "America/New_York"

    async def test_upsert_settings_updates_timezone(self, user_settings_service, user_service, db_session):
        """Test updating timezone via upsert."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create initial settings
        await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="en",
            timezone_str="Europe/Zurich",
            custom_prompt=None,
        )
        await db_session.commit()

        # Update timezone
        settings = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language=None,
            timezone_str="Asia/Tokyo",
            custom_prompt=None,
        )
        await db_session.commit()

        assert settings.timezone == "Asia/Tokyo"
        assert settings.language == "en"  # Preserved

    async def test_upsert_settings_partial_update_preserves_timezone(
        self, user_settings_service, user_service, db_session
    ):
        """Test updating language preserves timezone."""
        # Create a user first
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create initial settings with timezone
        await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="en",
            timezone_str="America/Los_Angeles",
            custom_prompt=None,
        )
        await db_session.commit()

        # Update only language
        settings = await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="de",
            timezone_str=None,
            custom_prompt=None,
        )
        await db_session.commit()

        assert settings.language == "de"
        assert settings.timezone == "America/Los_Angeles"  # Preserved

    async def test_cascade_delete_removes_settings(self, user_settings_service, user_service, db_session):
        """Test that deleting a user cascades to delete their settings."""
        # Create a user
        await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await db_session.commit()

        # Create settings
        await user_settings_service.upsert_settings(
            db=db_session,
            user_id="test-user-id",
            language="de",
            custom_prompt="My prompt",
        )
        await db_session.commit()

        # Verify settings exist
        result = await db_session.execute(
            text("SELECT COUNT(*) FROM user_settings WHERE user_id = :user_id"),
            {"user_id": "test-user-id"},
        )
        count = result.scalar()
        assert count == 1

        # Delete user (hard delete for this test)
        await db_session.execute(
            text("DELETE FROM users WHERE id = :user_id"),
            {"user_id": "test-user-id"},
        )
        await db_session.commit()

        # Verify settings are deleted via cascade
        result = await db_session.execute(
            text("SELECT COUNT(*) FROM user_settings WHERE user_id = :user_id"),
            {"user_id": "test-user-id"},
        )
        count = result.scalar()
        assert count == 0
