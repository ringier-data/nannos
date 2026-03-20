"""Base OAuth2 client with OIDC metadata discovery.

Provides common functionality for all OAuth2 operations:
- OIDC metadata discovery via .well-known/openid-configuration
- AsyncOAuth2Client lifecycle management
- Proper resource cleanup
"""

import logging
from typing import Optional

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

logger = logging.getLogger(__name__)


class OAuthError(Exception):
    """Base exception for OAuth operations."""

    pass


class BaseOAuth2Client:
    """
    Base class for OAuth2 operations with metadata discovery.

    Handles common patterns across all OAuth2 flows:
    - Lazy metadata discovery from OIDC .well-known endpoint
    - AsyncOAuth2Client creation and caching
    - Proper async resource cleanup

    Subclasses implement specific OAuth2 grant types.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        issuer: str,
    ):
        """
        Initialize OAuth2 client.

        Args:
            client_id: OAuth2 client ID
            client_secret: OAuth2 client secret
            issuer: OIDC issuer URL (e.g., https://login.p.nannos.rcplus.io/realms/nannos)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.issuer = issuer.rstrip("/")
        self._oauth_client: Optional[AsyncOAuth2Client] = None
        self._metadata: Optional[dict] = None

    async def _discover_metadata(self) -> dict:
        """
        Discover OIDC metadata from .well-known endpoint.

        Caches result per instance to avoid repeated network calls.

        Returns:
            OIDC metadata dictionary with endpoints and configuration

        Raises:
            OAuthError: If metadata discovery fails
        """
        if self._metadata is None:
            well_known_url = f"{self.issuer}/.well-known/openid-configuration"
            logger.info(f"Fetching OIDC metadata from: {well_known_url} (issuer={self.issuer})")

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    logger.debug(f"Making GET request to {well_known_url}")
                    response = await client.get(well_known_url)
                    logger.debug(f"Response status: {response.status_code}, headers: {dict(response.headers)}")
                    response.raise_for_status()
                    self._metadata = response.json()
                    logger.info(f"Discovered OIDC metadata for {self.issuer}")
            except httpx.HTTPError as e:
                logger.error(
                    f"HTTP error fetching OIDC metadata: status={getattr(e.response, 'status_code', 'N/A')}, url={well_known_url}, error={e}"
                )
                raise OAuthError(f"Failed to fetch OIDC metadata from {well_known_url}: {e}") from e
            except Exception as e:
                logger.error(
                    f"Unexpected error during metadata discovery for {well_known_url}: {type(e).__name__}: {e}"
                )
                raise OAuthError(f"Unexpected error during metadata discovery: {e}") from e

        # Type assertion: _metadata is guaranteed to be dict after successful discovery
        assert self._metadata is not None
        return self._metadata

    async def _get_oauth_client(self) -> AsyncOAuth2Client:
        """
        Get or create OAuth2 client with discovered endpoints.

        Returns:
            Configured AsyncOAuth2Client instance

        Raises:
            OAuthError: If client creation fails
        """
        if self._oauth_client is None:
            metadata = await self._discover_metadata()

            if "token_endpoint" not in metadata:
                raise OAuthError(f"Token endpoint not found in OIDC metadata for {self.issuer}")

            logger.debug(f"Creating OAuth2 client for {self.client_id}")

            # Determine auth method based on whether client_secret is provided
            # Public clients (like agent-console) don't use client secrets
            auth_method = "client_secret_post" if self.client_secret else "none"

            self._oauth_client = AsyncOAuth2Client(
                client_id=self.client_id,
                client_secret=self.client_secret if self.client_secret else None,
                token_endpoint=metadata["token_endpoint"],
                token_endpoint_auth_method=auth_method,
            )

        return self._oauth_client

    async def close(self):
        """Close OAuth2 client and cleanup resources."""
        if self._oauth_client is not None:
            try:
                await self._oauth_client.aclose()
            except RuntimeError as e:
                # Ignore "Event loop is closed" errors during cleanup
                if "Event loop is closed" not in str(e):
                    raise
            finally:
                self._oauth_client = None
                logger.debug(f"Closed OAuth2 client for {self.client_id}")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
