"""Unit tests for OrchestratorAuth middleware.

Tests cover:
- Token exchange for orchestrator requests
- Token refresh when expired
- Cookie caching with DynamoDB backing
- Custom header forwarding
- Orchestrator vs non-orchestrator request detection
"""

import os

# Set up boto3 mock environment before any imports that use boto3
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from console_backend.middleware.orchestrator_auth import OrchestratorAuth


@pytest.fixture
def mock_oauth_service():
    """Mock OAuth service."""
    service = MagicMock()
    service.exchange_token = AsyncMock(return_value="exchanged_token_123")
    service.refresh_token = AsyncMock(
        return_value={
            "access_token": "refreshed_token_456",
            "refresh_token": "new_refresh_token",
            "id_token": "new_id_token",
            "expires_in": 3600,
        }
    )
    return service


@pytest.fixture
def mock_session_service():
    """Mock session service."""
    service = MagicMock()

    # Mock session object with expiry time
    mock_session = MagicMock()
    mock_session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    mock_session.refresh_token = "refresh_token_123"
    mock_session.user_id = "user_123"
    mock_session.id_token = "id_token_123"

    service.get_session = AsyncMock(return_value=mock_session)
    service.update_session = AsyncMock()
    return service


@pytest.fixture
def mock_cookie_cache():
    """Mock orchestrator cookie cache."""
    cache = MagicMock()
    cache.get_cookie = AsyncMock(return_value=None)
    cache.set_cookie = AsyncMock()
    cache.clear_cookie = AsyncMock()
    cache.delete = AsyncMock()
    cache.invalidate = MagicMock()
    return cache


