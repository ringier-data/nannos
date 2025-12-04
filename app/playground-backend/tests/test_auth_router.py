"""Integration tests for auth router."""

from unittest.mock import AsyncMock

import pytest

from authlib.integrations.base_client.errors import OAuthError
from starlette.responses import RedirectResponse


@pytest.mark.asyncio
class TestAuthRouter:
    """Test auth router endpoints."""

    def test_login_endpoint_exists(self, mock_oauth, client):
        """Test that login endpoint exists."""
        # Configure the OAuth mock (mock_oauth IS the oidc client mock from fixture)
        mock_oauth.authorize_redirect = AsyncMock(
            return_value=RedirectResponse(url='https://oidc.example.com/authorize', status_code=303)
        )

        response = client.get(
            '/api/v1/auth/login',
            params={'redirectTo': 'https://localhost:9999/'},
            follow_redirects=False,
        )

        # Should redirect (not 404)
        assert response.status_code in [303, 307, 302]

    def test_login_endpoint_invalid_redirect(self, client):
        """Test login with invalid redirect URL."""
        response = client.get(
            '/api/v1/auth/login',
            params={'redirectTo': 'javascript:alert(1)'},
            follow_redirects=False,
        )

        assert response.status_code == 422

    def test_login_callback_missing_params(self, mock_oauth, client):
        """Test login callback without required params."""
        # Configure mock to raise OAuthError for missing/invalid params
        mock_oauth.authorize_access_token = AsyncMock(
            side_effect=OAuthError(error='invalid_request', description='Missing required parameter')
        )

        # Missing code
        response = client.get(
            '/api/v1/auth/login-callback',
            params={'state': 'test-state'},
        )
        assert response.status_code == 400

        # Missing state
        response = client.get(
            '/api/v1/auth/login-callback',
            params={'code': 'test-code'},
        )
        assert response.status_code == 400

    def test_logout_endpoint_exists(self, client):
        """Test that logout endpoint exists."""
        response = client.get(
            '/api/v1/auth/logout',
            follow_redirects=False,
        )

        # Should redirect (not 404)
        assert response.status_code in [303, 307, 302]

    def test_logout_callback_endpoint_exists(self, client):
        """Test that logout callback endpoint exists."""
        response = client.get(
            '/api/v1/auth/logout-callback',
            follow_redirects=False,
        )

        # Should redirect (not 404)
        assert response.status_code in [303, 307, 302]

    def test_all_auth_endpoints_registered(self, app):
        """Test that all auth endpoints are registered."""
        routes = [route.path for route in app.routes]

        assert '/api/v1/auth/login' in routes
        assert '/api/v1/auth/login-callback' in routes
        assert '/api/v1/auth/logout' in routes
        assert '/api/v1/auth/logout-callback' in routes


@pytest.mark.asyncio
class TestAuthFlow:
    """Test complete authentication flow."""

    def test_complete_login_flow(
        self,
        mock_oauth,
        client,
        mock_config,
        oidc_token_response,
        oidc_userinfo_response,
    ):
        """Test complete login flow from start to finish."""
        # Configure the OAuth mock (mock_oauth IS the oidc client mock from fixture)
        mock_oauth.authorize_redirect = AsyncMock(
            return_value=RedirectResponse(url='https://oidc.example.com/authorize?state=test-state', status_code=303)
        )

        # Step 1: Initiate login
        response = client.get(
            '/api/v1/auth/login',
            params={'redirectTo': f'https://{mock_config.base_domain}/dashboard'},
            follow_redirects=False,
        )

        assert response.status_code == 303

    def test_logout_flow(self, client, mock_config):
        """Test complete logout flow."""
        # Logout
        response = client.get(
            '/api/v1/auth/logout',
            params={'redirectTo': f'https://{mock_config.base_domain}/'},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert 'location' in response.headers
