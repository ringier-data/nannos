"""Tests for SessionService."""

from unittest.mock import patch

import pytest
import pytest_asyncio

from playground_backend.exceptions import SessionNotFoundError, SessionOwnershipError
from playground_backend.services.session_service import SessionService


@pytest_asyncio.fixture
async def session_service(postgres_with_migrations, mock_config):
    """Create a real SessionService backed by the test PostgreSQL database."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(postgres_with_migrations["dsn"], echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    # Patch get_async_session_factory to return our test factory
    with patch("playground_backend.services.session_service.get_async_session_factory", return_value=factory):
        service = SessionService()

    yield service

    await engine.dispose()


@pytest.mark.asyncio
class TestSessionService:
    """Test SessionService functionality."""

    async def test_create_session(self, session_service, test_user):
        """Test creating a new session."""
        user_id = test_user.id
        refresh_token = "test_refresh_token"
        id_token = "test_id_token"
        access_token = "test_access_token"

        session_id = await session_service.create_session(
            user_id=user_id,
            refresh_token=refresh_token,
            id_token=id_token,
            access_token=access_token,
        )

        assert session_id is not None
        assert isinstance(session_id, str)

        # Verify session was created
        session = await session_service.get_session(session_id)
        assert session is not None
        assert session.user_id == user_id
        assert session.refresh_token == refresh_token
        assert session.id_token == id_token
        assert session.access_token == access_token

    async def test_get_session_not_found(self, session_service):
        """Test getting a non-existent session."""
        session = await session_service.get_session("non-existent-id")
        assert session is None

    async def test_get_session_success(self, session_service, test_user):
        """Test getting an existing session."""
        # Create session first
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="test_refresh_token",
            id_token="test_id_token",
            access_token="test_access_token",
        )

        # Get the session
        session = await session_service.get_session(session_id)

        assert session is not None
        assert session.session_id == session_id
        assert session.user_id == test_user.id
        assert session.refresh_token == "test_refresh_token"

    async def test_destroy_session(self, session_service, test_user):
        """Test destroying a session."""
        # Create session
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="test_refresh_token",
            id_token="test_id_token",
            access_token="test_access_token",
        )

        # Verify it exists
        session = await session_service.get_session(session_id)
        assert session is not None

        # Destroy it
        await session_service.destroy_session(session_id)

        # Verify it's gone
        session = await session_service.get_session(session_id)
        assert session is None

    async def test_update_session(self, session_service, test_user):
        """Test updating a session."""
        # Create session
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="old_refresh_token",
            id_token="old_id_token",
            access_token="old_access_token",
        )

        # Update the session
        await session_service.update_session(
            session_id=session_id,
            user_id=test_user.id,
            access_token="new_access_token",
        )

        # Verify update
        session = await session_service.get_session(session_id)
        assert session is not None
        assert session.access_token == "new_access_token"
        assert session.refresh_token == "old_refresh_token"  # Should remain unchanged

    async def test_update_session_user_id_mismatch(self, session_service, test_user):
        """Test that updating a session with wrong user_id fails."""
        # Create session
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="test_refresh_token",
            id_token="test_id_token",
            access_token="test_access_token",
        )

        # Try to update with different user_id
        with pytest.raises(SessionOwnershipError, match="does not own session"):
            await session_service.update_session(
                session_id=session_id,
                user_id="different-user-id",
                access_token="new_access_token",
            )

        # Verify session was not updated
        session = await session_service.get_session(session_id)
        assert session is not None
        assert session.user_id == test_user.id
        assert session.access_token == "test_access_token"  # Should remain unchanged

    async def test_update_session_correct_user_id_allows_update(self, session_service, test_user):
        """Test that providing the correct user_id allows the update."""
        # Create session
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="old_refresh_token",
            id_token="old_id_token",
            access_token="old_access_token",
        )

        # Update with correct user_id should succeed
        await session_service.update_session(
            session_id=session_id,
            user_id=test_user.id,
            access_token="new_access_token",
            refresh_token="new_refresh_token",
        )

        # Verify session was updated
        session = await session_service.get_session(session_id)
        assert session is not None
        assert session.user_id == test_user.id
        assert session.access_token == "new_access_token"
        assert session.refresh_token == "new_refresh_token"

    async def test_update_session_nonexistent_session_with_user_id(self, session_service):
        """Test that updating a non-existent session fails with SessionNotFoundError."""
        # Try to update a session that doesn't exist
        with pytest.raises(SessionNotFoundError, match="not found"):
            await session_service.update_session(
                session_id="nonexistent-session-id",
                user_id="some-user-id",
                access_token="new_access_token",
            )

    async def test_session_ttl_set_correctly(self, session_service, test_user, test_config):
        """Test that session expiry is set correctly."""
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="test_refresh_token",
            id_token="test_id_token",
            access_token="test_access_token",
        )

        # Get the session to check expiry
        session = await session_service.get_session(session_id)
        assert session is not None

        # expires_at should be approximately issued_at + session_ttl_seconds
        expected_expires_at = session.issued_at.timestamp() + test_config.session_ttl_seconds
        assert abs(session.expires_at.timestamp() - expected_expires_at) <= 1  # Allow 1 second difference

    async def test_update_session_with_tokens(self, session_service, test_user):
        """Test updating session tokens."""
        # Create session with initial tokens
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="old_refresh_token",
            id_token="old_id_token",
            access_token="old_access_token",
        )

        # Update with new tokens
        await session_service.update_session(
            session_id=session_id,
            user_id=test_user.id,
            access_token="new_access_token",
            refresh_token="new_refresh_token",
            id_token="new_id_token",
        )

        # Verify update
        session = await session_service.get_session(session_id)
        assert session is not None
        assert session.access_token == "new_access_token"
        assert session.refresh_token == "new_refresh_token"
        assert session.id_token == "new_id_token"

    async def test_update_session_partial(self, session_service, test_user):
        """Test updating only access token without changing refresh token."""
        # Create session
        session_id = await session_service.create_session(
            user_id=test_user.id,
            refresh_token="original_refresh_token",
            id_token="original_id_token",
            access_token="original_access_token",
        )

        # Update only access token
        await session_service.update_session(
            session_id=session_id,
            user_id=test_user.id,
            access_token="new_access_token",
        )

        # Verify only access token was updated
        session = await session_service.get_session(session_id)
        assert session is not None
        assert session.access_token == "new_access_token"
        assert session.refresh_token == "original_refresh_token"  # Should remain unchanged
        assert session.id_token == "original_id_token"  # Should remain unchanged
