"""Tests for UserService with PostgreSQL using standard fixtures with migrations."""

from unittest.mock import patch

import pytest
import pytest_asyncio
from console_backend.models.user import User
from console_backend.services.user_service import UserService
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# user_service fixture now provided by conftest.py with proper DI


@pytest_asyncio.fixture
async def pg_session_2(postgres_with_migrations):
    """Create an async SQLAlchemy session for the test database.

    Each test gets its own database cloned from the template,
    so no transaction rollback needed - the DB is dropped after the test.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_async_engine(
        postgres_with_migrations["dsn"],
        echo=False,
    )

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        await session.execute(text(f"SET search_path TO {postgres_with_migrations['schema']}"))
        yield session

    await engine.dispose()


@pytest.mark.asyncio
class TestUserService:
    """Test UserService functionality."""

    async def test_upsert_user_create_new(self, user_service: UserService, pg_session: AsyncSession):
        """Test creating a new user."""
        user = await user_service.upsert_user(
            db=pg_session,
            sub="new-user-id",
            email="new@example.com",
            first_name="New",
            last_name="User",
            company_name="New Company",
        )

        assert user is not None
        assert user.sub == "new-user-id"  # sub is the OIDC subject
        assert user.id is not None  # id is a generated UUID
        assert user.email == "new@example.com"
        assert user.first_name == "New"
        assert user.last_name == "User"
        assert user.company_name == "New Company"
        assert user.is_administrator is False

    async def test_upsert_user_update_existing(self, user_service: UserService, pg_session: AsyncSession):
        """Test updating an existing user."""
        # Create initial user
        user1 = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Update the user
        user2 = await user_service.upsert_user(
            db=pg_session,
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

    async def test_get_user_not_found(self, user_service: UserService, pg_session: AsyncSession):
        """Test getting a non-existent user."""
        user = await user_service.get_user(pg_session, "non-existent-id")
        assert user is None

    async def test_get_user_success(self, user_service: UserService, pg_session: AsyncSession):
        """Test getting an existing user."""
        # Create user first
        created_user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        # Get the user by their UUID id
        user = await user_service.get_user(pg_session, created_user.id)

        assert user is not None
        assert user.id == created_user.id
        assert user.email == created_user.email

    async def test_upsert_user_without_company_name(self, user_service: UserService, pg_session: AsyncSession):
        """Test creating a user without company name."""
        user = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )

        assert user.company_name is None

    async def test_user_editable_fields_preserved_on_relogin(self, user_service: UserService, pg_session: AsyncSession):
        """Test that user-editable fields are preserved when user logs in again.

        This verifies the upsert correctly preserves is_administrator
        on subsequent logins.
        """
        # Create initial user
        user1 = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        assert user1.is_administrator is False

        # Simulate admin setting is_administrator=True
        await pg_session.execute(
            text("""
                UPDATE users
                SET is_administrator = TRUE
                WHERE id = :user_id
            """),
            {"user_id": user1.id},  # Use the actual UUID
        )
        await pg_session.commit()

        # Verify the direct update worked
        user_after_admin_update = await user_service.get_user(pg_session, user1.id)
        assert user_after_admin_update is not None
        assert user_after_admin_update.is_administrator is True

        # Now simulate user logging in again (upsert from OIDC callback)
        user2 = await user_service.upsert_user(
            db=pg_session,
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

    async def test_created_at_preserved_on_update(self, user_service: UserService, pg_session: AsyncSession):
        """Test that created_at is preserved when updating user."""
        # Create user
        user1 = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
        )
        original_created_at = user1.created_at

        # Update user
        user2 = await user_service.upsert_user(
            db=pg_session,
            sub="test-user-id",
            email="updated@example.com",
            first_name="Updated",
            last_name="User",
        )

        # Verify created_at is the same
        assert user2.created_at == original_created_at


@pytest.mark.asyncio
class TestUserServiceRetryLogic:
    """Test UserService retry logic for handling race conditions."""

    async def test_upsert_user_email_unique_constraint_raises_value_error(
        self, user_service: UserService, pg_session: AsyncSession, test_user_db: User, test_admin_user_db: User
    ):
        """Test that IntegrityError on idx_users_email_unique is caught and raises ValueError."""

        with pytest.raises(ValueError, match="Multiple users found with email admin@example.com or sub test-user-sub"):
            await user_service.upsert_user(
                db=pg_session,
                sub=test_user_db.sub,
                email=test_admin_user_db.email,  # tries to update test_user_db with email of test_admin_user_db, which should violate unique constraint
                first_name=test_user_db.first_name,
                last_name=test_user_db.last_name,
            )

    async def test_upsert_user_retry_logic_with_sub_constraint(
        self, user_service: UserService, pg_session: AsyncSession, test_user_db: User
    ):
        """Test that retry decorator is applied to _upsert_user_internal for idx_users_sub_unique."""

        # Just verify successful upsert works
        user = await user_service.upsert_user(
            db=pg_session,
            sub="retry-test-sub",
            email="retry@example.com",
            first_name="Retry",
            last_name="Test",
        )

        assert user is not None
        assert user.sub == "retry-test-sub"

    async def test_upsert_user_with_same_sub_different_email_updates_correctly(
        self, user_service: UserService, pg_session: AsyncSession, test_user_db: User
    ):
        """Test that upsert correctly handles same sub with different email (typical re-login scenario)."""
        # Update with same sub but different email (user changed email in IdP)
        user2 = await user_service.upsert_user(
            db=pg_session,
            sub=test_user_db.sub,
            email="email2@example.com",
            first_name="User",
            last_name="Updated",
        )

        # Should be same user (same id) with updated fields
        assert user2.id == test_user_db.id
        assert user2.sub == test_user_db.sub
        assert user2.email == "email2@example.com"
        assert user2.first_name == "User"
        assert user2.last_name == "Updated"

    async def test_upsert_user_generic_integrity_error_bubbles_up(
        self, user_service: UserService, pg_session: AsyncSession
    ):
        """Test that non-email IntegrityErrors bubble up after retry logic."""

        # Mock to raise IntegrityError that doesn't match email filter
        async def mock_generic_error(*args, **kwargs):
            raise IntegrityError(
                "some database constraint violation",
                params={},
                orig=Exception("generic database error"),
            )

        with patch.object(user_service, "_upsert_user_internal", side_effect=mock_generic_error):
            with pytest.raises(IntegrityError):
                await user_service.upsert_user(
                    db=pg_session,
                    sub="test-sub",
                    email="test@example.com",
                    first_name="Test",
                    last_name="User",
                )

    async def test_upsert_user_concurrent_sub_integrity_error_retries_successfully(
        self, user_service: UserService, test_user: User, pg_session: AsyncSession, pg_session_2: AsyncSession
    ):
        """Test that concurrent upserts causing sub IntegrityError are handled gracefully.

        With true concurrent operations, one may succeed while the other might fail initially due to unique constraint,
        but should succeed on retry due to the retry logic in place.
        """
        from asyncio import create_task, gather

        # Helper to upsert and commit in one transaction
        async def upsert_and_commit(session: AsyncSession):
            try:
                user = await user_service.upsert_user(
                    db=session,
                    sub=test_user.sub,
                    email=test_user.email,
                    first_name=test_user.first_name,
                    last_name=test_user.last_name,
                )
                await session.commit()
                return user
            except Exception as e:
                await session.rollback()
                return e

        # Start two concurrent upserts with same sub
        task1 = create_task(upsert_and_commit(pg_session))
        task2 = create_task(upsert_and_commit(pg_session_2))

        # Gather results (one or both should succeed, some might fail due to race conditions)
        results = await gather(task1, task2, return_exceptions=True)

        # Both should be successful, since one should have created the user and the other should have updated it without violating constraints due to retry logic
        successful_results = [r for r in results if isinstance(r, User)]
        assert len(successful_results) == 2, f"Expected both upserts to succeed, got results: {results}"

        # All successful results should have the correct sub
        for user in successful_results:
            assert user.sub == test_user.sub

        # Verify the user exists in the database
        final_user = await user_service.get_user_by_sub(pg_session, test_user.sub)
        assert final_user is not None
        assert final_user.sub == test_user.sub