class TestOrchestratorAuthTokenExchange:
    """Test token exchange functionality."""

    @pytest.mark.asyncio
    async def test_token_exchange_on_first_request(self, mock_oauth_service, mock_session_service, mock_cookie_cache):
        """Test that token is exchanged on the first orchestrator request."""
        # Create auth handler
        auth = OrchestratorAuth(
            user_token="user_token_123",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # Simulate token exchange
        token = await auth._get_orchestrator_token()

        # Assert token exchange was called
        assert token == "exchanged_token_123"
        mock_oauth_service.exchange_token.assert_called_once()
        call_kwargs = mock_oauth_service.exchange_token.call_args.kwargs
        assert call_kwargs["subject_token"] == "user_token_123"

    @pytest.mark.asyncio
    async def test_token_cached_after_first_exchange(self, mock_oauth_service, mock_session_service, mock_cookie_cache):
        """Test that exchanged token is cached for subsequent requests."""
        auth = OrchestratorAuth(
            user_token="user_token_123",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # First call - should exchange
        token1 = await auth._get_orchestrator_token()
        assert mock_oauth_service.exchange_token.call_count == 1

        # Second call - should use cached
        token2 = await auth._get_orchestrator_token()
        assert mock_oauth_service.exchange_token.call_count == 1
        assert token1 == token2

    @pytest.mark.asyncio
    async def test_token_refresh_when_expired(self, mock_oauth_service, mock_session_service, mock_cookie_cache):
        """Test that expired access token is refreshed before exchange."""
        # Mock session with expired token (expires in 30 seconds - below 60 second buffer)
        mock_session = MagicMock()
        mock_session.access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        mock_session.refresh_token = "refresh_token_123"
        mock_session.user_id = "user_123"
        mock_session.id_token = "id_token_123"
        mock_session_service.get_session = AsyncMock(return_value=mock_session)

        auth = OrchestratorAuth(
            user_token="expired_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # Get token - should trigger refresh
        await auth._get_orchestrator_token()

        # Assert refresh was called with the refresh token
        mock_oauth_service.refresh_token.assert_called_once_with("refresh_token_123")
        # Assert session was updated
        mock_session_service.update_session.assert_called_once()
        # Assert orchestrator cookie was cleared
        mock_cookie_cache.clear_cookie.assert_called_once_with("session_123")


class TestOrchestratorAuthCookieManagement:
    """Test orchestrator cookie caching with DynamoDB."""

    @pytest.mark.asyncio
    async def test_get_orchestrator_cookie_from_cache(
        self, mock_oauth_service, mock_session_service, mock_cookie_cache
    ):
        """Test retrieving cached orchestrator cookie."""
        # Setup cache to return a cookie with expiry
        future_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_cookie_cache.get_cookie = AsyncMock(return_value=("orchestrator_session=jwt_value", future_expiry))

        auth = OrchestratorAuth(
            user_token="user_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # Create a mock request to orchestrator
        request = httpx.Request("GET", f"{auth.orchestrator_base_url}/api/test")

        # Run through auth flow using anext()
        auth_flow = auth.async_auth_flow(request)
        authenticated_request = await anext(auth_flow)

        # Assert cache was checked
        mock_cookie_cache.get_cookie.assert_called_once_with("session_123")
        # Assert cookie was added to request
        assert "Cookie" in authenticated_request.headers
        assert authenticated_request.headers["Cookie"] == "orchestrator_session=jwt_value"

    @pytest.mark.asyncio
    async def test_store_orchestrator_cookie_to_cache(
        self, mock_oauth_service, mock_session_service, mock_cookie_cache
    ):
        """Test storing orchestrator cookie to DynamoDB cache."""
        auth = OrchestratorAuth(
            user_token="user_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # Create mock response with Set-Cookie header (no domain to avoid mismatch)
        mock_response = MagicMock()
        mock_response.headers = httpx.Headers(
            [("set-cookie", "orchestrator_session=jwt_value; Path=/; HttpOnly; Secure; Max-Age=900")]
        )

        # Extract and store cookie
        await auth._extract_and_store_cookie(mock_response)

        # Assert cookie was stored (verify set_cookie was called with session_id, value, and expires_at)
        mock_cookie_cache.set_cookie.assert_called_once()
        call_args = mock_cookie_cache.set_cookie.call_args
        assert call_args[0][0] == "session_123"  # session_id
        assert call_args[0][1] == "orchestrator_session=jwt_value"  # cookie value (name=value format)
        assert isinstance(call_args[0][2], datetime)  # expires_at

    @pytest.mark.asyncio
    async def test_extract_cookie_from_response_headers(
        self, mock_oauth_service, mock_session_service, mock_cookie_cache
    ):
        """Test cookie extraction from Set-Cookie header."""
        auth = OrchestratorAuth(
            user_token="user_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # Create mock response with Set-Cookie header
        mock_response = MagicMock()
        mock_response.headers = httpx.Headers(
            [("set-cookie", "orchestrator_session=jwt_token_value; Path=/; HttpOnly; Max-Age=3600")]
        )

        # Extract and store cookie
        await auth._extract_and_store_cookie(mock_response)

        # Assert cookie was stored
        mock_cookie_cache.set_cookie.assert_called_once()
        # Verify the cookie value was extracted correctly (includes "name=value" format)
        call_args = mock_cookie_cache.set_cookie.call_args
        assert call_args[0][1] == "orchestrator_session=jwt_token_value"


class TestOrchestratorAuthRequestDetection:
    """Test orchestrator request detection logic."""

    @pytest.mark.asyncio
    async def test_is_orchestrator_request_with_matching_domain(
        self, mock_oauth_service, mock_session_service, mock_cookie_cache
    ):
        """Test that requests to orchestrator domain are detected."""
        auth = OrchestratorAuth(
            user_token="user_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # Test with matching domain
        url = httpx.URL(auth.orchestrator_base_url + "/api/test")
        assert auth._is_orchestrator_request(url) is True

    @pytest.mark.asyncio
    async def test_is_orchestrator_request_with_localhost(
        self, mock_oauth_service, mock_session_service, mock_cookie_cache
    ):
        """Test that localhost requests are handled correctly in local mode."""
        # Patch config before creating OrchestratorAuth
        with patch("console_backend.middleware.orchestrator_auth.config") as mock_config:
            mock_config.orchestrator.base_domain = "localhost:10001"
            mock_config.orchestrator.is_local.return_value = True
            mock_config.orchestrator.client_id = "test_client_id"

            auth = OrchestratorAuth(
                user_token="user_token",
                session_id="session_123",
                session_service=mock_session_service,
                oauth_service=mock_oauth_service,
                cookie_cache=mock_cookie_cache,
            )

            # Test localhost URL
            url = httpx.URL("http://localhost:10001/api/test")
            assert auth._is_orchestrator_request(url) is True

            # Test 0.0.0.0 URL (should also work in local mode)
            url2 = httpx.URL("http://0.0.0.0:10001/api/test")
            assert auth._is_orchestrator_request(url2) is True

    @pytest.mark.asyncio
    async def test_non_orchestrator_request_passes_through(
        self, mock_oauth_service, mock_session_service, mock_cookie_cache
    ):
        """Test that non-orchestrator requests are not modified."""
        auth = OrchestratorAuth(
            user_token="user_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
        )

        # Test with different domain
        url = httpx.URL("https://example.com/api/test")
        assert auth._is_orchestrator_request(url) is False


class TestOrchestratorAuthCustomHeaders:
    """Test custom header forwarding."""

    @pytest.mark.asyncio
    async def test_custom_headers_added_to_request(self, mock_oauth_service, mock_session_service, mock_cookie_cache):
        """Test that custom headers are forwarded to orchestrator."""
        custom_headers = {"X-Custom-Header": "custom_value"}

        auth = OrchestratorAuth(
            user_token="user_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
            custom_headers=custom_headers,
        )

        # Assert custom headers are stored
        assert auth.custom_headers == custom_headers

    @pytest.mark.asyncio
    async def test_admin_mode_header_forwarded(self, mock_oauth_service, mock_session_service, mock_cookie_cache):
        """Test that X-Admin-Mode header is properly forwarded."""
        custom_headers = {"X-Admin-Mode": "true"}

        auth = OrchestratorAuth(
            user_token="user_token",
            session_id="session_123",
            session_service=mock_session_service,
            oauth_service=mock_oauth_service,
            cookie_cache=mock_cookie_cache,
            custom_headers=custom_headers,
        )

        # Assert X-Admin-Mode header is included
        assert "X-Admin-Mode" in auth.custom_headers
        assert auth.custom_headers["X-Admin-Mode"] == "true"
