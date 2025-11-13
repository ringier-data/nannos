"""
OAuth2 Token Exchange (RFC 8693) for Keycloak/Okta OIDC.

This module implements token exchange to obtain service-specific tokens
for sub-agent communication. Each component has its own OIDC client in Keycloak,
and only accepts tokens minted for that specific client.

Architecture:
1. User authenticates with Keycloak → receives user tokens (access + refresh)
2. Orchestrator validates user access token using its client_secret
3. To call JIRA sub-agent:
   - Orchestrator exchanges user access token for JIRA-specific token
   - Uses RFC 8693 token exchange with JIRA's audience
   - Receives new JWT minted for JIRA's client_id
4. JIRA sub-agent validates token using its own client_secret

This provides:
- Service isolation (each service validates only tokens for itself)
- Token scoping (tokens have limited audience)
- Zero-trust (no service trusts another's tokens)
- Audit trail (each token exchange is logged)
- Refresh token support (can refresh expired tokens automatically)

Reference: https://developer.okta.com/docs/guides/set-up-token-exchange/-/main/
Reference: https://www.keycloak.org/docs/latest/securing_apps/#_token-exchange

"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TokenExchangeError(Exception):
    """Raised when token exchange fails."""

    pass


class ExchangedToken(BaseModel):
    """Result of a successful token exchange."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str
    issued_token_type: str = "urn:ietf:params:oauth:token-type:access_token"

    # Computed fields
    expires_at: datetime = Field(default_factory=datetime.utcnow)

    def __init__(self, **data):
        super().__init__(**data)
        # Calculate expiration time
        self.expires_at = datetime.utcnow() + timedelta(seconds=self.expires_in)

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """Check if token is expired (with buffer for clock skew)."""
        return datetime.utcnow() >= (self.expires_at - timedelta(seconds=buffer_seconds))


class OktaTokenExchanger:
    """
    Handles OAuth2 Token Exchange (RFC 8693) with Keycloak/Okta using Authlib.

    This class manages the token exchange flow for obtaining service-specific
    tokens from a user's authenticated token.

    Configuration via environment variables:
    - OKTA_CLIENT_ID: OAuth2 client ID for this service (orchestrator)
    - OKTA_CLIENT_SECRET: Client secret for this service (orchestrator)
    - OKTA_ISSUER: OIDC issuer URL (e.g., https://login.alloy.ch/realms/a2a)

    Usage:
        exchanger = OktaTokenExchanger(
            client_id="orchestrator_client_id",
            client_secret="orchestrator_secret",
            issuer="https://login.alloy.ch/realms/a2a"
        )

        # Exchange user access token for JIRA-specific token
        jira_token = await exchanger.exchange_token(
            subject_token=user_access_token,
            target_client_id="jira_client_id",
            requested_scopes=["jira:read", "jira:write"],
        )
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        issuer: str,
    ):
        """
        Initialize the token exchanger.

        Args:
            client_id: This service's OAuth2 client ID
            client_secret: This service's client secret
            issuer: OIDC issuer URL (e.g., https://login.alloy.ch/realms/a2a)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.issuer = issuer
        # OAuth2 client will be lazily initialized with discovered token endpoint
        self._oauth_client: Optional[AsyncOAuth2Client] = None
        self._token_endpoint: Optional[str] = None

    async def _get_oauth_client(self) -> AsyncOAuth2Client:
        """Get or create the OAuth2 client with server metadata discovery.

        Lazily fetches the OIDC metadata from .well-known/openid-configuration
        to discover the token endpoint, making the implementation more generalizable
        across different OIDC providers (Keycloak, Okta, Auth0, etc.).

        Returns:
            Configured AsyncOAuth2Client instance

        Raises:
            TokenExchangeError: If metadata discovery fails
        """
        if self._oauth_client is None:
            # Fetch server metadata from .well-known/openid-configuration
            well_known_url = f"{self.issuer}/.well-known/openid-configuration"
            logger.debug(f"Fetching OIDC metadata from: {well_known_url}")

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(well_known_url)
                    response.raise_for_status()
                    metadata = response.json()

                self._token_endpoint = metadata.get("token_endpoint")
                if not self._token_endpoint:
                    raise TokenExchangeError("Token endpoint not found in OIDC metadata")

                logger.info(f"Discovered token endpoint: {self._token_endpoint}")

                # Create OAuth2 client with discovered token endpoint
                self._oauth_client = AsyncOAuth2Client(
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    token_endpoint=self._token_endpoint,
                )
            except httpx.HTTPError as e:
                logger.error(f"Failed to fetch OIDC metadata: {e}")
                raise TokenExchangeError(f"OIDC metadata discovery failed: {e}") from e
            except Exception as e:
                logger.error(f"Unexpected error during metadata discovery: {e}")
                raise TokenExchangeError(f"Failed to initialize OAuth2 client: {e}") from e

        return self._oauth_client

    async def exchange_token(
        self,
        subject_token: str,
        target_client_id: str,
        requested_scopes: Optional[List[str]] = None,
        actor_token: Optional[str] = None,
        force_refresh: bool = False,
    ) -> str:
        """
        Exchange a subject token for a target service-specific token.

        Implements OAuth2 Token Exchange (RFC 8693) to obtain a new access token
        that is valid for the target service. The exchanged token will have:
        - audience (aud) claim set to target_client_id
        - scopes limited to requested_scopes
        - same user context (sub claim) as original token

        Args:
            subject_token: The user's authenticated access token (NOT id_token)
            target_client_id: The target service's OAuth2 client ID
            requested_scopes: Optional list of scopes to request (e.g., ["jira:read"])
            actor_token: Optional actor token for delegation scenarios

        Returns:
            The exchanged access token string (JWT) for the target service

        Raises:
            TokenExchangeError: If token exchange fails
        """
        # Perform token exchange
        logger.info(f"Exchanging token for target client: {target_client_id}")

        # Get OAuth client (will initialize on first call)
        oauth_client = await self._get_oauth_client()

        # Prepare token exchange request (RFC 8693)
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": target_client_id,  # Critical: limits token to target service
        }

        # Add optional parameters
        if requested_scopes:
            data["scope"] = " ".join(requested_scopes)

        if actor_token:
            data["actor_token"] = actor_token
            data["actor_token_type"] = "urn:ietf:params:oauth:token-type:access_token"

        try:
            # Use authlib's fetch_token method for token exchange
            token_response = await oauth_client.fetch_token(**data)

            if "access_token" not in token_response:
                error_msg = token_response.get("error_description", token_response.get("error", "Unknown error"))
                logger.error(f"Token exchange failed: {error_msg}")
                raise TokenExchangeError(f"Token exchange failed for {target_client_id}: {error_msg}")

            # Create ExchangedToken object
            exchanged_token = ExchangedToken(
                access_token=token_response["access_token"],
                token_type=token_response.get("token_type", "Bearer"),
                expires_in=token_response.get("expires_in", 3600),
                scope=token_response.get("scope", ""),
                issued_token_type=token_response.get(
                    "issued_token_type", "urn:ietf:params:oauth:token-type:access_token"
                ),
            )

            logger.info(f"Successfully exchanged token for {target_client_id}")
            return exchanged_token.access_token

        except Exception as e:
            logger.error(f"Error during token exchange: {e}")
            raise TokenExchangeError(f"Token exchange failed: {e}") from e

    async def close(self):
        """Clean up resources."""
        if self._oauth_client is not None:
            await self._oauth_client.aclose()
