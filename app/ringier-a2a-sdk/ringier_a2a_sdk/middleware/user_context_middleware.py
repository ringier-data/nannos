"""
User Context Extraction Middleware.

This module provides middleware components for extracting and managing user context
in different authentication scenarios:
- UserContextFromMetadataMiddleware: Extracts from A2A message metadata
- UserContextFromRequestStateMiddleware: Extracts from request.state.user (OIDC flow)
"""

import json
import logging
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


# Context variable for storing user information
# This is thread-safe and async-safe - each request gets its own isolated copy
current_user_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("current_user_context", default=None)


class UserContextFromMetadataMiddleware(BaseHTTPMiddleware):
    """
    Middleware that extracts user context from A2A message metadata.

    Parses the JSON-RPC request body and extracts user information from:
    params.metadata.user_context = {user_id, email, name}

    Stores in request.state.user and async context variable for compatibility
    with existing patterns.

    This middleware should run AFTER authentication middleware (OrchestratorJWTMiddleware)
    but BEFORE A2A request handlers.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Extract user context from A2A message metadata.

        Args:
            request: The incoming Starlette request
            call_next: The next middleware/handler in the chain

        Returns:
            Response from downstream handlers
        """
        # Initialize with no user context
        user_context = None

        # Try to extract user context from request body
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                # Read and parse request body
                body = await request.body()
                if body:
                    data = json.loads(body)

                    # Extract user_context from A2A message metadata
                    # Structure: {jsonrpc, method, params: {metadata: {user_context: {...}}}}
                    params = data.get("params", {})
                    metadata = params.get("metadata", {})
                    user_context_data = metadata.get("user_context", {})

                    if user_context_data:
                        user_context = {
                            "user_id": user_context_data.get("user_id"),
                            "email": user_context_data.get("email"),
                            "name": user_context_data.get("name"),
                        }
                        logger.debug(f"Extracted user context from metadata: user_id={user_context['user_id']}")
                    else:
                        logger.debug("No user_context found in message metadata")

            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse request body as JSON: {e}")
            except Exception as e:
                logger.warning(f"Unexpected error extracting user context: {e}")

        # Store in request state and context variable
        if user_context:
            request.state.user = user_context
            current_user_context.set(user_context)
        else:
            # No user context found - this is OK for some operations
            request.state.user = None
            current_user_context.set(None)

        try:
            # Continue to next handler
            response = await call_next(request)
            return response
        finally:
            # Clean up context variable after request completes
            current_user_context.set(None)


class UserContextFromRequestStateMiddleware(BaseHTTPMiddleware):
    """
    Middleware that extracts user context from request.state.user (OIDC flow).

    This middleware is designed for OIDC authentication flows where an upstream
    middleware (like OidcUserinfoMiddleware) validates JWT tokens and populates
    request.state.user with verified user information.

    Extracts user information and stores it in the shared current_user_context
    ContextVar for use by AuthRequestContextBuilder.

    Also extracts the X-Playground-SubAgentConfig-Hash header for playground mode testing.

    ARCHITECTURE:
    OidcUserinfoMiddleware → UserContextFromRequestStateMiddleware → A2A RequestHandler → AgentExecutor
    (validates JWT)         (stores in contextvars)                 (builds RequestContext)  (uses user_id)

    This runs AFTER OidcUserinfoMiddleware which validates JWT tokens and populates request.state.user.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Transfer authenticated user information to async context.

        Args:
            request: The incoming Starlette request
            call_next: The next middleware/handler in the chain

        Returns:
            Response from downstream handlers
        """
        # Extract verified user info from upstream OIDC middleware
        if hasattr(request.state, "user") and request.state.user:
            user_data = request.state.user
            user_id = user_data.get("sub")  # 'sub' claim is the user ID

            # Extract playground sub-agent config hash from header if present
            sub_agent_config_hash = None
            playground_header = request.headers.get("X-Playground-SubAgentConfig-Hash")
            if playground_header:
                sub_agent_config_hash = playground_header
                logger.info(f"Playground mode: sub-agent config hash {sub_agent_config_hash}")

            # Store in context variable (async-safe, request-isolated)
            user_context = {
                "user_id": user_id,
                "email": user_data.get("email"),
                "name": user_data.get("name"),
                "token": user_data.get("token"),
                "scopes": user_data.get("scopes", []),
                "sub_agent_config_hash": sub_agent_config_hash,
            }
            current_user_context.set(user_context)
            logger.debug(f"Extracted user context from request.state: user_id={user_id}")
        else:
            current_user_context.set(None)
            logger.debug("No user found in request.state")

        try:
            # Continue to next handler
            response = await call_next(request)
            return response
        finally:
            # Clean up context variable after request completes
            current_user_context.set(None)
