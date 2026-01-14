"""Tests for authentication dependencies."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from playground_backend.dependencies import (
    ADMIN_MODE_HEADER,
    get_admin_mode,
    is_admin_mode,
    require_admin,
    require_auth,
)


class TestAuthDependencies:
    """Test authentication dependency functions."""

    def test_require_auth_with_user(self, test_user):
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

    def test_require_admin_with_admin_user_and_admin_mode_enabled(self, test_admin_user):
        """Test require_admin with admin user and admin mode header enabled."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_admin_user
        request.headers = {ADMIN_MODE_HEADER: "true"}

        user = require_admin(request)

        assert user == test_admin_user
        assert user.is_administrator is True

    def test_require_admin_with_admin_user_but_admin_mode_disabled(self, test_admin_user):
        """Test require_admin with admin user but admin mode header disabled raises 403."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_admin_user
        request.headers = {ADMIN_MODE_HEADER: "false"}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403
        assert "admin mode not enabled" in exc_info.value.detail.lower()

    def test_require_admin_with_admin_user_but_no_admin_mode_header(self, test_admin_user):
        """Test require_admin with admin user but no admin mode header raises 403."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_admin_user
        request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403

    def test_require_admin_with_non_admin_user(self, test_user):
        """Test require_admin with non-admin user raises 403."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_user
        request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403

    def test_require_admin_with_non_admin_user_privilege_escalation_attempt(self, test_user):
        """Test require_admin with non-admin user trying to use admin mode header raises 403."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_user
        request.headers = {ADMIN_MODE_HEADER: "true"}

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403
        assert "privilege escalation" in exc_info.value.detail.lower()

    def test_require_admin_without_user(self):
        """Test require_admin without user raises 401."""
        request = MagicMock()
        request.state = MagicMock()
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
        request.headers = MagicMock()
        request.headers.get = MagicMock(return_value="true")

        with pytest.raises(HTTPException) as exc_info:
            is_admin_mode(request, test_user)

        assert exc_info.value.status_code == 403
        assert "privilege escalation" in exc_info.value.detail.lower()
