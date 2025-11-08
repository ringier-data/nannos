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
        issuer: Optional[str] = None,
    ):
        super().__init__(app)
        self.client_id = client_id or os.getenv("OKTA_CLIENT_ID")
        self.issuer = issuer or os.getenv("OKTA_ISSUER")
        self.well_known_url = f"{self.issuer}/.well-known/openid-configuration"
        self.metadata: Optional[dict] = None
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_oidc_metadata(self) -> dict:
        """Fetch OIDC metadata from the well-known configuration endpoint."""
        client = await self._get_http_client()
        if self.metadata is not None:
            return self.metadata

        logger.info(f"Fetching OIDC metadata from: {self.well_known_url}")
        response = await client.get(self.well_known_url)
        response.raise_for_status()
        metadata = response.json()
        self.metadata = metadata
        return metadata

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client for userinfo requests.

        Returns:
            An httpx AsyncClient instance
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient()
        return self._http_client

    async def _fetch_userinfo(self, token: str) -> dict:
        """Fetch user information from the OIDC userinfo endpoint.

        Args:
            token: The bearer token to use for authentication

        Returns:
            The user information from the userinfo endpoint

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        client = await self._get_http_client()
        metadata = await self._get_oidc_metadata()
        userinfo_endpoint: Optional[str] = metadata.get("userinfo_endpoint")
        if not userinfo_endpoint:
            raise ValueError("Userinfo endpoint not found in OIDC metadata")
        logger.info(f"Fetching userinfo from endpoint: {userinfo_endpoint}")
        response = await client.get(userinfo_endpoint, headers={"Authorization": f"Bearer {token}"})

        # Raise for HTTP errors (401, 403, etc.)
        response.raise_for_status()

        return response.json()

    async def aclose(self) -> None:
        """Clean up HTTP client resources."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

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
                    "token": token,
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

            # Add user info to request state for use in handlers
            request.state.user = {
                "sub": userinfo.get("sub"),
                "email": userinfo.get("email"),
                "name": userinfo.get("name"),
                "client_id": self.client_id,
                "token": token,
            }

            # Step 3: Create session JWT and set as cookie
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
