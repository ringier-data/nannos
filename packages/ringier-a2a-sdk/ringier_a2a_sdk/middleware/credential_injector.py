"""MCP credential injection middleware for user authentication.

This module provides interceptors that inject user credentials into MCP tool calls
via request headers. Different implementations support various authentication strategies:
- PassThroughCredentialInjector: Pass through pre-exchanged credentials (for agents with pre-exchanged tokens)
- TokenExchangeCredentialInjector: Perform OIDC token exchange before injection (for agents needing token exchange)
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from langgraph.types import Command
from mcp.types import CallToolResult

from ..cost_tracking.logger import get_request_credentials

logger = logging.getLogger(__name__)


class BaseCredentialInjector(ABC):
    """Abstract base class for MCP credential injection.

    Provides common error handling and logging for credential injection.
    Subclasses must implement the _get_authorization_header() method.
    """

    def get_credentials(self) -> tuple[str | None, str | None]:
        """Get user credentials from context variables.

        Returns:
            Tuple of (user_sub, access_token) or (None, None) if not set
        """
        user_sub, access_token = get_request_credentials()

        if not user_sub or not access_token:
            logger.error(
                f"Credentials not set in context: user_sub={user_sub}, access_token={'SET' if access_token else 'NOT SET'}"
            )
            return None, None

        return user_sub, access_token

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[CallToolResult | ToolMessage | Command]],
    ) -> CallToolResult | ToolMessage | Command:
        """Inject user credentials into the request headers.

        Args:
            request: The MCP tool call request
            handler: The next handler in the interceptor chain

        Returns:
            The result from the handler
        """
        headers = await self.get_headers(request.headers)
        # Create modified request with updated headers
        # The MCP adapter will use these headers to create a new connection
        modified_request = request.override(headers=headers)

        # Call the handler with modified request
        return await handler(modified_request)

    @abstractmethod
    async def _get_authorization_header(self, access_token: str, user_sub: str | None = None) -> str:
        """Get the authorization header value.

        Args:
            user_sub: The user's subject identifier (sub claim)
            access_token: The user's access token

        Returns:
            The Authorization header value (e.g., "Bearer <token>")
        """
        pass

    async def get_headers(self, headers: dict[str, Any] | None = None) -> dict[str, str]:
        """Get the authorization header value.

        Returns:
            The Authorization header value (e.g., "Bearer <token>")
        """
        # Get credentials from context variables (thread-safe)
        user_sub, access_token = self.get_credentials()

        if not user_sub or not access_token:
            logger.error(
                f"Credentials not set in context: user_sub={user_sub}, access_token={'SET' if access_token else 'NOT SET'}"
            )
            raise ValueError("Credentials not set in context. This should not happen.")

        # Get existing headers or create new dict
        if headers is None:
            headers = {}
        headers["Authorization"] = await self._get_authorization_header(access_token, user_sub)
        return headers


class PassThroughCredentialInjector(BaseCredentialInjector):
    """Injects pre-exchanged credentials without additional token exchange.

    This injector passes through the gatana (MCP gateway) token that was already
    exchanged by the orchestrator. Use this when the access token is already in
    the proper format for the MCP server.

    Example:
        For alloy-agent where orchestrator pre-exchanges user token → gatana token:
        ```python
        injector = PassThroughCredentialInjector()
        ```
    """

    async def _get_authorization_header(self, access_token: str, user_sub: str | None = None) -> str:
        """Pass through the access token directly.

        Args:
            user_sub: The user's subject identifier (sub claim)
            access_token: The pre-exchanged access token

        Returns:
            The Authorization Bearer header
        """
        return f"Bearer {access_token}"


class TokenExchangeCredentialInjector(BaseCredentialInjector):
    """Injects credentials after performing OIDC token exchange.

    This injector exchanges the user's access token for an MCP gateway token
    using OIDC RFC 8693 token exchange before injecting credentials. Use this
    when the access token needs to be exchanged for a different audience/client.

    Token exchange preserves the user's sub claim, allowing the backend to
    look up the user directly using the new token.

    Args:
        oidc_client: An OIDC OAuth2Client instance configured with issuer details
        target_client_id: The target client ID for token exchange (typically "gatana")
        requested_scopes: List of scopes to request in the exchanged token

    Example:
        For agent-creator with OIDC token exchange:
        ```python
        oauth2_client = OidcOAuth2Client(
            client_id="agent-creator",
            client_secret="...",
            issuer="https://keycloak.example.com/realms/alloy",
        )
        injector = TokenExchangeCredentialInjector(
            oidc_client=oauth2_client,
            target_client_id="gatana",
            requested_scopes=["openid", "profile", "offline_access"],
        )
        ```
    """

    def __init__(self, oidc_client, target_client_id: str, requested_scopes: list[str] | None = None):
        """Initialize the token exchange credential injector.

        Args:
            oidc_client: An OIDC OAuth2Client instance
            target_client_id: The target client ID for token exchange
            requested_scopes: List of scopes to request (defaults to openid, profile, offline_access)
        """
        self.oidc_client = oidc_client
        self.target_client_id = target_client_id
        self.requested_scopes = requested_scopes or ["openid", "profile", "offline_access"]

    async def _get_authorization_header(self, access_token: str, user_sub: str | None = None) -> str:
        """Exchange token and return authorization header.

        Args:
            user_sub: The user's subject identifier (sub claim)
            access_token: The user's access token to exchange

        Returns:
            The Authorization Bearer header with the exchanged token
        """
        # Perform OIDC token exchange (RFC 8693)
        exchanged_token = await self.oidc_client.exchange_token(
            subject_token=access_token,
            target_client_id=self.target_client_id,
            requested_scopes=self.requested_scopes,
        )
        return f"Bearer {exchanged_token}"
