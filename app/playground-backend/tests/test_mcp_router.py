"""Tests for MCP tools router - token exchange, JSON-RPC, and Gatana integration."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

# Prevent AWS/boto3 local credential path during imports
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from playground_backend.routers.mcp_router import MCPToolsResponse, list_mcp_tools


class TestListMcpTools:
    """Tests for list_mcp_tools endpoint."""

    @pytest.fixture
    def mock_request(self):
        """Create a mock request with access token."""
        request = MagicMock()
        request.state.access_token = "valid_user_token"
        request.state.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        request.state.refresh_token = "valid_refresh_token"
        # No impersonation
        request.state.original_user = None
        # Mock headers.get() method
        request.headers.get = MagicMock(return_value=None)  # No Authorization header
        return request

    @pytest.fixture
    def mock_user(self):
        """Create a mock user."""
        user = MagicMock()
        user.id = "user-123"
        user.email = "test@example.com"
        return user

    @pytest.fixture
    def sample_mcp_response(self):
        """Sample MCP JSON-RPC response."""
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "search_web",
                        "description": "Search the web for information",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                        "server": "search-server",
                    },
                    {
                        "name": "fetch_weather",
                        "description": "Get current weather",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                        },
                        "serverName": "weather-server",  # Alternative field name
                    },
                    {
                        "name": "calculate",
                        "description": None,  # Test missing description
                        "inputSchema": None,  # Test missing schema
                    },
                ]
            },
        }

    @pytest.mark.asyncio
    async def test_successful_token_exchange_and_tool_fetch(self, mock_request, mock_user, sample_mcp_response):
        """Test successful flow: token exchange → MCP request → parse tools."""
        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            # Mock OAuth2 client
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_gateway_token")
            mock_oauth_class.return_value = mock_oauth

            # Mock httpx client
            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.headers = {"content-type": "application/json"}
                mock_response.json.return_value = sample_mcp_response
                mock_response.raise_for_status = MagicMock()

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                # Call endpoint
                result = await list_mcp_tools(mock_request, mock_user)

                # Verify token exchange
                mock_oauth.exchange_token.assert_awaited_once_with(
                    subject_token="valid_user_token",
                    target_client_id="mcp-gateway",
                    requested_scopes=["openid", "profile", "offline_access"],
                )

                # Verify MCP JSON-RPC request
                mock_client.post.assert_awaited_once()
                call_args = mock_client.post.call_args
                assert call_args[0][0] == "https://alloych.gatana.ai/mcp"
                assert call_args[1]["headers"]["Authorization"] == "Bearer mcp_gateway_token"
                assert call_args[1]["json"]["method"] == "tools/list"
                assert call_args[1]["json"]["jsonrpc"] == "2.0"

                # Verify response
                assert isinstance(result, MCPToolsResponse)
                assert len(result.tools) == 3
                assert result.tools[0].name == "search_web"
                assert result.tools[0].description == "Search the web for information"
                assert result.tools[0].server == "search-server"
                assert result.tools[1].server == "weather-server"  # serverName fallback
                assert result.tools[2].description is None  # Missing description

    @pytest.mark.asyncio
    async def test_sse_response_parsing(self, mock_request, mock_user):
        """Test parsing Server-Sent Events (SSE) response format."""
        sse_response_text = 'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"tool1"}]}}\n\n'

        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.headers = {"content-type": "text/event-stream"}
                mock_response.text = sse_response_text
                mock_response.raise_for_status = MagicMock()

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await list_mcp_tools(mock_request, mock_user)

                assert len(result.tools) == 1
                assert result.tools[0].name == "tool1"

    @pytest.mark.asyncio
    async def test_missing_access_token_raises_401(self, mock_user):
        """Test that missing access token raises 401."""
        request = MagicMock()
        request.state.access_token = None
        request.state.original_user = None
        request.headers.get = MagicMock(return_value=None)  # No Authorization header

        with pytest.raises(HTTPException) as exc_info:
            await list_mcp_tools(request, mock_user)

        assert exc_info.value.status_code == 401
        assert "No access token available" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_token_refresh_when_expired(self, mock_user, sample_mcp_response):
        """Test automatic token refresh when access token is expired."""
        # Create request with expired token
        request = MagicMock()
        request.state.access_token = "expired_token"
        request.state.access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        request.state.refresh_token = "valid_refresh_token"
        request.state.original_user = None
        request.headers.get = MagicMock(return_value=None)  # No Authorization header

        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            # Create two separate mock instances for refresh and exchange
            mock_oauth_refresh = AsyncMock()
            mock_oauth_refresh.refresh_token = AsyncMock(
                return_value={
                    "access_token": "refreshed_token",
                    "expires_in": 3600,
                    "refresh_token": "new_refresh_token",
                }
            )

            mock_oauth_exchange = AsyncMock()
            mock_oauth_exchange.exchange_token = AsyncMock(return_value="mcp_token")

            # First call returns refresh client, second call returns exchange client
            mock_oauth_class.side_effect = [mock_oauth_refresh, mock_oauth_exchange]

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.headers = {"content-type": "application/json"}
                mock_response.json.return_value = sample_mcp_response
                mock_response.raise_for_status = MagicMock()

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await list_mcp_tools(request, mock_user)

                # Verify token was refreshed
                mock_oauth_refresh.refresh_token.assert_awaited_once_with("valid_refresh_token")

                # Verify token exchange used refreshed token
                mock_oauth_exchange.exchange_token.assert_awaited_once_with(
                    subject_token="refreshed_token",
                    target_client_id="mcp-gateway",
                    requested_scopes=["openid", "profile", "offline_access"],
                )

                assert len(result.tools) == 3

    @pytest.mark.asyncio
    async def test_refresh_fails_raises_401(self, mock_user):
        """Test that failed token refresh raises 401."""
        request = MagicMock()
        request.state.access_token = "expired_token"
        request.state.access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        request.state.refresh_token = "invalid_refresh_token"
        request.state.original_user = None
        request.headers.get = MagicMock(return_value=None)  # No Authorization header

        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.refresh_token = AsyncMock(side_effect=Exception("Invalid refresh token"))
            mock_oauth_class.return_value = mock_oauth

            with pytest.raises(HTTPException) as exc_info:
                await list_mcp_tools(request, mock_user)

            assert exc_info.value.status_code == 401
            assert "Unable to refresh access token" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_missing_refresh_token_raises_401(self, mock_user):
        """Test that expired token without refresh token raises 401."""
        request = MagicMock()
        request.state.access_token = "expired_token"
        request.state.access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        request.state.refresh_token = None
        request.state.original_user = None
        request.headers.get = MagicMock(return_value=None)  # No Authorization header

        with pytest.raises(HTTPException) as exc_info:
            await list_mcp_tools(request, mock_user)

        assert exc_info.value.status_code == 401
        assert "Session expired" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_token_exchange_failure_raises_401(self, mock_request, mock_user):
        """Test that failed token exchange raises 401."""
        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            # Simulate token exchange failure
            mock_oauth.exchange_token = AsyncMock(side_effect=Exception("Token exchange failed"))
            mock_oauth_class.return_value = mock_oauth

            with pytest.raises(HTTPException) as exc_info:
                await list_mcp_tools(mock_request, mock_user)

            # The exception handler catches "token" in error message and converts to 401
            assert exc_info.value.status_code == 401
            assert "Token exchange failed" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_mcp_gateway_connect_error_raises_503(self, mock_request, mock_user):
        """Test that connection error to MCP gateway raises 503."""
        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
                mock_client_class.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await list_mcp_tools(mock_request, mock_user)

                assert exc_info.value.status_code == 503
                assert "Cannot connect to Gatana MCP gateway" in exc_info.value.detail
                assert "Gateway may be offline" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_mcp_gateway_timeout_raises_504(self, mock_request, mock_user):
        """Test that timeout to MCP gateway raises 504."""
        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("Request timed out"))
                mock_client_class.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await list_mcp_tools(mock_request, mock_user)

                assert exc_info.value.status_code == 504
                assert "timed out" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_mcp_gateway_http_error_raises_503(self, mock_request, mock_user):
        """Test that HTTP error from MCP gateway raises 503."""
        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                # Mock HTTP 500 error from gateway
                mock_response = MagicMock()
                mock_response.status_code = 500
                mock_response.text = "Internal Server Error"

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(
                    side_effect=httpx.HTTPStatusError("Server error", request=MagicMock(), response=mock_response)
                )
                mock_client_class.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await list_mcp_tools(mock_request, mock_user)

                assert exc_info.value.status_code == 503
                assert "Gatana MCP gateway returned error" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_invalid_mcp_response_format_raises_503(self, mock_request, mock_user):
        """Test that invalid MCP response format raises 503."""
        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.headers = {"content-type": "application/json"}
                # Missing "result" or "tools" field
                mock_response.json.return_value = {"jsonrpc": "2.0", "id": 1, "error": "Invalid"}
                mock_response.raise_for_status = MagicMock()

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await list_mcp_tools(mock_request, mock_user)

                assert exc_info.value.status_code == 503
                assert "Invalid response from MCP gateway" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_invalid_sse_response_raises_503(self, mock_request, mock_user):
        """Test that invalid SSE response raises 503."""
        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.headers = {"content-type": "text/event-stream"}
                # Invalid SSE format - no valid data lines
                mock_response.text = "invalid sse format\n\n"
                mock_response.raise_for_status = MagicMock()

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                with pytest.raises(HTTPException) as exc_info:
                    await list_mcp_tools(mock_request, mock_user)

                assert exc_info.value.status_code == 503
                assert "Invalid SSE response" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_tools_without_names_are_filtered(self, mock_request, mock_user):
        """Test that tools without names are filtered out."""
        response_with_invalid_tools = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "valid_tool", "description": "Valid"},
                    {"description": "No name"},  # Missing name
                    {"name": "", "description": "Empty name"},  # Empty name
                    {"name": "another_valid", "description": "Valid"},
                ]
            },
        }

        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.headers = {"content-type": "application/json"}
                mock_response.json.return_value = response_with_invalid_tools
                mock_response.raise_for_status = MagicMock()

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await list_mcp_tools(mock_request, mock_user)

                # Only 2 valid tools should be returned
                assert len(result.tools) == 2
                assert result.tools[0].name == "valid_tool"
                assert result.tools[1].name == "another_valid"

    @pytest.mark.asyncio
    async def test_empty_tools_list_returns_empty_response(self, mock_request, mock_user):
        """Test that empty tools list returns valid empty response."""
        empty_response = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

        with patch("playground_backend.routers.mcp_router.OidcOAuth2Client") as mock_oauth_class:
            mock_oauth = AsyncMock()
            mock_oauth.exchange_token = AsyncMock(return_value="mcp_token")
            mock_oauth_class.return_value = mock_oauth

            with patch("playground_backend.routers.mcp_router.httpx.AsyncClient") as mock_client_class:
                mock_response = MagicMock()
                mock_response.headers = {"content-type": "application/json"}
                mock_response.json.return_value = empty_response
                mock_response.raise_for_status = MagicMock()

                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                result = await list_mcp_tools(mock_request, mock_user)

                assert len(result.tools) == 0
