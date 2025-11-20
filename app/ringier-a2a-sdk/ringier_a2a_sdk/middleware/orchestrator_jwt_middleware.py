"""
Orchestrator JWT Authentication Middleware.

This middleware validates JWT tokens issued by the orchestrator service
using OAuth2 client credentials flow. It verifies the token signature
using JWKS and validates claims (iss, azp, aud, exp, etc.).
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..auth.jwt_validator import (
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    JWTValidationError,
    JWTValidator,
    MissingClaimError,
)

logger = logging.getLogger(__name__)


class OrchestratorJWTMiddleware(BaseHTTPMiddleware):
    """
    Validates bearer tokens issued by the orchestrator service via OAuth2
    client credentials flow.

    Configuration:
    - issuer: OIDC issuer URL (e.g., https://login.example.com/realms/my-realm)
    - expected_azp: Expected orchestrator client ID
    - expected_aud: Expected agent client ID
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
        expected_azp: str,
        expected_aud: str | None = None,
    ):
        """
        Initialize orchestrator JWT middleware.

        Args:
            app: The ASGI application
            issuer: OIDC issuer URL
            expected_azp: Expected orchestrator client ID (authorized party)
            expected_aud: Expected agent client ID (audience). If None, aud claim is not validated.
        """
        super().__init__(app)
        self.issuer = issuer
        self.expected_azp = expected_azp
        self.expected_aud = expected_aud
        self.validator = JWTValidator(
            issuer=issuer,
            expected_azp=expected_azp,
            expected_aud=expected_aud,
        )

    async def dispatch(self, request: Request, call_next):
        """
        Validate orchestrator JWT token with fail-fast semantics.

        Args:
            request: The incoming request
            call_next: The next middleware/handler

        Returns:
            Response from downstream or 401 error response
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

        # Validate JWT token (fail-fast)
        try:
            payload = await self.validator.validate(token)

            # Store orchestrator identity in request state
            request.state.orchestrator = {
                "client_id": payload.get("azp"),
                "audiences": payload.get("aud"),
                "token": token,
                "subject": payload.get("sub"),
            }

            logger.debug(
                f"Authenticated orchestrator request: "
                f"azp={payload.get('azp')}, aud={payload.get('aud')}"
            )

            # Proceed with the request
            response = await call_next(request)
            return response

        except ExpiredTokenError as e:
            logger.warning(f"Expired token for {request.url.path}: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "token_expired",
                    "message": "JWT token has expired. Please obtain a new token.",
                },
            )

        except InvalidSignatureError as e:
            logger.warning(f"Invalid signature for {request.url.path}: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_signature",
                    "message": "JWT signature validation failed. Token may have been tampered with.",
                },
            )

        except InvalidIssuerError as e:
            logger.warning(f"Invalid issuer for {request.url.path}: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_issuer",
                    "message": f"JWT issuer does not match expected issuer: {self.issuer}",
                },
            )

        except InvalidAudienceError as e:
            logger.warning(f"Invalid audience for {request.url.path}: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_audience",
                    "message": "JWT audience does not match this agent's client ID.",
                },
            )

        except MissingClaimError as e:
            logger.warning(f"Missing required claim for {request.url.path}: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "missing_claim",
                    "message": f"JWT missing required claim: {e}",
                },
            )

        except JWTValidationError as e:
            logger.warning(f"JWT validation failed for {request.url.path}: {e}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_token",
                    "message": f"JWT validation failed: {e}",
                },
            )

        except Exception as e:
            logger.error(f"Unexpected error validating JWT for {request.url.path}: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "message": "An error occurred while validating authentication",
                },
            )
