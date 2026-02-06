"""Tests for UserSettingsService with PostgreSQL."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.services.user_service import UserService
from playground_backend.services.user_settings_service import UserSettingsService


@pytest.mark.asyncio
class TestUserSettingsService:
    """Test UserSettingsService functionality."""

    async def test_get_settings_returns_defaults_when_not_exists(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test that get_settings returns default values when no settings exist."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Get settings for user who has no settings record
        settings = await user_settings_service.get_settings(pg_session, user.id)

        assert settings is not None
        assert settings.user_id == user.id
        assert settings.language == "en"  # Default
        assert settings.custom_prompt is None  # Default

    async def test_upsert_settings_creates_new(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test creating new settings via upsert."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create settings
        settings = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="de",
            custom_prompt="You are a helpful assistant.",
        )
        await pg_session.commit()

        assert settings is not None
        assert settings.user_id == user.id
        assert settings.language == "de"
        assert settings.custom_prompt == "You are a helpful assistant."
        assert settings.created_at is not None
        assert settings.updated_at is not None

    async def test_upsert_settings_updates_existing(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test updating existing settings via upsert."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings
        settings1 = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="de",
            custom_prompt="Initial prompt",
        )
        await pg_session.commit()

        # Update settings
        settings2 = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="fr",
            custom_prompt="Updated prompt",
        )
        await pg_session.commit()

        assert settings2.user_id == settings1.user_id
        assert settings2.language == "fr"
        assert settings2.custom_prompt == "Updated prompt"
        assert settings2.created_at == settings1.created_at  # Preserved
        assert settings2.updated_at >= settings1.updated_at  # Updated

    async def test_upsert_settings_partial_update_language_only(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test updating only language preserves custom_prompt."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings with custom_prompt
        await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="en",
            custom_prompt="My custom prompt",
        )
        await pg_session.commit()

        # Update only language
        settings = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="de",
        )
        await pg_session.commit()

        assert settings.language == "de"
        assert settings.custom_prompt == "My custom prompt"  # Preserved

    async def test_upsert_settings_partial_update_custom_prompt_only(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test updating only custom_prompt preserves language."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings with language
        await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="de",
            custom_prompt=None,
        )
        await pg_session.commit()

        # Update only custom_prompt
        settings = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            custom_prompt="New prompt",
        )
        await pg_session.commit()

        assert settings.language == "de"  # Preserved
        assert settings.custom_prompt == "New prompt"

    async def test_get_settings_returns_default_timezone(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test that get_settings returns default timezone when not set."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Get settings for user who has no settings record
        settings = await user_settings_service.get_settings(pg_session, user.id)

        assert settings is not None
        assert settings.timezone == "Europe/Zurich"  # Default timezone

    async def test_upsert_settings_creates_with_timezone(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test creating new settings with timezone."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create settings with timezone
        settings = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="en",
            timezone_str="America/New_York",
        )
        await pg_session.commit()

        assert settings is not None
        assert settings.timezone == "America/New_York"

    async def test_upsert_settings_updates_timezone(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test updating timezone via upsert."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings
        await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="en",
            timezone_str="Europe/Zurich",
        )
        await pg_session.commit()

        # Update timezone
        settings = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            # language=None,
            timezone_str="Asia/Tokyo",
            # custom_prompt=None,
        )
        await pg_session.commit()

        assert settings.timezone == "Asia/Tokyo"
        assert settings.language == "en"  # Preserved

    async def test_upsert_settings_partial_update_preserves_timezone(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test updating language preserves timezone."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings with timezone
        await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="en",
            timezone_str="America/Los_Angeles",
        )
        await pg_session.commit()

        # Update only language
        settings = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="de",
        )
        await pg_session.commit()

        assert settings.language == "de"
        assert settings.timezone == "America/Los_Angeles"  # Preserved

    async def test_upsert_settings_explicit_none_sets_null(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test updating language preserves timezone."""
        # Create a user first
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings with timezone
        await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="en",
            timezone_str="America/Los_Angeles",
        )
        await pg_session.commit()

        # Update only language
        settings = await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="de",
        )
        await pg_session.commit()

        assert settings.language == "de"
        assert settings.timezone == "America/Los_Angeles"  # Preserved

    async def test_cascade_delete_removes_settings(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test that deleting a user cascades to delete their settings."""
        # Create a user
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create settings
        await user_settings_service.upsert_settings(
            db=pg_session,
            user_id=user.id,
            language="de",
            custom_prompt="My prompt",
        )
        await pg_session.commit()

        # Verify settings exist
        result = await pg_session.execute(
            text("SELECT COUNT(*) FROM user_settings WHERE user_id = :user_id"),
            {"user_id": user.id},
        )
        count = result.scalar()
        assert count == 1

        # Delete user (hard delete for this test)
        await pg_session.execute(
            text("DELETE FROM users WHERE id = :user_id"),
            {"user_id": user.id},
        )
        await pg_session.commit()

        # Verify settings are deleted via cascade
        result = await pg_session.execute(
            text("SELECT COUNT(*) FROM user_settings WHERE user_id = :user_id"),
            {"user_id": user.id},
        )
        count = result.scalar()
        assert count == 0


@pytest.mark.asyncio
class TestUserSettingsThinkingConfiguration:
    """Test user settings with thinking and model preference configuration."""

    async def test_get_settings_returns_default_thinking_values(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test get_settings returns default thinking values when no settings exist."""
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-thinking-1",
            email="test1@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        settings = await user_settings_service.get_settings(pg_session, user.id)

        assert settings.user_id == user.id
        assert settings.preferred_model is None
        assert settings.enable_thinking is None
        assert settings.thinking_level is None

    async def test_upsert_settings_with_thinking_enabled(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test creating settings with thinking enabled."""
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-thinking-2",
            email="test2@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        settings = await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            preferred_model="claude-sonnet-4.5",
            enable_thinking=True,
            thinking_level="medium",
        )
        await pg_session.commit()

        assert settings.preferred_model == "claude-sonnet-4.5"
        assert settings.enable_thinking is True
        assert settings.thinking_level == "medium"

        # Verify in database
        result = await pg_session.execute(
            text("SELECT preferred_model, enable_thinking, thinking_level FROM user_settings WHERE user_id = :user_id"),
            {"user_id": user.id},
        )
        row = result.mappings().first()
        assert row["preferred_model"] == "claude-sonnet-4.5"
        assert row["enable_thinking"] is True
        assert row["thinking_level"] == "medium"

    async def test_partial_update_preserves_thinking_config(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test that partial update preserves thinking configuration."""
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-thinking-3",
            email="test3@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings with thinking
        await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            language="en",
            preferred_model="claude-sonnet-4.5",
            enable_thinking=True,
            thinking_level="low",
        )
        await pg_session.commit()

        # Update only language (thinking fields not provided, will use _UNSET defaults)
        settings = await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            language="de",
            # Thinking fields not provided
        )
        await pg_session.commit()

        # Thinking configuration should be preserved
        assert settings.language == "de"
        assert settings.preferred_model == "claude-sonnet-4.5"
        assert settings.enable_thinking is True
        assert settings.thinking_level == "low"

    async def test_explicit_none_clears_preferred_model(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test that explicitly passing None clears the preferred_model."""
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-thinking-4",
            email="test4@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # Create initial settings with preferred model
        await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            preferred_model="claude-sonnet-4.5",
        )
        await pg_session.commit()

        # Explicitly clear preferred_model by passing None
        settings = await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            preferred_model=None,
        )
        await pg_session.commit()

        assert settings.preferred_model is None

    async def test_all_thinking_levels(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test setting all available thinking levels."""
        from playground_backend.models.user import OrchestratorThinkingLevel

        levels = [
            OrchestratorThinkingLevel.MINIMAL,
            OrchestratorThinkingLevel.LOW,
            OrchestratorThinkingLevel.MEDIUM,
            OrchestratorThinkingLevel.HIGH,
        ]

        for i, level in enumerate(levels):
            user = await user_service.upsert_user(
                db=pg_session,
                sub=f"test-user-level-{i}",
                email=f"test-level-{i}@example.com",
                first_name="Test",
                last_name="User",
            )
            await pg_session.commit()

            settings = await user_settings_service.upsert_settings(
                pg_session,
                user_id=user.id,
                enable_thinking=True,
                thinking_level=level,
            )
            await pg_session.commit()

            assert settings.enable_thinking is True
            assert settings.thinking_level == level.value

    async def test_create_update_retrieve_thinking_settings(
        self, user_settings_service: UserSettingsService, user_service: UserService, pg_session: AsyncSession
    ):
        """Test complete lifecycle of thinking settings."""
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-lifecycle",
            email="test-lifecycle@example.com",
            first_name="Test",
            last_name="User",
        )
        await pg_session.commit()

        # 1. Create new settings with thinking
        created = await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            language="en",
            preferred_model="claude-sonnet-4.5",
            enable_thinking=True,
            thinking_level="low",
        )
        await pg_session.commit()

        assert created.preferred_model == "claude-sonnet-4.5"
        assert created.enable_thinking is True
        assert created.thinking_level == "low"

        # 2. Retrieve and verify
        retrieved = await user_settings_service.get_settings(pg_session, user.id)
        assert retrieved.preferred_model == "claude-sonnet-4.5"
        assert retrieved.enable_thinking is True
        assert retrieved.thinking_level == "low"

        # 3. Update thinking level
        updated = await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            thinking_level="high",
        )
        await pg_session.commit()

        assert updated.thinking_level == "high"
        assert updated.enable_thinking is True  # Preserved
        assert updated.preferred_model == "claude-sonnet-4.5"  # Preserved

        # 4. Disable thinking
        disabled = await user_settings_service.upsert_settings(
            pg_session,
            user_id=user.id,
            enable_thinking=False,
            thinking_level=None,
        )
        await pg_session.commit()

        assert disabled.enable_thinking is False
        assert disabled.preferred_model == "claude-sonnet-4.5"  # Still preserved
