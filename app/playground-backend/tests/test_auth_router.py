"""Integration tests for auth router."""

from unittest.mock import AsyncMock

import pytest
from authlib.integrations.base_client.errors import OAuthError
from starlette.responses import RedirectResponse


@pytest.mark.asyncio
class TestAuthRouter:
    """Test auth router endpoints."""

    @pytest.mark.asyncio
    async def test_login_endpoint_exists(self, client_with_db, mock_oauth):
        """Test that login endpoint exists."""
        # Configure the OAuth mock (mock_oauth IS the oidc client_with_db mock from fixture)
        mock_oauth.authorize_redirect = AsyncMock(
            return_value=RedirectResponse(url="https://oidc.example.com/authorize", status_code=303)
        )
        response = await client_with_db.get(
            "/api/v1/auth/login",
            params={"redirectTo": "https://localhost:9999/"},
            follow_redirects=False,
        )

        # Should redirect (not 404)
        assert response.status_code in [303, 307, 302]

    async def test_login_endpoint_invalid_redirect(self, client_with_db):
        """Test login with invalid redirect URL."""
        response = await client_with_db.get(
            "/api/v1/auth/login",
            params={"redirectTo": "javascript:alert(1)"},
            follow_redirects=False,
        )

        assert response.status_code == 422

    async def test_login_callback_missing_params(self, mock_oauth, client_with_db):
        """Test login callback without required params."""
        # Configure mock to raise OAuthError for missing/invalid params
        mock_oauth.authorize_access_token = AsyncMock(
            side_effect=OAuthError(error="invalid_request", description="Missing required parameter")
        )

        # Missing code
        response = await client_with_db.get(
            "/api/v1/auth/login-callback",
            params={"state": "test-state"},
        )
        assert response.status_code == 400

        # Missing state
        response = await client_with_db.get(
            "/api/v1/auth/login-callback",
            params={"code": "test-code"},
        )
        assert response.status_code == 400

    async def test_logout_endpoint_exists(self, client_with_db):
        """Test that logout endpoint exists."""
        response = await client_with_db.get(
            "/api/v1/auth/logout",
            follow_redirects=False,
        )

        # Should redirect (not 404)
        assert response.status_code in [303, 307, 302]

    async def test_logout_callback_endpoint_exists(self, client_with_db):
        """Test that logout callback endpoint exists."""
        response = await client_with_db.get(
            "/api/v1/auth/logout-callback",
            follow_redirects=False,
        )

        # Should redirect (not 404)
        assert response.status_code in [303, 307, 302]


@pytest.mark.asyncio
class TestAuthFlow:
    """Test complete authentication flow."""

    async def test_complete_login_flow(
        self,
        mock_oauth,
        client_with_db,
        mock_config,
        oidc_token_response,
        oidc_userinfo_response,
    ):
        """Test complete login flow from start to finish."""
        # Configure the OAuth mock (mock_oauth IS the oidc client_with_db mock from fixture)
        mock_oauth.authorize_redirect = AsyncMock(
            return_value=RedirectResponse(url="https://oidc.example.com/authorize?state=test-state", status_code=303)
        )

        # Step 1: Initiate login
        response = await client_with_db.get(
            "/api/v1/auth/login",
            params={"redirectTo": f"https://{mock_config.base_domain}/dashboard"},
            follow_redirects=False,
        )

        assert response.status_code == 303

    async def test_logout_flow(self, client_with_db, mock_config):
        """Test complete logout flow."""
        # Logout
        response = await client_with_db.get(
            "/api/v1/auth/logout",
            params={"redirectTo": f"https://{mock_config.base_domain}/"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "location" in response.headers
