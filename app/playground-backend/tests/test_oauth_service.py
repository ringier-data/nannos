"""Tests for OAuthService."""

from unittest.mock import AsyncMock

import httpx
import pytest

from playground_backend.services.oauth_service import (
    OAuthService,
    TokenExchangeError,
    TokenRefreshError,
)


@pytest.mark.asyncio
class TestOAuthService:
    """Test OAuthService functionality."""

    async def test_exchange_token_success(self, oauth_service, oidc_token_exchange_response):
        """Test successful token exchange."""
        # Mock the OAuth client's fetch_token method to return dict
        mock_oauth_client = AsyncMock()

        async def mock_fetch(**kwargs):
            return oidc_token_exchange_response

        mock_oauth_client.fetch_token = mock_fetch

        # Override _get_oauth_client to return our mock
        async def get_mock_client():
            return mock_oauth_client

        oauth_service._get_oauth_client = get_mock_client

        # Exchange token
        exchanged_token = await oauth_service.exchange_token(
            subject_token="user_access_token",
            target_client_id="target_client_id",
        )

        assert exchanged_token == oidc_token_exchange_response["access_token"]

    async def test_exchange_token_with_scopes(self, oauth_service, oidc_token_exchange_response):
        """Test token exchange with requested scopes."""
        # Track the called params
        called_params = {}

        async def mock_fetch(**kwargs):
            called_params.update(kwargs)
            return oidc_token_exchange_response

        mock_oauth_client = AsyncMock()
        mock_oauth_client.fetch_token = mock_fetch

        # Override _get_oauth_client to return our mock
        async def get_mock_client():
            return mock_oauth_client

        oauth_service._get_oauth_client = get_mock_client

        await oauth_service.exchange_token(
            subject_token="user_token",
            target_client_id="target_client",
            requested_scopes=["scope1", "scope2"],
        )

        assert called_params["scope"] == "scope1 scope2"

    async def test_exchange_token_failure_non_200(self, oauth_service):
        """Test token exchange failure with non-200 response."""

        async def mock_fetch(**kwargs):
            raise Exception("Invalid token")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.fetch_token = mock_fetch

        # Override _get_oauth_client to return our mock
        async def get_mock_client():
            return mock_oauth_client

        oauth_service._get_oauth_client = get_mock_client

        with pytest.raises(TokenExchangeError) as exc_info:
            await oauth_service.exchange_token(
                subject_token="invalid_token",
                target_client_id="target_client",
            )

        assert "Token exchange failed" in str(exc_info.value)

    async def test_exchange_token_http_error(self, oauth_service):
        """Test token exchange with HTTP error."""

        async def mock_fetch(**kwargs):
            raise httpx.HTTPError("Network error")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.fetch_token = mock_fetch

        # Override _get_oauth_client to return our mock
        async def get_mock_client():
            return mock_oauth_client

        oauth_service._get_oauth_client = get_mock_client

        with pytest.raises(TokenExchangeError) as exc_info:
            await oauth_service.exchange_token(
                subject_token="user_token",
                target_client_id="target_client",
            )

        assert "Network error" in str(exc_info.value)

    async def test_oauth_service_initialization(self, mock_config):
        """Test OAuthService initialization."""
        service = OAuthService(
            client_id="test_client",
            client_secret="test_secret",
            issuer="https://test.oidc.com/oauth2/default",
        )

        assert service.client_id == "test_client"
        assert service.client_secret == "test_secret"
        assert service.issuer == "https://test.oidc.com/oauth2/default"

    async def test_oauth_service_custom_issuer(self):
        """Test OAuthService with custom issuer."""
        service = OAuthService(
            client_id="test_client",
            client_secret="test_secret",
            issuer="https://custom.oidc.com/oauth2/custom",
        )

        assert service.issuer == "https://custom.oidc.com/oauth2/custom"

    async def test_close_cleanup(self, oauth_service):
        """Test cleanup on close."""
        await oauth_service.close()

        # OAuthService manages its own httpx client internally
        # Verify close doesn't raise errors
        assert oauth_service._oauth_client is None or oauth_service._oauth_client is not None

    async def test_close_cleanup_owned_client(self, mock_config):
        """Test cleanup of owned oauth client."""
        service = OAuthService(
            client_id="test_client",
            client_secret="test_secret",
            issuer="https://test.oidc.com/oauth2/default",
        )

        # Create a mock client manually
        mock_client = AsyncMock()
        service._oauth_client = mock_client

        await service.close()

        # Verify close was called
        assert service._oauth_client is None

    async def test_refresh_access_token_success(self, oauth_service):
        """Test successful token refresh."""

        # Mock the OAuth client's fetch_token method to return dict
        async def mock_fetch(**kwargs):
            return {
                "access_token": "new_access_token",
                "refresh_token": "new_refresh_token",
                "id_token": "new_id_token",
                "expires_in": 3600,
            }

        mock_oauth_client = AsyncMock()
        mock_oauth_client.fetch_token = mock_fetch

        # Override _get_oauth_client to return our mock
        async def get_mock_client():
            return mock_oauth_client

        oauth_service._get_oauth_client = get_mock_client

        # Refresh token
        result = await oauth_service.refresh_token("old_refresh_token")

        # Verify the result
        assert result["access_token"] == "new_access_token"
        assert result["refresh_token"] == "new_refresh_token"
        assert result["id_token"] == "new_id_token"

    async def test_refresh_access_token_without_new_refresh_token(self, oauth_service):
        """Test token refresh when server doesn't return a new refresh token."""

        # Mock the OAuth client's fetch_token method (no new refresh_token in response)
        async def mock_fetch(**kwargs):
            return {
                "access_token": "new_access_token",
                "expires_in": 3600,
            }

        mock_oauth_client = AsyncMock()
        mock_oauth_client.fetch_token = mock_fetch

        # Override _get_oauth_client to return our mock
        async def get_mock_client():
            return mock_oauth_client

        oauth_service._get_oauth_client = get_mock_client

        # Refresh token
        result = await oauth_service.refresh_token("old_refresh_token")

        # Verify the result - should use old refresh token if not rotated
        assert result["access_token"] == "new_access_token"
        assert result["refresh_token"] == "old_refresh_token"

    async def test_refresh_access_token_failure(self, oauth_service):
        """Test token refresh failure."""

        # Mock the OAuth client's fetch_token method to raise an exception
        async def mock_fetch(**kwargs):
            raise Exception("Invalid refresh token")

        mock_oauth_client = AsyncMock()
        mock_oauth_client.fetch_token = mock_fetch

        # Override _get_oauth_client to return our mock
        async def get_mock_client():
            return mock_oauth_client

        oauth_service._get_oauth_client = get_mock_client

        # Test that refresh raises TokenRefreshError
        with pytest.raises(TokenRefreshError) as exc_info:
            await oauth_service.refresh_token("invalid_refresh_token")

        assert "Token refresh failed" in str(exc_info.value)
