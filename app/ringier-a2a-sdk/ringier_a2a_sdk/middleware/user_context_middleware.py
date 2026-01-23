"""
User Context Extraction Middleware.

This module provides middleware for extracting and managing user context
from JWT authentication in request.state.user (populated by JWTValidatorMiddleware).
"""

import logging
from contextvars import ContextVar
from typing import Any, Dict, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


# Context variable for storing user information
# This is thread-safe and async-safe - each request gets its own isolated copy
current_user_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("current_user_context", default=None)


class UserContextFromRequestStateMiddleware:
    """
    Middleware that extracts user context from request.state.user (OIDC flow).

    This middleware is designed for OIDC authentication flows where an upstream
    middleware (like JWTValidatorMiddleware) validates JWT tokens and populates
    request.state.user with verified user information.

    Extracts user information and stores it in the shared current_user_context
    ContextVar for use by AuthRequestContextBuilder.

    Also extracts the X-Playground-SubAgentConfig-Hash header for playground mode testing.

    ARCHITECTURE:
    JWTValidatorMiddleware → SubAgentIdMiddleware → UserContextFromRequestStateMiddleware → A2A RequestHandler
    (validates JWT)         (extracts sub_agent_id) (stores in contextvars)              (builds RequestContext)

    This runs AFTER both JWTValidatorMiddleware and SubAgentIdMiddleware.

    Implemented as pure ASGI middleware (not BaseHTTPMiddleware) for consistency.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Transfer authenticated user information to async context (pure ASGI)."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Skip user context extraction for health/monitoring endpoints
        path = scope.get("path", "")
        if path in ("/.well-known/agent-card.json", "/health", "/readiness", "/liveness", "/api/v1/health"):
            await self.app(scope, receive, send)
            return

        # Extract verified user info from upstream OIDC middleware
        state = scope.get("state", {})
        user_data = state.get("user")

        if user_data:
            user_id = user_data.get("sub")  # 'sub' claim is the user ID

            # Extract playground sub-agent config hash from header if present
            sub_agent_config_hash = None
            headers = dict(scope.get("headers", []))
            playground_header = headers.get(b"x-playground-subagentconfig-hash", b"").decode("utf-8")
            if playground_header:
                sub_agent_config_hash = playground_header
                logger.info(f"Playground mode: sub-agent config hash {sub_agent_config_hash}")

            # Extract sub_agent_id from scope state (set by SubAgentIdMiddleware)
            sub_agent_id = state.get("sub_agent_id")

            # Store in context variable (async-safe, request-isolated)
            user_context = {
                "user_id": user_id,
                "email": user_data.get("email"),
                "name": user_data.get("name"),
                "token": user_data.get("token"),
                "scopes": user_data.get("scopes", []),
                "groups": user_data.get("groups", []),  # Groups from OIDC middleware
                "sub_agent_config_hash": sub_agent_config_hash,
                "sub_agent_id": sub_agent_id,  # For cost tracking attribution
            }
            current_user_context.set(user_context)
            logger.info(
                f"[USER_CONTEXT] Extracted from scope.state: user_id={user_id}, "
                f"sub_agent_id={sub_agent_id}, groups={user_context.get('groups', [])}"
            )
        else:
            current_user_context.set(None)
            logger.debug("No user found in scope.state")

        try:
            await self.app(scope, receive, send)
        finally:
            current_user_context.set(None)
