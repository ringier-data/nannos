"""Middleware to extract sub_agent_id from A2A request metadata."""

import logging

from starlette.types import ASGIApp, Receive, Scope, Send

# Re-export the canonical attribution ContextVar (defined in cost_tracking.attribution —
# the one the gateway httpx hook stamps onto x-litellm-spend-logs-metadata, and that
# GatewayAttributionMiddleware sets per model call). This ASGI middleware sets the SAME
# object, so there is a single source of truth for sub_agent_id and no footgun of
# importing the wrong `current_sub_agent_id`. For remote agents this means the header is
# attributed to the sub-agent for the whole request, directly from the transport layer.
from ..cost_tracking.attribution import current_sub_agent_id  # noqa: F401 (re-exported)

logger = logging.getLogger(__name__)


class SubAgentIdMiddleware:
    """
    Extract sub_agent_id from A2A request metadata and store in request.state.

    The orchestrator passes sub_agent_id in the request metadata for cost tracking.
    This middleware extracts it and makes it available to the agent implementation.

    IMPORTANT: Implemented as pure ASGI middleware (not BaseHTTPMiddleware) to support SSE streaming.
    BaseHTTPMiddleware has issues with long-running SSE connections.

    Usage:
        ```python
        from ringier_a2a_sdk.middleware import SubAgentIdMiddleware

        app = server.build()
        app.add_middleware(SubAgentIdMiddleware)
        ```
    """

    def __init__(self, app: ASGIApp) -> None:
        """Initialize middleware.

        Args:
            app: The ASGI application
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle ASGI request.

        Args:
            scope: ASGI scope
            receive: ASGI receive callable
            send: ASGI send callable
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Only process for A2A endpoints
        path = scope.get("path", "")
        if path not in ["/", "/execute", "/stream"]:
            await self.app(scope, receive, send)
            return

        # Extract sub_agent_id from X-Sub-Agent-Id header (cleaner than body parsing)
        headers = dict(scope.get("headers", []))
        sub_agent_id_header = headers.get(b"x-sub-agent-id")

        if sub_agent_id_header:
            try:
                sub_agent_id = int(sub_agent_id_header.decode("utf-8"))
                # Store in scope state for downstream handlers
                if "state" not in scope:
                    scope["state"] = {}
                scope["state"]["sub_agent_id"] = sub_agent_id
                # Also set in ContextVar for easy access in agent implementations
                current_sub_agent_id.set(sub_agent_id)
                logger.info(f"[SUB_AGENT_ID] Extracted sub_agent_id from header: {sub_agent_id}")
            except (ValueError, UnicodeDecodeError) as e:
                logger.warning(f"[SUB_AGENT_ID] Failed to parse X-Sub-Agent-Id header: {e}")
        else:
            logger.debug("[SUB_AGENT_ID] No X-Sub-Agent-Id header found")

        # No need to replay body - just pass through unchanged
        try:
            await self.app(scope, receive, send)
        finally:
            # Clean up ContextVar after request
            current_sub_agent_id.set(None)
