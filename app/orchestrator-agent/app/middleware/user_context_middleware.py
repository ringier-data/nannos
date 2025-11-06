"""
User Context Middleware for Zero-Trust Authentication.

This middleware implements the zero-trust pattern by:
1. Extracting verified user information from Okta JWT validation (request.state.user)
2. Storing in async context variable for RequestContextBuilder to access
3. Ensuring user isolation is based on authenticated identifiers, not client-provided values

ARCHITECTURE:
Starlette Request → OktaAuthMiddleware → UserContextMiddleware → A2A RequestHandler → AgentExecutor
                    (validates JWT)   (stores in contextvars) (builds RequestContext) (uses user_id)

The contextvars module provides thread-local storage for async contexts, ensuring isolation
between concurrent requests.
"""

import logging
from contextvars import ContextVar
from typing import Callable, Optional, Dict, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


logger = logging.getLogger(__name__)


# Context variable for storing authenticated user information
# This is thread-safe and async-safe - each request gets its own isolated copy
current_user_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("current_user_context", default=None)


class UserContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware that transfers verified user information from Okta authentication to context variables.

    - Extracts user_id from validated JWT ('sub' claim in request.state.user)
    - Stores in async-safe context variable for RequestContextBuilder to access
    - Ensures all agent operations use the authenticated user_id

    This runs AFTER OktaAuthMiddleware which validates JWT tokens and populates request.state.user.
    Uses contextvars for async-safe, request-isolated storage.
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
        # Extract verified user info from Okta middleware
        if hasattr(request.state, "user"):
            user_data = request.state.user
            user_id = user_data.get("sub")  # 'sub' claim is the user ID

            # Store in context variable (async-safe, request-isolated)
            user_context = {
                "user_id": user_id,
                "email": user_data.get("email"),
                "name": user_data.get("name"),
                "token": user_data.get("token"),
                "scopes": user_data.get("scopes", []),
            }
            current_user_context.set(user_context)
        else:
            current_user_context.set(None)

        try:
            # Continue to next handler
            response = await call_next(request)
            return response
        finally:
            # Clean up context variable after request completes
            current_user_context.set(None)
