"""
OIDC Userinfo Authentication Middleware.

Validates user OIDC tokens by calling the userinfo endpoint and optionally
caches user information in session JWTs.
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

from .session_jwt import create_session_jwt, verify_session_jwt

logger = logging.getLogger(__name__)

# Session cookie configuration
SESSION_COOKIE_NAME = "a2a_session"
SESSION_COOKIE_MAX_AGE = int(os.getenv("JWT_SESSION_EXPIRY_MINUTES", "15")) * 60  # Convert to seconds
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"


class OidcUserinfoMiddleware(BaseHTTPMiddleware):
    """
    Middleware to validate bearer tokens via OIDC userinfo endpoint.

    This middleware validates bearer tokens from users by calling the OIDC
    provider's userinfo endpoint. After first validation, it issues a session
    JWT cookie to cache user information and avoid repeated network calls.

    Configuration:
    - issuer: OIDC issuer URL (e.g., https://login.alloy.ch/realms/a2a)
    - client_id: OAuth2 client ID (optional, for logging)
    - client_secret: OAuth2 client secret (optional)
    - jwt_secret_key: Secret key for signing session JWTs (required for caching)
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
        issuer: str,
        jwt_secret_key: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        """
        Initialize OIDC userinfo middleware.

        Args:
            app: The ASGI application
            issuer: OIDC issuer URL
            jwt_secret_key: Secret key for signing session JWTs
            client_id: OAuth2 client ID (optional)
            client_secret: OAuth2 client secret (optional)
        """
        super().__init__(app)
        self.client_id = client_id
        self.client_secret = client_secret
        self.issuer = issuer.rstrip("/")
        self.jwt_secret_key = jwt_secret_key
        self._oauth_client: Optional[AsyncOAuth2Client] = None
        self._metadata: Optional[dict] = None

    async def _get_oauth_client(self, token: str) -> AsyncOAuth2Client:
        """Get or create OAuth2 client with OIDC metadata discovery."""
        if self._oauth_client is not None:
            # Update token in case it has changed
            self._oauth_client.token = OAuth2Token({"access_token": token, "token_type": "Bearer"})
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
        self._oauth_client = AsyncOAuth2Client(
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_endpoint=self._metadata["token_endpoint"],
            token=OAuth2Token({"access_token": token, "token_type": "Bearer"}),
        )

        logger.info(f"OAuth2 client initialized with userinfo endpoint: {self._metadata.get('userinfo_endpoint')}")
        return self._oauth_client

    async def _fetch_userinfo(self, token: str) -> dict:
        """Fetch user information from the OIDC userinfo endpoint."""
        oauth_client = await self._get_oauth_client(token)

        # Get userinfo endpoint from cached metadata
        if not self._metadata or "userinfo_endpoint" not in self._metadata:
            raise ValueError("Userinfo endpoint not found in OIDC metadata")

        userinfo_endpoint = self._metadata["userinfo_endpoint"]
        logger.debug(f"Fetching userinfo from endpoint: {userinfo_endpoint}")

        # Use authlib's client to make authenticated request
        response = await oauth_client.get(userinfo_endpoint)

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
        Validate authentication via session JWT or userinfo endpoint.

        Priority order:
        1. Check for valid session JWT in cookie (fast, no network call)
        2. If no valid session JWT, validate bearer token via userinfo endpoint
        3. On successful validation, issue new session JWT cookie
        """
        # Allow public endpoints without authentication
        if any(request.url.path.startswith(path) for path in self.PUBLIC_PATHS):
            return await call_next(request)

        # Extract Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
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

        # Step 1: Check for session JWT cookie (local verification, no network call)
        session_jwt = request.cookies.get(SESSION_COOKIE_NAME)
        logger.debug(f"Found session JWT cookie: {session_jwt is not None}")

        if session_jwt:
            jwt_payload = verify_session_jwt(session_jwt, self.jwt_secret_key)
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

        # Step 2: No valid session JWT - validate with OIDC userinfo (requires network call)
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
            session_jwt = create_session_jwt(userinfo, self.jwt_secret_key)

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

            logger.debug(f"Authenticated via userinfo endpoint: {userinfo.get('sub')}")
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
                    status_code=403,
                    content={"error": "forbidden", "message": "Access forbidden with provided token."},
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
