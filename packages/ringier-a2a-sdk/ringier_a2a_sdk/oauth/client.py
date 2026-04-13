"""OAuth2 client supporting multiple grant types.

Implements OAuth2 operations:
- Client credentials flow (service-to-service authentication)
- Token exchange (RFC 8693 for service-specific tokens)
- Token refresh (refresh_token grant)

Uses AsyncOAuth2Client with shared metadata discovery and connection pooling.
"""

import logging
from typing import Dict, List, Optional

from authlib.oauth2.rfc6749 import OAuth2Token

from .base import BaseOAuth2Client, OAuthError

logger = logging.getLogger(__name__)


class ClientCredentialsError(OAuthError):
    """Raised when client credentials flow fails."""

    pass


class TokenExchangeError(OAuthError):
    """Raised when token exchange fails."""

    pass


class TokenRefreshError(OAuthError):
    """Raised when token refresh fails."""

    pass


class OidcOAuth2Client(BaseOAuth2Client):
    """
    OAuth2 client supporting multiple grant types.

    Handles:
    - Client credentials flow with per-audience token caching
    - RFC 8693 token exchange for service-specific tokens
    - Token refresh with rotation support

    Features:
    - OIDC metadata discovery from .well-known endpoints
    - Connection pooling via shared AsyncOAuth2Client
    - Per-audience token caching for client credentials
    - Configurable token expiry leeway

    Usage:
        client = OidcOAuth2Client(
            client_id="my-service",
            client_secret="secret",
            issuer=os.getenv("OIDC_ISSUER")  # e.g., "https://login.p.nannos.rcplus.io/realms/nannos"
        )

        # Client credentials
        token = await client.get_token(audience="target-service")

        # Token exchange
        exchanged = await client.exchange_token(
            subject_token=user_token,
            target_client_id="target-service"
        )

        # Token refresh
        refreshed = await client.refresh_token(refresh_token)

        await client.close()
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        issuer: str,
        token_leeway: int = 600,
    ):
        """
        Initialize OAuth2 client.

        Args:
            client_id: Service's OAuth2 client ID
            client_secret: Service's client secret
            issuer: OIDC issuer URL (e.g., https://login.p.nannos.rcplus.io/realms/nannos)
            token_leeway: Seconds before expiry to consider token expired (default: 600)
        """
        super().__init__(client_id, client_secret, issuer)
        self.token_leeway = token_leeway
        self._token_cache: Dict[str, OAuth2Token] = {}

    # ============================================================================
    # Client Credentials Flow
    # ============================================================================

    async def get_token(self, audience: str) -> str:
        """
        Get access token for target service using client credentials.

        Uses per-audience caching to minimize token requests while respecting expiry.

        Args:
            audience: Target service's client ID (used as JWT audience claim)

        Returns:
            JWT access token string

        Raises:
            ClientCredentialsError: If token request fails
        """
        client = await self._get_oauth_client()

        # Check cache with authlib's expiry checking
        if audience in self._token_cache:
            cached_token = self._token_cache[audience]
            if not cached_token.is_expired(leeway=self.token_leeway):
                logger.debug(f"Using cached token for audience: {audience}")
                return str(cached_token["access_token"])
            else:
                logger.debug(f"Cached token expired for audience: {audience}")

        # Fetch new token
        logger.debug(f"Fetching client credentials token for audience: {audience}")
        try:
            token = await client.fetch_token(audience=audience)
            self._token_cache[audience] = token
            logger.info(f"Successfully obtained client credentials token for {audience}")
            return token["access_token"]
        except Exception as e:
            raise ClientCredentialsError(f"Failed to fetch token for audience {audience}: {e}") from e

    def clear_cache(self, audience: Optional[str] = None):
        """
        Clear cached tokens.

        Args:
            audience: Clear only specific audience token, or all if None
        """
        if audience:
            self._token_cache.pop(audience, None)
            logger.debug(f"Cleared token cache for audience: {audience}")
        else:
            self._token_cache.clear()
            logger.debug("Cleared all token cache")

    # ============================================================================
    # Token Exchange (RFC 8693)
    # ============================================================================

    async def exchange_token(
        self,
        subject_token: str,
        target_client_id: str,
        requested_scopes: Optional[List[str]] = None,
        actor_token: Optional[str] = None,
    ) -> str:
        """
        Exchange subject token for target service-specific token.

        Implements OAuth2 Token Exchange (RFC 8693) to obtain a new access token
        that is valid for the target service. Each exchanged token has:
        - audience (aud) claim set to target service's client_id
        - scopes limited to requested_scopes
        - same user context (sub claim) as original token

        Args:
            subject_token: User's authenticated access token
            target_client_id: Target service's OAuth2 client ID (becomes aud claim)
            requested_scopes: Optional list of scopes to request
            actor_token: Optional actor token for delegation scenarios

        Returns:
            Exchanged access token string (JWT) for target service

        Raises:
            TokenExchangeError: If token exchange fails
        """
        logger.info(f"Exchanging token for target client: {target_client_id}")

        client = await self._get_oauth_client()

        # RFC 8693 token exchange parameters
        params = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": target_client_id,
        }

        if requested_scopes:
            params["scope"] = " ".join(requested_scopes)

        if actor_token:
            params["actor_token"] = actor_token
            params["actor_token_type"] = "urn:ietf:params:oauth:token-type:access_token"

        try:
            token = await client.fetch_token(**params)

            if "access_token" not in token:
                error_msg = token.get("error_description", token.get("error", "Unknown error"))
                raise TokenExchangeError(f"Token exchange failed for {target_client_id}: {error_msg}")

            logger.info(f"Successfully exchanged token for {target_client_id}")
            return token["access_token"]

        except TokenExchangeError:
            raise
        except Exception as e:
            raise TokenExchangeError(f"Token exchange failed for {target_client_id}: {e}") from e

    # ============================================================================
    # Token Refresh
    # ============================================================================

    async def refresh_token(self, refresh_token: str) -> dict[str, str]:
        """
        Refresh access token using refresh token.

        Handles both rotating and non-rotating refresh token scenarios.

        Args:
            refresh_token: Refresh token from previous authentication

        Returns:
            Dictionary containing new tokens:
                - access_token: New access token
                - refresh_token: New or same refresh token (may be rotated)
                - id_token: New ID token (if available)
                - expires_in: Token lifetime in seconds

        Raises:
            TokenRefreshError: If token refresh fails
        """
        logger.info("Refreshing access token using refresh token")

        client = await self._get_oauth_client()

        try:
            token = await client.fetch_token(
                grant_type="refresh_token",
                refresh_token=refresh_token,
            )

            logger.debug("Successfully refreshed access token")

            return {
                "access_token": token["access_token"],
                "refresh_token": token.get("refresh_token", refresh_token),  # May be rotated
                "id_token": token.get("id_token", ""),
                "expires_in": token.get("expires_in", 3600),
            }

        except Exception as e:
            raise TokenRefreshError(f"Token refresh failed: {e}") from e

    # ============================================================================
    # Lifecycle Management
    # ============================================================================

    async def close(self):
        """Close OAuth2 client and clear all cached tokens."""
        await super().close()
        self._token_cache.clear()
