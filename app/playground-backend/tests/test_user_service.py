"""Tests for UserService with PostgreSQL using standard fixtures with migrations."""

import pytest
import pytest_asyncio
from sqlalchemy import text

from playground_backend.services.user_service import UserService


# Alias pg_session to db_session for compatibility with tests
@pytest_asyncio.fixture
async def db_session(pg_session):
    """Alias for pg_session to match test expectations."""
    yield pg_session


@pytest.fixture
def user_service():
    """Create UserService instance."""
    return UserService()


@pytest.mark.asyncio
class TestUserService:
    """Test UserService functionality."""

    async def test_upsert_user_create_new(self, user_service, db_session):
        """Test creating a new user."""
        user = await user_service.upsert_user(
            db=db_session,
            sub="new-user-id",
            email="new@example.com",
            first_name="New",
            last_name="User",
            company_name="New Company",
        )

        assert user is not None
        assert user.id == "new-user-id"
        assert user.sub == "new-user-id"
        assert user.email == "new@example.com"
        assert user.first_name == "New"
        assert user.last_name == "User"
        assert user.company_name == "New Company"
        assert user.is_administrator is False

    async def test_upsert_user_update_existing(self, user_service, db_session):
        """Test updating an existing user."""
        # Create initial user
        user1 = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Update the user
        user2 = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="updated@example.com",
            first_name="Updated",
            last_name="Name",
            company_name="New Company",
        )

        # Verify update
        assert user2.id == user1.id
        assert user2.email == "updated@example.com"
        assert user2.first_name == "Updated"
        assert user2.last_name == "Name"
        assert user2.company_name == "New Company"

    async def test_get_user_not_found(self, user_service, db_session):
        """Test getting a non-existent user."""
        user = await user_service.get_user(db_session, "non-existent-id")
        assert user is None

    async def test_get_user_success(self, user_service, db_session):
        """Test getting an existing user."""
        # Create user first
        created_user = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Get the user
        user = await user_service.get_user(db_session, "test-user-id")

        assert user is not None
        assert user.id == created_user.id
        assert user.email == created_user.email

    async def test_upsert_user_without_company_name(self, user_service, db_session):
        """Test creating a user without company name."""
        user = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        assert user.company_name is None

    async def test_user_editable_fields_preserved_on_relogin(self, user_service, db_session):
        """Test that user-editable fields are preserved when user logs in again.

        This verifies the upsert correctly preserves is_administrator
        on subsequent logins.
        """
        # Create initial user
        user1 = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        assert user1.is_administrator is False

        # Simulate admin setting is_administrator=True
        await db_session.execute(
            text("""
                UPDATE users
                SET is_administrator = TRUE
                WHERE id = :user_id
            """),
            {"user_id": "test-user-id"},
        )
        await db_session.commit()

        # Verify the direct update worked
        user_after_admin_update = await user_service.get_user(db_session, "test-user-id")
        assert user_after_admin_update is not None
        assert user_after_admin_update.is_administrator is True

        # Now simulate user logging in again (upsert from OIDC callback)
        user2 = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="newemail@example.com",  # Email changed in IdP
            first_name="Test",
            last_name="User",
        )

        # OIDC-sourced fields should be updated
        assert user2.email == "newemail@example.com"

        # User-editable fields should be PRESERVED (not reset to defaults)
        assert user2.is_administrator is True  # Should NOT be reset to False

        # created_at should also be preserved
        assert user2.created_at == user1.created_at

    async def test_created_at_preserved_on_update(self, user_service, db_session):
        """Test that created_at is preserved when updating user."""
        # Create user
        user1 = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        original_created_at = user1.created_at

        # Update user
        user2 = await user_service.upsert_user(
            db=db_session,
            sub="test-user-id",
            email="updated@example.com",
            first_name="Updated",
            last_name="User",
        )

        # Verify created_at is the same
        assert user2.created_at == original_created_at
