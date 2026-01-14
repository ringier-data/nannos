"""OAuth2 client implementations using Authlib.

Provides standardized OAuth2 operations for the A2A ecosystem:
- Client credentials flow (service-to-service authentication)
- Token exchange (RFC 8693 for service-specific tokens)
- Token refresh (refresh_token grant)

All operations are available through the OidcOAuth2Client class.
"""

from .base import OAuthError
from .client import (
    ClientCredentialsError,
    OidcOAuth2Client,
    TokenExchangeError,
    TokenRefreshError,
)

__all__ = [
    "OAuthError",
    "OidcOAuth2Client",
    "ClientCredentialsError",
    "TokenExchangeError",
    "TokenRefreshError",
]
