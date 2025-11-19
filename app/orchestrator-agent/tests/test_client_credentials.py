"""Tests for OidcClientCredentialsAuth."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from authlib.oauth2.rfc6749 import OAuth2Token

from app.authentication.client_credentials import (
    OidcClientCredentialsAuth,
)


class TestOidcClientCredentialsAuth:
    """Tests for OidcClientCredentialsAuth class with authlib."""

    @pytest.mark.asyncio
    async def test_init(self):
        """Test initialization."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        assert auth.client_id == "orchestrator"
        assert auth.client_secret == "secret123"
        assert auth.issuer == "https://login.example.com/realms/test"
        assert auth._token_endpoint == "https://login.example.com/realms/test/protocol/openid-connect/token"
        assert auth._client is None
        assert auth._token_cache == {}

    @pytest.mark.asyncio
    async def test_get_token_success(self):
        """Test successful token fetch using authlib client."""
        auth = OidcClientCredentialsAuth(
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
                "expires_at": 9999999999,  # Far future
            }
        )

        # Mock AsyncOAuth2Client
        with patch("app.authentication.client_credentials.AsyncOAuth2Client") as mock_client_class:
            mock_client = Mock()
            mock_client.token = None
            mock_client.fetch_token = AsyncMock(return_value=mock_token)
            mock_client_class.return_value = mock_client

            token = await auth.get_token(audience="agent-1")

            assert token == "test-token-123"
            mock_client.fetch_token.assert_called_once_with(audience="agent-1")

    @pytest.mark.asyncio
    async def test_get_token_caching(self):
        """Test that get_token uses cached tokens."""
        auth = OidcClientCredentialsAuth(
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
                "expires_at": 9999999999,  # Far future
            }
        )
        auth._token_cache["agent-1"] = mock_token

        with patch("app.authentication.client_credentials.AsyncOAuth2Client") as mock_client_class:
            mock_client = Mock()
            mock_client.fetch_token = AsyncMock()
            mock_client_class.return_value = mock_client

            token = await auth.get_token(audience="agent-1")

            assert token == "cached-token"
            # Should not call fetch_token when cached token is valid
            mock_client.fetch_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_token_per_audience(self):
        """Test that tokens are managed separately per audience."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        with patch("app.authentication.client_credentials.AsyncOAuth2Client") as mock_client_class:
            mock_client = Mock()

            # Mock fetch_token to return different tokens based on audience parameter
            def fetch_token_side_effect(audience):
                return OAuth2Token(
                    {
                        "access_token": f"token-{audience}",
                        "token_type": "Bearer",
                        "expires_in": 300,
                        "expires_at": 9999999999,
                    }
                )

            mock_client.fetch_token = AsyncMock(side_effect=fetch_token_side_effect)
            mock_client_class.return_value = mock_client

            token1 = await auth.get_token(audience="agent-1")
            token2 = await auth.get_token(audience="agent-2")

            assert token1 == "token-agent-1"
            assert token2 == "token-agent-2"
            # Should have both in cache
            assert len(auth._token_cache) == 2
            # Should only create one client
            mock_client_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_cache_specific_audience(self):
        """Test clearing cache for specific audience."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Populate cache
        auth._token_cache["agent-1"] = OAuth2Token({"access_token": "token1", "expires_at": 9999999999})
        auth._token_cache["agent-2"] = OAuth2Token({"access_token": "token2", "expires_at": 9999999999})

        # Clear only agent-1
        auth.clear_cache(audience="agent-1")

        assert "agent-1" not in auth._token_cache
        assert "agent-2" in auth._token_cache

    @pytest.mark.asyncio
    async def test_clear_cache_all(self):
        """Test clearing all cached tokens."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        # Populate cache
        auth._token_cache["agent-1"] = OAuth2Token({"access_token": "token1", "expires_at": 9999999999})
        auth._token_cache["agent-2"] = OAuth2Token({"access_token": "token2", "expires_at": 9999999999})

        # Clear all
        auth.clear_cache()

        assert len(auth._token_cache) == 0

    @pytest.mark.asyncio
    async def test_client_initialization_params(self):
        """Test that OAuth2 client is initialized with correct parameters."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test",
            client_id="orchestrator",
            client_secret="secret123",
        )

        with patch("app.authentication.client_credentials.AsyncOAuth2Client") as mock_client_class:
            mock_client = Mock()
            mock_client.token = None
            mock_client.fetch_token = AsyncMock(
                return_value=OAuth2Token(
                    {"access_token": "token", "token_type": "Bearer", "expires_in": 300, "expires_at": 9999999999}
                )
            )
            mock_client_class.return_value = mock_client

            await auth.get_token(audience="agent-1")

            # Verify client was created with correct parameters
            mock_client_class.assert_called_once_with(
                client_id="orchestrator",
                client_secret="secret123",
                token_endpoint="https://login.example.com/realms/test/protocol/openid-connect/token",
                token_endpoint_auth_method="client_secret_post",
            )

            # Verify both are cached
            token1_again = await auth.get_token(audience="agent-1")
            token2_again = await auth.get_token(audience="agent-2")

            assert token1_again == "token-agent-1"
            assert token2_again == "token-agent-2"
        """Test that tokens are considered invalid at 90% of expiry."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test", client_id="orchestrator", client_secret="secret123"
        )

        # Token expires in 300 seconds
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
        token_data = {"access_token": "test-token", "expires_at": expires_at.isoformat()}

        # At 0% of lifetime, should be valid
        assert auth._is_token_valid(token_data) is True

        # Create token that's at 89% of lifetime (expires in 33 seconds out of 300)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=33)
        token_data["expires_at"] = expires_at.isoformat()
        assert auth._is_token_valid(token_data) is True

        # Create token that's at 91% of lifetime (expires in 27 seconds out of 300)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=27)
        token_data["expires_at"] = expires_at.isoformat()
        assert auth._is_token_valid(token_data) is False

        # Expired token should be invalid
        expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        token_data["expires_at"] = expires_at.isoformat()
        assert auth._is_token_valid(token_data) is False

    @pytest.mark.asyncio
    async def test_get_token_refreshes_on_expiry(self):
        """Test that get_token refreshes token when it reaches 90% expiry."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test", client_id="orchestrator", client_secret="secret123"
        )

        call_count = 0

        def mock_post_response(url, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = Mock()
            mock_resp.raise_for_status = Mock()

            # First call: short expiry (will be considered expired on second get_token)
            # Second call: normal expiry
            expires_in = 10 if call_count == 1 else 300

            mock_resp.json.return_value = {"access_token": f"token-{call_count}", "expires_in": expires_in}
            return mock_resp

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post = mock_post_response
            mock_client_class.return_value = mock_client

            auth._token_endpoint = "https://login.example.com/realms/test/protocol/openid-connect/token"

            # First call
            token1 = await auth.get_token(audience="agent-1")
            assert token1 == "token-1"
            assert call_count == 1

            # Wait for token to reach 90% expiry threshold (10 seconds * 0.9 = 9 seconds)
            # Since we can't actually wait, we'll manipulate the expires_at
            cached = auth._token_cache["agent-1"]
            # Set expires_at to be past the 90% threshold
            cached["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=0.5)).isoformat()

            # Second call should refresh
            token2 = await auth.get_token(audience="agent-1")
            assert token2 == "token-2"
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_clear_cache_single_audience(self):
        """Test clearing cache for a single audience."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test", client_id="orchestrator", client_secret="secret123"
        )

        # Manually populate cache
        auth._token_cache["agent-1"] = {
            "access_token": "token-1",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        auth._token_cache["agent-2"] = {
            "access_token": "token-2",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

        # Clear cache for agent-1
        auth.clear_cache(audience="agent-1")

        assert "agent-1" not in auth._token_cache
        assert "agent-2" in auth._token_cache

    @pytest.mark.asyncio
    async def test_clear_cache_all_audiences(self):
        """Test clearing cache for all audiences."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test", client_id="orchestrator", client_secret="secret123"
        )

        # Manually populate cache
        auth._token_cache["agent-1"] = {
            "access_token": "token-1",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        auth._token_cache["agent-2"] = {
            "access_token": "token-2",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }

        # Clear all cache
        auth.clear_cache()

        assert len(auth._token_cache) == 0

    @pytest.mark.asyncio
    async def test_fetch_token_http_error(self):
        """Test that HTTP errors are propagated."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test", client_id="orchestrator", client_secret="secret123"
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.post.side_effect = Exception("Network error")
            mock_client_class.return_value = mock_client

            auth._token_endpoint = "https://login.example.com/realms/test/protocol/openid-connect/token"

            with pytest.raises(Exception, match="Network error"):
                await auth._fetch_token(audience="agent-1")

    @pytest.mark.asyncio
    async def test_discover_token_endpoint_missing_field(self):
        """Test OIDC discovery with missing token_endpoint field."""
        auth = OidcClientCredentialsAuth(
            issuer="https://login.example.com/realms/test", client_id="orchestrator", client_secret="secret123"
        )

        mock_response = Mock()
        mock_response.json.return_value = {"issuer": "https://login.example.com/realms/test"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_response
            mock_client_class.return_value = mock_client

            with pytest.raises(ValueError, match="token_endpoint not found"):
                await auth._discover_token_endpoint()
