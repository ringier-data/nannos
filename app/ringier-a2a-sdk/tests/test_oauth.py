"""Tests for OAuth2 client components."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from authlib.oauth2.rfc6749 import OAuth2Token

from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.oauth.client import (
    ClientCredentialsError,
    TokenExchangeError,
    TokenRefreshError,
)


class TestOidcOAuth2Client:
    """Tests for OidcOAuth2Client class."""

    @pytest.mark.asyncio
    async def test_init(self):
        """Test initialization."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        assert client.client_id == "orchestrator"
        assert client.client_secret == "secret123"
        assert client.issuer == "https://login.example.com/realms/test"
        assert client._oauth_client is None
        assert client._token_cache == {}
        assert client.token_leeway == 600  # Default leeway

    @pytest.mark.asyncio
    async def test_init_custom_leeway(self):
        """Test initialization with custom token leeway."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
            token_leeway=300,
        )

        assert client.token_leeway == 300

    @pytest.mark.asyncio
    async def test_get_token_success(self):
        """Test successful token fetch using client credentials."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Mock the OAuth2Token
        mock_token = OAuth2Token(
            {
                "access_token": "test-token-123",
                "token_type": "Bearer",
                "expires_in": 300,
                "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            }
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(return_value=mock_token)
                mock_oauth_client_class.return_value = mock_oauth_client

                token = await client.get_token(audience="agent-1")

                assert token == "test-token-123"
                mock_oauth_client.fetch_token.assert_called_once_with(audience="agent-1")

    @pytest.mark.asyncio
    async def test_get_token_caching(self):
        """Test that get_token uses cached tokens."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Mock a non-expired token and put it in cache
        mock_token = OAuth2Token(
            {
                "access_token": "cached-token",
                "token_type": "Bearer",
                "expires_in": 300,
                "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            }
        )
        client._token_cache["agent-1"] = mock_token

        # Mock metadata to avoid discovery call
        client._metadata = {"token_endpoint": "https://login.example.com/realms/test/protocol/openid-connect/token"}

        with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
            mock_oauth_client = Mock()
            mock_oauth_client.fetch_token = AsyncMock()
            mock_oauth_client_class.return_value = mock_oauth_client

            token = await client.get_token(audience="agent-1")

            assert token == "cached-token"
            # Should not call fetch_token when cached token is valid
            mock_oauth_client.fetch_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_token_per_audience(self):
        """Test that tokens are managed separately per audience."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()

                # Mock fetch_token to return different tokens based on audience parameter
                def fetch_token_side_effect(audience):
                    return OAuth2Token(
                        {
                            "access_token": f"token-{audience}",
                            "token_type": "Bearer",
                            "expires_in": 300,
                            "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                        }
                    )

                mock_oauth_client.fetch_token = AsyncMock(side_effect=fetch_token_side_effect)
                mock_oauth_client_class.return_value = mock_oauth_client

                token1 = await client.get_token(audience="agent-1")
            token2 = await client.get_token(audience="agent-2")

            assert token1 == "token-agent-1"
            assert token2 == "token-agent-2"
            # Should have both in cache
            assert len(client._token_cache) == 2

    @pytest.mark.asyncio
    async def test_get_token_expired_refresh(self):
        """Test that expired tokens are refreshed."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
            token_leeway=60,
        )

        # Put an expired token in cache
        expired_token = OAuth2Token(
            {
                "access_token": "expired-token",
                "token_type": "Bearer",
                "expires_in": 300,
                "expires_at": int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()),
            }
        )
        client._token_cache["agent-1"] = expired_token

        # Mock fresh token
        fresh_token = OAuth2Token(
            {
                "access_token": "fresh-token",
                "token_type": "Bearer",
                "expires_in": 300,
                "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
            }
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(return_value=fresh_token)
                mock_oauth_client_class.return_value = mock_oauth_client

                token = await client.get_token(audience="agent-1")

                assert token == "fresh-token"
                mock_oauth_client.fetch_token.assert_called_once_with(audience="agent-1")

    @pytest.mark.asyncio
    async def test_clear_cache_specific_audience(self):
        """Test clearing cache for specific audience."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Populate cache
        client._token_cache["agent-1"] = OAuth2Token(
            {"access_token": "token1", "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}
        )
        client._token_cache["agent-2"] = OAuth2Token(
            {"access_token": "token2", "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}
        )

        # Clear only agent-1
        client.clear_cache(audience="agent-1")

        assert "agent-1" not in client._token_cache
        assert "agent-2" in client._token_cache

    @pytest.mark.asyncio
    async def test_clear_cache_all(self):
        """Test clearing all cached tokens."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Populate cache
        client._token_cache["agent-1"] = OAuth2Token(
            {"access_token": "token1", "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}
        )
        client._token_cache["agent-2"] = OAuth2Token(
            {"access_token": "token2", "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}
        )

        # Clear all
        client.clear_cache()

        assert len(client._token_cache) == 0

    @pytest.mark.asyncio
    async def test_get_token_error_handling(self):
        """Test that errors are properly wrapped."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(side_effect=Exception("Network error"))
                mock_oauth_client_class.return_value = mock_oauth_client

                with pytest.raises(ClientCredentialsError, match="Network error"):
                    await client.get_token(audience="agent-1")

    @pytest.mark.asyncio
    async def test_exchange_token_success(self):
        """Test successful token exchange."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Mock exchanged token
        exchanged_token = OAuth2Token(
            {
                "access_token": "exchanged-token-123",
                "token_type": "Bearer",
                "expires_in": 300,
            }
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(return_value=exchanged_token)
                mock_oauth_client_class.return_value = mock_oauth_client

                token = await client.exchange_token(
                    subject_token="user-token",
                    target_client_id="target-service",
                )

                assert token == "exchanged-token-123"
                # Verify RFC 8693 parameters
                call_kwargs = mock_oauth_client.fetch_token.call_args[1]
                assert call_kwargs["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
                assert call_kwargs["subject_token"] == "user-token"
                assert call_kwargs["audience"] == "target-service"

    @pytest.mark.asyncio
    async def test_exchange_token_with_scopes(self):
        """Test token exchange with requested scopes."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        exchanged_token = OAuth2Token({"access_token": "token", "token_type": "Bearer"})

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(return_value=exchanged_token)
                mock_oauth_client_class.return_value = mock_oauth_client

                await client.exchange_token(
                    subject_token="user-token",
                    target_client_id="target-service",
                    requested_scopes=["read", "write"],
                )

                call_kwargs = mock_oauth_client.fetch_token.call_args[1]
                assert call_kwargs["scope"] == "read write"

    @pytest.mark.asyncio
    async def test_exchange_token_error(self):
        """Test token exchange error handling."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(side_effect=Exception("Exchange failed"))
                mock_oauth_client_class.return_value = mock_oauth_client

                with pytest.raises(TokenExchangeError, match="Exchange failed"):
                    await client.exchange_token(
                        subject_token="user-token",
                        target_client_id="target-service",
                    )

    @pytest.mark.asyncio
    async def test_refresh_token_success(self):
        """Test successful token refresh."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        refreshed_token = OAuth2Token(
            {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "id_token": "new-id-token",
                "expires_in": 300,
            }
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(return_value=refreshed_token)
                mock_oauth_client_class.return_value = mock_oauth_client

                result = await client.refresh_token("old-refresh-token")

                assert result["access_token"] == "new-access-token"
                assert result["refresh_token"] == "new-refresh-token"
                assert result["id_token"] == "new-id-token"
                assert result["expires_in"] == 300

    @pytest.mark.asyncio
    async def test_refresh_token_error(self):
        """Test token refresh error handling."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        with patch.object(client, "_discover_metadata", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = {"token_endpoint": "https://login.example.com/token"}

            with patch("ringier_a2a_sdk.oauth.base.AsyncOAuth2Client") as mock_oauth_client_class:
                mock_oauth_client = Mock()
                mock_oauth_client.fetch_token = AsyncMock(side_effect=Exception("Refresh failed"))
                mock_oauth_client_class.return_value = mock_oauth_client

                with pytest.raises(TokenRefreshError, match="Refresh failed"):
                    await client.refresh_token("old-refresh-token")

    @pytest.mark.asyncio
    async def test_close(self):
        """Test that close clears cache and closes client."""
        client = OidcOAuth2Client(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Populate cache
        client._token_cache["agent-1"] = OAuth2Token({"access_token": "token1"})

        # Mock the oauth client
        mock_oauth_client = AsyncMock()
        client._oauth_client = mock_oauth_client

        await client.close()

        # Cache should be cleared
        assert len(client._token_cache) == 0
        # OAuth client should be closed
        mock_oauth_client.aclose.assert_called_once()
