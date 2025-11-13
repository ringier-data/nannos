"""
Okta OIDC Authentication Middleware for A2A Protocol.

This middleware validates bearer tokens from a trusted OIDC application by fetching
user information from the OIDC provider. The tokens are passed directly from the
OIDC application (e.g., web-client) without JWT validation - instead, we use the
token to fetch user information from the userinfo endpoint.

To optimize performance, after the first successful Okta validation, the middleware
issues a signed session JWT that is stored in an HttpOnly cookie. Subsequent requests
are validated locally by verifying this JWT, eliminating the need for repeated calls
to Okta's userinfo endpoint.

Authentication information is stored in request.state.user and can be accessed
by custom request handlers or middleware downstream.
"""

import logging
import os
from typing import Optional

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .jwt_utils import create_session_jwt, verify_session_jwt

logger = logging.getLogger(__name__)

# Session cookie configuration
SESSION_COOKIE_NAME = "orchestrator_session"
SESSION_COOKIE_MAX_AGE = int(os.getenv("JWT_SESSION_EXPIRY_MINUTES", "15")) * 60  # Convert to seconds
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"


class OktaAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to validate bearer tokens from trusted OIDC application.

    This middleware receives bearer tokens from a trusted OIDC application and
    validates them by fetching user information from the OIDC provider's userinfo
    endpoint. This approach trusts tokens from the configured OIDC application.

    Configuration via environment variables:
    - OKTA_ISSUER:
    - OKTA_CLIENT_ID: OAuth2 client ID for this orchestrator agent (optional, for logging)
    """

    # Public endpoints that don't require authentication
    PUBLIC_PATHS = [
        "/.well-known/agent-card.json",
        "/health",
        "/docs",
        "/openapi.json",
    ]

    def __init__(
        self,
        app,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        issuer: Optional[str] = None,
    ):
        super().__init__(app)
        self.client_id = client_id or os.getenv("OKTA_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("OKTA_CLIENT_SECRET")
        self.issuer = issuer or os.getenv("OKTA_ISSUER")
        self._oauth_client: Optional[AsyncOAuth2Client] = None
        self._metadata: Optional[dict] = None

    async def _get_oauth_client(self, token: str) -> AsyncOAuth2Client:
        """Get or create OAuth2 client with OIDC metadata discovery.

        Uses authlib's AsyncOAuth2Client for standards-compliant OIDC integration.
        Lazily fetches OIDC metadata from .well-known/openid-configuration endpoint.

        Returns:
            Configured AsyncOAuth2Client instance
        """
        if self._oauth_client is not None:
            return self._oauth_client

        # Fetch OIDC metadata for dynamic endpoint discovery
        well_known_url = f"{self.issuer}/.well-known/openid-configuration"
        logger.info(f"Fetching OIDC metadata from: {well_known_url}")

        async with httpx.AsyncClient() as client:
            response = await client.get(well_known_url)
            response.raise_for_status()
            self._metadata = response.json()
        if not self._metadata:
            raise ValueError("Failed to fetch OIDC metadata")

        # Create OAuth2 client with discovered metadata
        # Note: We wouldn't necessarily need the client_secret to verify the bearer token, but since we need also
        # to refresh tokens and eventually perform token exchange, it's good to have it configured.
        self._oauth_client = AsyncOAuth2Client(
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_endpoint=self._metadata["token_endpoint"],
            # TODO: shall we get the expiration info from the token itself?
            token=OAuth2Token({"access_token": token, "token_type": "Bearer"}),
        )

        logger.info(f"OAuth2 client initialized with userinfo endpoint: {self._metadata.get('userinfo_endpoint')}")
        return self._oauth_client

    async def _fetch_userinfo(self, token: str) -> dict:
        """Fetch user information from the OIDC userinfo endpoint using authlib.

        Args:
            token: The bearer token to use for authentication

        Returns:
            The user information from the userinfo endpoint

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        oauth_client = await self._get_oauth_client(token)

        # Get userinfo endpoint from cached metadata
        if not self._metadata or "userinfo_endpoint" not in self._metadata:
            raise ValueError("Userinfo endpoint not found in OIDC metadata")

        userinfo_endpoint = self._metadata["userinfo_endpoint"]
        logger.debug(f"Fetching userinfo from endpoint: {userinfo_endpoint}")

        # Use authlib's client to make authenticated request
        # We need to manually set the Authorization header since we're just using
        # the client for convenience, not full OAuth2 flow
        response = await oauth_client.get(
            userinfo_endpoint,
        )

        # Raise for HTTP errors (401, 403, etc.)
        response.raise_for_status()

        return response.json()

    async def aclose(self) -> None:
        """Clean up OAuth2 client resources."""
        if self._oauth_client is not None:
            await self._oauth_client.aclose()
            self._oauth_client = None

    async def dispatch(self, request: Request, call_next):
        """
        Intercept requests and validate authentication.

        Priority order:
        1. Check for valid session JWT in cookie (fast, no network call)
        2. If no valid session JWT, validate bearer token via Okta userinfo endpoint
        3. On successful Okta validation, issue new session JWT cookie
        """
        # Allow public endpoints without authentication
        if any(request.url.path.startswith(path) for path in self.PUBLIC_PATHS):
            return await call_next(request)

        # Extract Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            # NOTE: each request will come with an Authorization header from the OIDC app even if we might not use it
            #       since we first try to validate the session JWT cookie. But the authorization header must be present.
            #       since it will be needed for the MCP Gateway or to exchange the token for sub-agent requests.
            logger.warning(f"Missing Authorization header for {request.url.path}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Missing Authorization header. Please provide a valid bearer token.",
                },
            )

        # Extract token from "Bearer <token>"
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning(f"Invalid Authorization header format for {request.url.path}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_token_format",
                    "message": "Authorization header must be in format: Bearer <token>",
                },
            )

        token = parts[1]
        # NOTE: In the case of Keycloak this token is a JWT issued by the OIDC application (e.g., web-client).
        #       So we could validate it locally instead of validating it through the userinfo endpoint + issuing
        #       a session JWT. However, to keep the architecture consistent across OIDC providers,
        #       we always validate via userinfo endpoint first

        # Step 1: Check for session JWT cookie (local verification, no network call)
        session_jwt = request.cookies.get(SESSION_COOKIE_NAME)
        logger.debug(f"Cookies: {request.cookies}")
        logger.debug(f"Found session JWT cookie: {session_jwt is not None}")
        if session_jwt:
            jwt_payload = verify_session_jwt(session_jwt)
            if jwt_payload:
                # Valid session JWT - use cached user info
                request.state.user = {
                    "sub": jwt_payload.get("sub"),
                    "email": jwt_payload.get("email"),
                    "name": jwt_payload.get("name"),
                    "client_id": self.client_id,
                    "token": token,  # Always from current Authorization header
                }

                logger.debug(
                    f"Authenticated via session JWT: {request.state.user.get('sub')} "
                    f"({request.state.user.get('email')})"
                )

                # Proceed with the request (no network call needed!)
                response = await call_next(request)
                return response

        # Step 2: No valid session JWT - validate with Okta (requires network call)
        # Fetch user information using the bearer token (network call to Okta)
        try:
            userinfo = await self._fetch_userinfo(token)

            request.state.user = {
                "sub": userinfo.get("sub"),
                "email": userinfo.get("email"),
                "name": userinfo.get("name"),
                "client_id": self.client_id,
                "token": token,
            }

            # Step 3: Create session JWT and set as cookie
            # Session JWT caches stable userinfo only (no tokens)
            # Tokens are always extracted from request headers for freshness
            session_jwt = create_session_jwt(userinfo)

            # Proceed with the request
            response = await call_next(request)

            # Set session JWT cookie on response
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=session_jwt,
                max_age=SESSION_COOKIE_MAX_AGE,
                httponly=True,  # Prevents JavaScript access (XSS protection)
                secure=SESSION_COOKIE_SECURE,  # HTTPS only in production
                samesite="lax",  # CSRF protection
                path="/",  # Available for all paths
            )

            return response

        except httpx.HTTPStatusError as e:
            # Handle HTTP errors from the userinfo endpoint
            if e.response.status_code == 401:
                logger.warning(f"Unauthorized token for {request.url.path}: {e}")
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "invalid_token",
                        "message": "Bearer token is invalid or expired. Please re-authenticate.",
                    },
                )
            elif e.response.status_code == 403:
                logger.warning(f"Forbidden token for {request.url.path}: {e}")
                return JSONResponse(
                    status_code=403, content={"error": "forbidden", "message": "Access forbidden with provided token."}
                )
            else:
                logger.error(f"HTTP error fetching userinfo for {request.url.path}: {e}", exc_info=True)
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": "authentication_service_error",
                        "message": "Failed to validate token with authentication service.",
                    },
                )
        except httpx.RequestError as e:
            logger.error(f"Network error fetching userinfo: {e}", exc_info=True)
            return JSONResponse(
                status_code=503,
                content={
                    "error": "authentication_service_unavailable",
                    "message": "Authentication service is currently unavailable.",
                },
            )
        except Exception as e:
            logger.error(f"Unexpected error validating token: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": "internal_error", "message": "An error occurred while validating authentication"},
            )
