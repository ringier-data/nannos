"""
User Context Extraction Middleware.

This module provides middleware components for extracting and managing user context
in different authentication scenarios:
- UserContextFromMetadataMiddleware: Extracts from A2A message metadata
- UserContextFromRequestStateMiddleware: Extracts from request.state.user (OIDC flow)
"""

import logging
from contextvars import ContextVar
from typing import Any, Dict, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


# Context variable for storing user information
# This is thread-safe and async-safe - each request gets its own isolated copy
current_user_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("current_user_context", default=None)


class UserContextFromMetadataMiddleware:
    """
    Middleware that extracts user context from A2A message metadata.

    Parses the JSON-RPC request body and extracts user information from:
    params.metadata.user_context = {user_id, email, name}

    Stores in scope["state"]["user"] and async context variable for compatibility
    with existing patterns.

    This middleware should run AFTER SubAgentIdMiddleware but BEFORE A2A request handlers.

    Implemented as pure ASGI middleware (not BaseHTTPMiddleware) to avoid conflicts
    with long-running SSE streams. BaseHTTPMiddleware crashes with RuntimeError when
    HTTP client sends additional messages during streaming.

    NOTE: This middleware does NOT extract sub_agent_id. Use SubAgentIdMiddleware
    for that (separation of concerns).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Extract user context from HTTP headers (pure ASGI implementation).

        Reads user context from HTTP headers: X-User-Id, X-User-Email, X-User-Name.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Skip user context extraction for health/monitoring endpoints
        path = scope.get("path", "")
        if path in ("/.well-known/agent-card.json", "/health", "/readiness", "/liveness", "/api/v1/health"):
            await self.app(scope, receive, send)
            return

        user_context = None

        # Extract user context from HTTP headers
        headers = dict(scope.get("headers", []))

        user_id_header = headers.get(b"x-user-id")
        email_header = headers.get(b"x-user-email")
        name_header = headers.get(b"x-user-name")

        # Extract JWT from Authorization header
        auth_header = headers.get(b"authorization", b"").decode("utf-8")
        user_jwt = auth_header.replace("Bearer ", "") if auth_header else None

        if user_id_header:
            try:
                user_context = {
                    "user_id": user_id_header.decode("utf-8"),
                    "email": email_header.decode("utf-8") if email_header else None,
                    "name": name_header.decode("utf-8") if name_header else None,
                    "token": user_jwt,
                }
                logger.info(f"[USER_CONTEXT] Extracted from headers: user_id={user_context['user_id']}")
            except UnicodeDecodeError as e:
                logger.warning(f"Failed to decode user context headers: {e}")
        else:
            logger.debug(f"[USER_CONTEXT] No X-User-Id header found for path: {path}")

        # Store in scope state
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["user"] = user_context
        current_user_context.set(user_context)

        # No body consumption - pass through unchanged
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_context.set(None)


class UserContextFromRequestStateMiddleware:
    """
    Middleware that extracts user context from request.state.user (OIDC flow).

    This middleware is designed for OIDC authentication flows where an upstream
    middleware (like OidcUserinfoMiddleware) validates JWT tokens and populates
    request.state.user with verified user information.

    Extracts user information and stores it in the shared current_user_context
    ContextVar for use by AuthRequestContextBuilder.

    Also extracts the X-Playground-SubAgentConfig-Hash header for playground mode testing.

    ARCHITECTURE:
    OidcUserinfoMiddleware → SubAgentIdMiddleware → UserContextFromRequestStateMiddleware → A2A RequestHandler
    (validates JWT)         (extracts sub_agent_id) (stores in contextvars)              (builds RequestContext)

    This runs AFTER both OidcUserinfoMiddleware and SubAgentIdMiddleware.

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
                "sub_agent_config_hash": sub_agent_config_hash,
                "sub_agent_id": sub_agent_id,  # For cost tracking attribution
            }
            current_user_context.set(user_context)
            logger.debug(f"Extracted user context from scope.state: user_id={user_id}, sub_agent_id={sub_agent_id}")
        else:
            current_user_context.set(None)
            logger.debug("No user found in scope.state")

        try:
            await self.app(scope, receive, send)
        finally:
            current_user_context.set(None)
