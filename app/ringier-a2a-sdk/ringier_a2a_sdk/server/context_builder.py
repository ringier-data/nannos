"""
Custom Request Context Builder for Zero-Trust Authentication.

This builder extracts user information from authentication middleware
and makes it available to the agent executor following zero-trust principles.

ZERO-TRUST PATTERN:
- Never trust client-provided identifiers (context_id, task_id, etc.)
- Always extract user_id from validated JWT tokens (middleware)
- User isolation is based on authenticated user_id, not client-provided values
"""

import logging

from a2a.server.agent_execution import RequestContext, RequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.types import MessageSendParams, Task

from ..middleware.user_context_middleware import current_user_context

logger = logging.getLogger(__name__)


class AuthRequestContextBuilder(RequestContextBuilder):
    """
    Custom RequestContextBuilder implementing zero-trust authentication pattern.

    Extracts verified user information from JWT validation (middleware sets current_user_context):
    - 'user_id': User ID (from validated JWT) - PRIMARY IDENTIFIER
    - 'email': User email
    - 'name': User display name
    - 'token': Original JWT token
    - 'scopes': OAuth scopes granted

    The user_id is the ONLY trusted identifier for user isolation.
    Context IDs and task IDs from clients are for conversation tracking only.
    """

    async def build(
        self,
        params: MessageSendParams | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
        context: ServerCallContext | None = None,
    ) -> RequestContext:
        """
        Build RequestContext with verified user information from authentication middleware.

        ZERO-TRUST: The authenticated user_id is extracted from async context variable
        (set by UserContextFromMetadataMiddleware after JWT validation) and stored in call_context.state.

        Args:
            params: The A2A message send parameters
            task_id: Optional task ID (client-provided, untrusted)
            context_id: Optional context ID (client-provided, untrusted)
            task: Optional existing task
            context: ServerCallContext (will be populated with user info)

        Returns:
            RequestContext with verified user_id in call_context.state
        """
        # Use provided context or create new one
        call_context = context if context is not None else ServerCallContext()

        # ZERO-TRUST: Extract verified user information from context variable
        # This was set by UserContextFromMetadataMiddleware after JWT validation
        user_context = current_user_context.get()

        if user_context and "user_id" in user_context:
            # Store verified user information in call context
            call_context.state["user_id"] = user_context["user_id"]
            call_context.state["user_email"] = user_context.get("email")
            call_context.state["user_name"] = user_context.get("name")
            call_context.state["user_token"] = user_context.get("token")
            call_context.state["user_scopes"] = user_context.get("scopes", [])

            logger.info(f"[ZERO-TRUST] Building RequestContext for verified user_id: {user_context['user_id']}")
        else:
            # No authenticated user - mark as anonymous
            logger.warning("[ZERO-TRUST] No user context found - authentication may have been bypassed!")
            logger.warning("Ensure JWT authentication and UserContextFromMetadataMiddleware are properly configured")
            call_context.state["user_id"] = "anonymous"  # Fallback (should trigger auth errors)

        # Create and return the request context
        return RequestContext(
            request=params,
            task_id=task_id,
            context_id=context_id,
            task=task,
            call_context=call_context,
        )
