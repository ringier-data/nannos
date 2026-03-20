"""Tests for authentication dependencies."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from playground_backend.dependencies import (
    ADMIN_MODE_HEADER,
    get_admin_mode,
    is_admin_mode,
    require_admin,
    require_auth,
    require_auth_or_bearer_token,
)
from playground_backend.models.user import User, UserRole, UserStatus


class TestAuthDependencies:
    """Test authentication dependency functions."""

    def test_require_auth_with_user(self, test_user: User):
        """Test require_auth with authenticated user."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_user

        user = require_auth(request)

        assert user == test_user

    def test_require_auth_without_user(self):
        """Test require_auth without user raises 401."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None

        with pytest.raises(HTTPException) as exc_info:
            require_auth(request)

        assert exc_info.value.status_code == 401

    def test_require_auth_no_user_attribute(self):
        """Test require_auth when state has no user attribute."""
        request = MagicMock()
        request.state = MagicMock(spec=[])  # No user attribute

        with pytest.raises(HTTPException) as exc_info:
            require_auth(request)

        assert exc_info.value.status_code == 401

    def test_require_admin_with_admin_user_and_admin_mode_enabled(self, test_admin_user: User):
        """Test require_admin with admin user and admin mode header enabled."""
        request = MagicMock()
        request.state = MagicMock(spec_set=["user"])  # Only 'user' attribute exists
        request.state.user = test_admin_user
        request.headers = {ADMIN_MODE_HEADER: "true"}

        user = require_admin(request)

        assert user == test_admin_user
        assert user.is_administrator is True

    def test_require_admin_with_admin_user_but_admin_mode_disabled(self, test_admin_user: User):
        """Test require_admin with admin user but admin mode header disabled raises 403."""
        request = MagicMock()
        request.state = MagicMock(spec_set=["user"])  # Only 'user' attribute exists
        request.state.user = test_admin_user
        request.headers = {ADMIN_MODE_HEADER: "false"}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403
        assert "admin mode not enabled" in exc_info.value.detail.lower()

    def test_require_admin_with_admin_user_but_no_admin_mode_header(self, test_admin_user: User):
        """Test require_admin with admin user but no admin mode header raises 403."""
        request = MagicMock()
        request.state = MagicMock(spec_set=["user"])  # Only 'user' attribute exists
        request.state.user = test_admin_user
        request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403

    def test_require_admin_with_non_admin_user(self, test_user: User):
        """Test require_admin with non-admin user raises 403."""
        request = MagicMock()
        request.state = MagicMock(spec_set=["user"])  # Only 'user' attribute exists
        request.state.user = test_user
        request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403

    def test_require_admin_with_non_admin_user_privilege_escalation_attempt(self, test_user: User):
        """Test require_admin with non-admin user trying to use admin mode header raises 403."""
        request = MagicMock()
        request.state = MagicMock(spec_set=["user"])  # Only 'user' attribute exists
        request.state.user = test_user
        request.headers = {ADMIN_MODE_HEADER: "true"}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403
        assert "privilege escalation" in exc_info.value.detail.lower()


class TestRequireAuthOrBearerToken:
    """Test require_auth_or_bearer_token dependency function."""

    @pytest.mark.asyncio
    async def test_with_session_user(self, test_user: User, pg_session: AsyncSession):
        """Test require_auth_or_bearer_token returns session user when available."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_user
        request.headers = {}

        user = await require_auth_or_bearer_token(request, db=pg_session)

        assert user == test_user

    @pytest.mark.asyncio
    async def test_with_valid_bearer_token_existing_user(self, test_user: User, pg_session: AsyncSession):
        """Test require_auth_or_bearer_token validates bearer token and returns existing user."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None
        request.headers = {"Authorization": "Bearer valid_token_123"}

        # Mock the app state and user service
        user_service_mock = AsyncMock()
        user_service_mock.get_user_by_sub = AsyncMock(return_value=test_user)
        request.app.state.user_service = user_service_mock

        with (
            patch("playground_backend.dependencies.JWTValidator") as mock_validator_class,
        ):
            mock_validator = AsyncMock()
            mock_validator.validate = AsyncMock(
                return_value={
                    "sub": "test-user-sub",
                    "email": "test@example.com",
                    "given_name": "Test",
                    "family_name": "User",
                    "company_name": "Test Company",
                }
            )
            mock_validator_class.return_value = mock_validator

            user = await require_auth_or_bearer_token(request, db=pg_session)

            assert user == test_user
            user_service_mock.get_user_by_sub.assert_called_once_with(pg_session, "test-user-sub")
            # Should NOT call upsert_user since user exists
            user_service_mock.upsert_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_onboard_with_bearer_token_new_user(self, test_user: User, pg_session: AsyncSession):
        """Test require_auth_or_bearer_token auto-onboards new user from valid bearer token."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None
        request.headers = {"Authorization": "Bearer valid_token_123"}

        # Create expected auto-onboarded user
        auto_onboarded_user = User(
            id="auto-onboard-user-id",
            sub="new-user-sub",
            email="newuser@example.com",
            first_name="New",
            last_name="User",
            company_name="New Company",
            is_administrator=False,
            role=UserRole.MEMBER,
            status=UserStatus.ACTIVE,
        )

        # Mock the app state and user service
        user_service_mock = AsyncMock()
        user_service_mock.get_user_by_sub = AsyncMock(return_value=None)  # User doesn't exist
        user_service_mock.upsert_user = AsyncMock(return_value=auto_onboarded_user)
        request.app.state.user_service = user_service_mock

        token_payload = {
            "sub": "new-user-sub",
            "email": "newuser@example.com",
            "given_name": "New",
            "family_name": "User",
            "company_name": "New Company",
        }

        with (
            patch("playground_backend.dependencies.JWTValidator") as mock_validator_class,
        ):
            mock_validator = AsyncMock()
            mock_validator.validate = AsyncMock(return_value=token_payload)
            mock_validator_class.return_value = mock_validator

            user = await require_auth_or_bearer_token(request, db=pg_session)

            assert user == auto_onboarded_user
            user_service_mock.get_user_by_sub.assert_called_once_with(pg_session, "new-user-sub")
            # Should call upsert_user with token claims
            user_service_mock.upsert_user.assert_called_once_with(
                pg_session,
                sub="new-user-sub",
                email="newuser@example.com",
                first_name="New",
                last_name="User",
                company_name="New Company",
            )

    @pytest.mark.asyncio
    async def test_auto_onboard_with_missing_token_claims(self, pg_session: AsyncSession):
        """Test require_auth_or_bearer_token auto-onboards with empty strings for missing claims."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None
        request.headers = {"Authorization": "Bearer valid_token_456"}

        # Create expected auto-onboarded user with minimal info
        auto_onboarded_user = User(
            id="minimal-user-id",
            sub="minimal-user-sub",
            email="",
            first_name="",
            last_name="",
            company_name="",
            is_administrator=False,
            role=UserRole.MEMBER,
            status=UserStatus.ACTIVE,
        )

        # Mock the app state and user service
        user_service_mock = AsyncMock()
        user_service_mock.get_user_by_sub = AsyncMock(return_value=None)
        user_service_mock.upsert_user = AsyncMock(return_value=auto_onboarded_user)
        request.app.state.user_service = user_service_mock
        # Token with only sub claim (no email, name, etc.)
        token_payload = {"sub": "minimal-user-sub"}

        with patch("playground_backend.dependencies.JWTValidator") as mock_validator_class:
            mock_validator = AsyncMock()
            mock_validator.validate = AsyncMock(return_value=token_payload)
            mock_validator_class.return_value = mock_validator

            user = await require_auth_or_bearer_token(request, db=pg_session)

            assert user == auto_onboarded_user
            # Should call upsert_user with empty strings for missing claims
            user_service_mock.upsert_user.assert_called_once_with(
                pg_session,
                sub="minimal-user-sub",
                email="",
                first_name="",
                last_name="",
                company_name=None,
            )

    @pytest.mark.asyncio
    async def test_bearer_token_without_sub_claim(self, pg_session: AsyncSession):
        """Test require_auth_or_bearer_token raises 401 when token missing sub claim."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None
        request.headers = {"Authorization": "Bearer invalid_token"}

        with patch("playground_backend.dependencies.JWTValidator") as mock_validator_class:
            mock_validator = AsyncMock()
            mock_validator.validate = AsyncMock(return_value={"email": "test@example.com"})  # No sub
            mock_validator_class.return_value = mock_validator

            with pytest.raises(HTTPException) as exc_info:
                await require_auth_or_bearer_token(request, db=pg_session)

            assert exc_info.value.status_code == 401
            assert "missing subject" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_invalid_bearer_token(self, pg_session: AsyncSession):
        """Test require_auth_or_bearer_token raises 401 for invalid bearer token."""
        from ringier_a2a_sdk.auth import JWTValidationError

        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None
        request.headers = {"Authorization": "Bearer invalid_token"}

        with patch("playground_backend.dependencies.JWTValidator") as mock_validator_class:
            mock_validator = AsyncMock()
            mock_validator.validate = AsyncMock(side_effect=JWTValidationError("Invalid token"))
            mock_validator_class.return_value = mock_validator

            with pytest.raises(HTTPException) as exc_info:
                await require_auth_or_bearer_token(request, db=pg_session)

            assert exc_info.value.status_code == 401
            assert "invalid token" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_no_authentication(self, pg_session: AsyncSession):
        """Test require_auth_or_bearer_token raises 401 when no authentication provided."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None
        request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            await require_auth_or_bearer_token(request, db=pg_session)

        assert exc_info.value.status_code == 401
        assert "not authenticated" in exc_info.value.detail.lower()

    def test_require_admin_without_user(self):
        """Test require_admin without user raises 401."""
        request = MagicMock()
        request.state = MagicMock(spec_set=["user"])  # Only 'user' attribute exists
        request.state.user = None
        request.headers = {ADMIN_MODE_HEADER: "true"}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 401


class TestGetAdminMode:
    """Test get_admin_mode helper function."""

    def test_get_admin_mode_true(self):
        """Test get_admin_mode returns True when header is 'true'."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="true")

        assert get_admin_mode(request) is True

    def test_get_admin_mode_true_case_insensitive(self):
        """Test get_admin_mode is case-insensitive."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="TRUE")

        assert get_admin_mode(request) is True

    def test_get_admin_mode_false(self):
        """Test get_admin_mode returns False when header is 'false'."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="false")

        assert get_admin_mode(request) is False

    def test_get_admin_mode_missing_header(self):
        """Test get_admin_mode returns False when header is missing."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="")

        assert get_admin_mode(request) is False


class TestIsAdminMode:
    """Test is_admin_mode helper function."""

    def test_is_admin_mode_admin_with_header_enabled(self, test_admin_user):
        """Test is_admin_mode returns True for admin with header enabled."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="true")

        assert is_admin_mode(request, test_admin_user) is True

    def test_is_admin_mode_admin_with_header_disabled(self, test_admin_user):
        """Test is_admin_mode returns False for admin with header disabled."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="false")

        assert is_admin_mode(request, test_admin_user) is False

    def test_is_admin_mode_non_admin(self, test_user):
        """Test is_admin_mode returns False for non-admin."""
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="false")

        assert is_admin_mode(request, test_user) is False

    def test_is_admin_mode_privilege_escalation_attempt(self, test_user):
        """Test is_admin_mode raises 403 for non-admin with header enabled."""
        request = MagicMock()
        request.state = MagicMock(spec_set=[])  # No attributes at all
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="true")

        with pytest.raises(HTTPException) as exc_info:
            is_admin_mode(request, test_user)

        assert exc_info.value.status_code == 403
        assert "privilege escalation" in exc_info.value.detail.lower()
