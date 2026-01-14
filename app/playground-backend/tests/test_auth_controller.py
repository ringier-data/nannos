"""Tests for auth controller."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from authlib.integrations.starlette_client import OAuthError
from fastapi import HTTPException


@pytest.mark.asyncio
class TestAuthController:
    """Test AuthController functionality."""

    async def test_get_login_valid_redirect(self, auth_controller, create_mock_request, mock_config, mock_oauth):
        """Test login endpoint with valid redirect URL."""
        request = create_mock_request(query_params={"redirectTo": f"https://{mock_config.base_domain}/dashboard"})

        # Configure the mock (mock_oauth IS the oidc client mock from fixture)
        mock_oauth.authorize_redirect = AsyncMock(return_value=MagicMock(status_code=303))

        _ = await auth_controller.get_login(request)

        # Should store redirect_to in session
        assert request.session["redirect_to"] == f"https://{mock_config.base_domain}/dashboard"

        # Should call authorize_redirect
        mock_oauth.authorize_redirect.assert_called_once()

    async def test_get_login_invalid_redirect(self, auth_controller, create_mock_request):
        """Test login endpoint with invalid redirect URL."""
        request = create_mock_request(query_params={"redirectTo": "javascript:alert(1)"})

        with pytest.raises(HTTPException) as exc_info:
            await auth_controller.get_login(request)

        assert exc_info.value.status_code == 422

    async def test_get_login_callback_success(
        self,
        auth_controller,
        create_mock_request,
        create_mock_response,
        mock_config,
        oidc_userinfo_response,
        mock_oauth,
        mock_db_session_factory,
    ):
        """Test successful login callback."""
        redirect_to = f"https://{mock_config.base_domain}/"
        request = create_mock_request()
        request.session["redirect_to"] = redirect_to
        response = create_mock_response()

        # Mock user_service.upsert_user to return a mock user
        mock_user = MagicMock(id="user-123", email="test@example.com")
        auth_controller.user_service.upsert_user = AsyncMock(return_value=mock_user)

        # Configure mock (mock_oauth IS the oidc client mock from fixture)
        mock_token = {
            "access_token": "test_access_token",
            "id_token": "test_id_token",
            "refresh_token": "test_refresh_token",
            "userinfo": oidc_userinfo_response,
        }
        mock_oauth.authorize_access_token = AsyncMock(return_value=mock_token)

        result = await auth_controller.get_login_callback(request, response)

        # Should redirect to original URL
        assert result.status_code == 303
        assert redirect_to in result.headers["location"]

    async def test_get_login_callback_missing_code(
        self, auth_controller, create_mock_request, create_mock_response, mock_oauth
    ):
        """Test login callback without code."""
        request = create_mock_request()
        response = create_mock_response()

        # Configure mock (mock_oauth IS the oidc client mock from fixture)
        mock_oauth.authorize_access_token = AsyncMock(
            side_effect=OAuthError(error="invalid_request", description="Missing code")
        )

        with pytest.raises(HTTPException) as exc_info:
            await auth_controller.get_login_callback(request, response)

        assert exc_info.value.status_code == 400

    async def test_get_login_callback_missing_state(
        self, auth_controller, create_mock_request, create_mock_response, mock_oauth
    ):
        """Test login callback without state."""
        request = create_mock_request()
        response = create_mock_response()

        # Configure mock (mock_oauth IS the oidc client mock from fixture)
        mock_oauth.authorize_access_token = AsyncMock(
            side_effect=OAuthError(error="invalid_request", description="Missing state")
        )

        with pytest.raises(HTTPException) as exc_info:
            await auth_controller.get_login_callback(request, response)

        assert exc_info.value.status_code == 400

    async def test_get_login_callback_invalid_state(
        self, auth_controller, create_mock_request, create_mock_response, mock_oauth
    ):
        """Test login callback with invalid state."""
        request = create_mock_request()
        response = create_mock_response()

        # Configure mock (mock_oauth IS the oidc client mock from fixture)
        mock_oauth.authorize_access_token = AsyncMock(
            side_effect=OAuthError(error="invalid_request", description="Invalid state")
        )

        with pytest.raises(HTTPException) as exc_info:
            await auth_controller.get_login_callback(request, response)

        assert exc_info.value.status_code == 400

    async def test_get_login_callback_token_exchange_failure(
        self,
        auth_controller,
        create_mock_request,
        create_mock_response,
        mock_config,
        mock_oauth,
    ):
        """Test login callback when token exchange fails."""
        request = create_mock_request()
        response = create_mock_response()

        # Configure mock to raise an exception
        mock_oauth.authorize_access_token = AsyncMock(side_effect=Exception("Token exchange failed"))

        with pytest.raises(HTTPException) as exc_info:
            await auth_controller.get_login_callback(request, response)

        assert exc_info.value.status_code == 500

    async def test_get_logout(self, auth_controller, create_mock_request, mock_config):
        """Test logout endpoint."""
        request = create_mock_request(
            query_params={"redirectTo": f"https://{mock_config.base_domain}/"},
            session_id="test-session-id",
        )

        response = await auth_controller.get_logout(request)

        # Should store redirect_to in session and redirect to Oidc
        assert request.session["logout_redirect_to"] == f"https://{mock_config.base_domain}/"
        assert response.status_code == 303

    async def test_get_logout_callback(self, auth_controller, create_mock_request, mock_config):
        """Test logout callback."""
        redirect_to = f"https://{mock_config.base_domain}/"
        request = create_mock_request()
        request.session["logout_redirect_to"] = redirect_to

        response = await auth_controller.get_logout_callback(request)

        # Should redirect to specified URL
        assert response.status_code == 303
        assert redirect_to in response.headers["location"]

        # Should clear redirect from session
        assert "logout_redirect_to" not in request.session

    async def test_get_logout_callback_no_state(self, auth_controller, create_mock_request, mock_config):
        """Test logout callback without state."""
        request = create_mock_request(query_params={})

        response = await auth_controller.get_logout_callback(request)

        # Should redirect to home
        assert response.status_code == 303
        assert mock_config.base_domain in response.headers["location"]

    async def test_is_valid_redirect_url_https(self, auth_controller, mock_config):
        """Test URL validation for HTTPS URLs."""
        valid_url = f"https://{mock_config.base_domain}/dashboard"
        assert auth_controller._is_valid_redirect_url(valid_url)

    async def test_is_valid_redirect_url_different_domain(self, auth_controller):
        """Test URL validation rejects different domains in non-local mode."""
        from unittest.mock import patch

        from playground_backend.config import Config

        # Mock Config.is_local to return False so domain validation is enforced
        with patch.object(Config, "is_local", return_value=False):
            invalid_url = "https://evil.com/phishing"
            assert not auth_controller._is_valid_redirect_url(invalid_url)

    async def test_is_valid_redirect_url_http_in_dev(self, auth_controller):
        """Test URL validation allows HTTP in dev mode."""
        # Controller should be in dev mode from test config
        valid_url = "http://localhost:9999/dashboard"
        assert auth_controller._is_valid_redirect_url(valid_url)

    async def test_is_valid_redirect_url_javascript(self, auth_controller):
        """Test URL validation rejects javascript: URLs."""
        invalid_url = "javascript:alert(1)"
        assert not auth_controller._is_valid_redirect_url(invalid_url)

    async def test_is_valid_redirect_url_rejects_login_callback(self, auth_controller, mock_config):
        """Test URL validation rejects redirect to login-callback to prevent loops."""
        invalid_url = f"http://{mock_config.base_domain}/api/v1/auth/login-callback"
        assert not auth_controller._is_valid_redirect_url(invalid_url)

    async def test_is_valid_redirect_url_rejects_login(self, auth_controller, mock_config):
        """Test URL validation rejects redirect to login to prevent loops."""
        invalid_url = f"http://{mock_config.base_domain}/api/v1/auth/login"
        assert not auth_controller._is_valid_redirect_url(invalid_url)

    async def test_is_valid_redirect_url_rejects_logout_callback(self, auth_controller, mock_config):
        """Test URL validation rejects redirect to logout-callback to prevent loops."""
        invalid_url = f"http://{mock_config.base_domain}/api/v1/auth/logout-callback"
        assert not auth_controller._is_valid_redirect_url(invalid_url)

    async def test_is_valid_redirect_url_rejects_logout(self, auth_controller, mock_config):
        """Test URL validation rejects redirect to logout to prevent loops."""
        invalid_url = f"http://{mock_config.base_domain}/api/v1/auth/logout"
        assert not auth_controller._is_valid_redirect_url(invalid_url)

    async def test_get_login_callback_clears_session_redirect_to(
        self,
        auth_controller,
        create_mock_request,
        create_mock_response,
        mock_config,
        oidc_userinfo_response,
        mock_oauth,
        mock_db_session_factory,
    ):
        """Test that login callback clears redirect_to from session to prevent reuse."""
        redirect_to = f"https://{mock_config.base_domain}/dashboard"
        request = create_mock_request()
        request.session["redirect_to"] = redirect_to
        response = create_mock_response()

        # Mock user_service.upsert_user to return a mock user
        mock_user = MagicMock(id="user-123", email="test@example.com")
        auth_controller.user_service.upsert_user = AsyncMock(return_value=mock_user)

        # Configure mock
        mock_token = {
            "access_token": "test_access_token",
            "id_token": "test_id_token",
            "refresh_token": "test_refresh_token",
            "userinfo": oidc_userinfo_response,
        }
        mock_oauth.authorize_access_token = AsyncMock(return_value=mock_token)

        await auth_controller.get_login_callback(request, response)

        # redirect_to should be cleared from session
        assert "redirect_to" not in request.session

    async def test_get_login_callback_uses_default_redirect_when_invalid(
        self,
        auth_controller,
        create_mock_request,
        create_mock_response,
        mock_config,
        oidc_userinfo_response,
        mock_oauth,
        mock_db_session_factory,
    ):
        """Test that login callback uses default redirect when stored redirect_to is invalid."""
        # Store an invalid redirect_to (contains auth path)
        invalid_redirect = f"http://{mock_config.base_domain}/api/v1/auth/login-callback?foo=bar"
        request = create_mock_request()
        request.session["redirect_to"] = invalid_redirect
        response = create_mock_response()

        # Mock user_service.upsert_user to return a mock user
        mock_user = MagicMock(id="user-123", email="test@example.com")
        auth_controller.user_service.upsert_user = AsyncMock(return_value=mock_user)

        # Configure mock
        mock_token = {
            "access_token": "test_access_token",
            "id_token": "test_id_token",
            "refresh_token": "test_refresh_token",
            "userinfo": oidc_userinfo_response,
        }
        mock_oauth.authorize_access_token = AsyncMock(return_value=mock_token)

        result = await auth_controller.get_login_callback(request, response)

        # Should redirect to default URL, not the invalid one
        assert result.status_code == 303
        assert f"https://{mock_config.base_domain}/" in result.headers["location"]
        assert "/api/v1/auth/login-callback" not in result.headers["location"]

    async def test_get_login_callback_uses_default_redirect_when_missing(
        self,
        auth_controller,
        create_mock_request,
        create_mock_response,
        mock_config,
        oidc_userinfo_response,
        mock_oauth,
        mock_db_session_factory,
    ):
        """Test that login callback uses default redirect when redirect_to is missing from session."""
        request = create_mock_request()
        # Don't set redirect_to in session
        response = create_mock_response()

        # Mock user_service.upsert_user to return a mock user
        mock_user = MagicMock(id="user-123", email="test@example.com")
        auth_controller.user_service.upsert_user = AsyncMock(return_value=mock_user)

        # Configure mock
        mock_token = {
            "access_token": "test_access_token",
            "id_token": "test_id_token",
            "refresh_token": "test_refresh_token",
            "userinfo": oidc_userinfo_response,
        }
        mock_oauth.authorize_access_token = AsyncMock(return_value=mock_token)

        result = await auth_controller.get_login_callback(request, response)

        # Should redirect to default URL
        assert result.status_code == 303
        assert f"https://{mock_config.base_domain}/" in result.headers["location"]

    async def test_get_login_callback_clears_session_on_oauth_error(
        self,
        auth_controller,
        create_mock_request,
        create_mock_response,
        mock_oauth,
    ):
        """Test that login callback clears session on OAuth error to prevent state reuse."""
        request = create_mock_request()
        request.session["redirect_to"] = "https://example.com/dashboard"
        request.session["some_other_key"] = "some_value"
        response = create_mock_response()

        # Configure mock to raise OAuth error
        mock_oauth.authorize_access_token = AsyncMock(
            side_effect=OAuthError(
                error="mismatching_state", description="CSRF Warning! State not equal in request and response."
            )
        )

        with pytest.raises(HTTPException):
            await auth_controller.get_login_callback(request, response)

        # Session should be cleared on error
        assert len(request.session) == 0

    async def test_get_login_callback_clears_session_on_generic_error(
        self,
        auth_controller,
        create_mock_request,
        create_mock_response,
        mock_oauth,
    ):
        """Test that login callback clears session on generic error to prevent state reuse."""
        request = create_mock_request()
        request.session["redirect_to"] = "https://example.com/dashboard"
        request.session["some_other_key"] = "some_value"
        response = create_mock_response()

        # Configure mock to raise generic exception
        mock_oauth.authorize_access_token = AsyncMock(side_effect=Exception("Unexpected error"))

        with pytest.raises(HTTPException):
            await auth_controller.get_login_callback(request, response)

        # Session should be cleared on error
        assert len(request.session) == 0

    async def test_get_login_rejects_redirect_to_callback_url(self, auth_controller, create_mock_request, mock_config):
        """Test login endpoint rejects redirectTo containing login-callback path."""
        invalid_redirect = f"http://{mock_config.base_domain}/api/v1/auth/login-callback"
        request = create_mock_request(query_params={"redirectTo": invalid_redirect})

        with pytest.raises(HTTPException) as exc_info:
            await auth_controller.get_login(request)

        assert exc_info.value.status_code == 422

    async def test_get_login_rejects_redirect_to_login_url(self, auth_controller, create_mock_request, mock_config):
        """Test login endpoint rejects redirectTo containing login path."""
        invalid_redirect = f"http://{mock_config.base_domain}/api/v1/auth/login?foo=bar"
        request = create_mock_request(query_params={"redirectTo": invalid_redirect})

        with pytest.raises(HTTPException) as exc_info:
            await auth_controller.get_login(request)

        assert exc_info.value.status_code == 422
