"""Session middleware for FastAPI."""

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import config
from ..db import get_async_session_factory
from ..dependencies import get_admin_mode, get_impersonated_user_id
from ..utils.cookie_signer import verify_cookie

logger = logging.getLogger(__name__)


class SessionMiddleware(BaseHTTPMiddleware):
    """Middleware to load session data from cookies.

    Services are accessed from app.state which is populated during lifespan startup.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Process the request and load session data."""
        # Access services from app.state (populated during lifespan startup)
        session_service = request.app.state.session_service
        user_service = request.app.state.user_service

        signed_session_id = request.cookies.get(config.cookie_name)

        # Initialize state
        request.state.session_id = None
        request.state.session = None
        request.state.user = None
        request.state.id_token = None
        request.state.access_token = None
        request.state.access_token_expires_at = None
        request.state.refresh_token = None

        if signed_session_id:
            # Verify the signed cookie (no max_age check - session expiry is handled server-side)
            session_id = verify_cookie(signed_session_id)

            if session_id:
                stored_session = await session_service.get_session(session_id)
                if stored_session:
                    # Get database session for user lookup
                    session_factory = get_async_session_factory()
                    async with session_factory() as db:
                        user = await user_service.get_user(db, stored_session.user_id)
                    if user:
                        request.state.session_id = session_id
                        request.state.session = stored_session
                        request.state.user = user
                        request.state.id_token = stored_session.id_token
                        request.state.access_token = stored_session.access_token
                        request.state.access_token_expires_at = stored_session.access_token_expires_at
                        request.state.refresh_token = stored_session.refresh_token
                        logger.debug(f"Session loaded for user: {user.email}")

                        # Handle impersonation: admin can impersonate another user
                        impersonated_user_id = get_impersonated_user_id(request)
                        if impersonated_user_id:
                            logger.info(f"Impersonation header detected: {impersonated_user_id}")
                            admin_mode = get_admin_mode(request)
                            # Only allow impersonation if user is admin and admin mode is enabled
                            if user.is_administrator and admin_mode:
                                impersonated_user = await user_service.get_user(db, impersonated_user_id)
                                if impersonated_user:
                                    # Store original user for audit logging
                                    request.state.original_user = user
                                    # Override request.state.user with impersonated user
                                    request.state.user = impersonated_user
                                    logger.info(
                                        f"✓ Impersonation active: Admin {user.email} → User {impersonated_user.email}"
                                    )
                                else:
                                    logger.warning(
                                        f"Admin {user.email} attempted to impersonate non-existent user: "
                                        f"{impersonated_user_id}"
                                    )
                            else:
                                logger.warning(
                                    f"User {user.email} (admin={user.is_administrator}, admin_mode={admin_mode}) "
                                    f"attempted to impersonate user {impersonated_user_id} without proper privileges"
                                )
                        else:
                            logger.debug(f"No impersonation header for {user.email}")
                    else:
                        logger.debug(f"User not found for session: {session_id}")
                else:
                    logger.debug(f"Session not found: {session_id}")
            else:
                logger.warning("Session cookie signature verification failed - possible tampering")
        else:
            logger.debug("No session cookie found in request")

        return await call_next(request)
