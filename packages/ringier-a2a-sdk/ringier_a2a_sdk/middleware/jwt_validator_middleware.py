"""
JWT Validator Middleware for A2A Authentication.

Validates JWT bearer tokens locally using JWKS without calling userinfo endpoint.
Provides faster authentication and eliminates dependency on OIDC provider for each request.
"""

import logging
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ringier_a2a_sdk.auth.jwt_validator import JWTValidator

logger = logging.getLogger(__name__)


class JWTValidatorMiddleware(BaseHTTPMiddleware):
    """
    Middleware to validate JWT bearer tokens locally using JWKS.

    This middleware validates JWT tokens by checking their signature against
    the issuer's JWKS endpoint. It validates signature, expiry, issuer, and
    optionally audience claims. No network call to userinfo endpoint needed.

    Configuration:
    - issuer: OIDC issuer URL (e.g., https://login.p.nannos.rcplus.io/realms/nannos)
    - expected_azp: Expected authorized party (optional)
    - expected_aud: Expected audience claim (optional)
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
        expected_azp: Optional[str] = None,
        expected_aud: Optional[str] = None,
        additional_public_paths: Optional[list[str]] = None,
    ):
        """
        Initialize JWT validator middleware.

        Args:
            app: The ASGI application
            issuer: OIDC issuer URL (e.g., https://login.p.nannos.rcplus.io/realms/nannos)
            expected_azp: Expected authorized party claim (optional)
            expected_aud: Expected audience claim (optional, not validated if None)
            additional_public_paths: Extra paths to skip authentication for (optional)
        """
        super().__init__(app)
        self.validator = JWTValidator(
            issuer=issuer,
            expected_azp=expected_azp,
            expected_aud=expected_aud,
        )
        self._public_paths = self.PUBLIC_PATHS + (additional_public_paths or [])
        logger.info(
            f"JWT validator middleware initialized with issuer={issuer}, "
            f"expected_azp={expected_azp}, expected_aud={expected_aud}"
        )

    async def dispatch(self, request: Request, call_next):
        """
        Validate JWT bearer token and extract user claims.

        Process:
        1. Skip authentication for public endpoints
        2. Extract Authorization header
        3. Validate JWT locally (signature, expiry, issuer, optionally audience)
        4. Set request.state.user with extracted claims
        5. Proceed with request
        """
        # Allow public endpoints without authentication
        if any(request.url.path.startswith(path) for path in self._public_paths):
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

        # Validate JWT token locally
        try:
            payload = await self.validator.validate(token)

            # Extract user information from JWT claims
            request.state.user = {
                "sub": payload.get("sub"),
                "email": payload.get("email"),
                "name": payload.get("name"),
                "phone_number": payload.get("phone_number"),
                "token": token,
                "groups": payload.get("groups", []),
            }

            logger.debug(
                f"Authenticated user: {request.state.user.get('sub')} "
                f"({request.state.user.get('email')}), "
                f"groups: {request.state.user.get('groups')}"
            )

            # Proceed with the request
            return await call_next(request)

        except Exception as e:
            # JWT validation failed
            error_type = type(e).__name__
            logger.warning(f"JWT validation failed for {request.url.path}: {error_type}: {e}")

            # Map specific errors to appropriate HTTP status codes
            if "expired" in str(e).lower():
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "token_expired",
                        "message": "JWT token has expired. Please re-authenticate.",
                    },
                )
            elif "invalid signature" in str(e).lower() or "InvalidSignatureError" in error_type:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "invalid_signature",
                        "message": "JWT token signature is invalid.",
                    },
                )
            elif "audience" in str(e).lower():
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "invalid_audience",
                        "message": "JWT token audience claim does not match expected value.",
                    },
                )
            elif "issuer" in str(e).lower():
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "invalid_issuer",
                        "message": "JWT token issuer is not trusted.",
                    },
                )
            else:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "invalid_token",
                        "message": "JWT token validation failed.",
                    },
                )
