"""Tests for authentication dependencies."""

from unittest.mock import MagicMock

import pytest

from dependencies import require_admin, require_auth
from fastapi import HTTPException


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

    def test_require_admin_with_admin_user(self, test_admin_user):
        """Test require_admin with admin user."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_admin_user

        user = require_admin(request)

        assert user == test_admin_user
        assert user.is_administrator is True

    def test_require_admin_with_non_admin_user(self, test_user):
        """Test require_admin with non-admin user raises 403."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = test_user

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 403

    def test_require_admin_without_user(self):
        """Test require_admin without user raises 401."""
        request = MagicMock()
        request.state = MagicMock()
        request.state.user = None

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request)

        assert exc_info.value.status_code == 401
