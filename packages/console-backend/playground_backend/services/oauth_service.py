"""OAuth2 service for token management.

This module re-exports the unified OAuth2 client from ringier_a2a_sdk.
"""

# Import from SDK
from ringier_a2a_sdk.oauth.client import (
    OidcOAuth2Client,
    TokenExchangeError,
    TokenRefreshError,
)

__all__ = [
    "OAuthService",
    "TokenExchangeError",
    "TokenRefreshError",
]


class OAuthService(OidcOAuth2Client):
    """Handles OAuth2 token operations.

    This class is now a direct alias to OidcOAuth2Client from ringier_a2a_sdk,
    which provides all OAuth2 operations (client credentials, token exchange,
    token refresh) through a single unified client.

    Backward compatible with existing a2a-inspector code.
    """

    @property
    def token_endpoint(self) -> str | None:
        """Get token endpoint URL if available."""
        # Access internal metadata for backward compatibility
        # This is a workaround - consider exposing metadata via SDK public API
        metadata = getattr(self, "_metadata", None)
        if metadata:
            return metadata.get("token_endpoint")
        return None
