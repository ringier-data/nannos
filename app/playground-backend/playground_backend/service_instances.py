"""Service initialization and management for FastAPI app.state.

This module provides functions to initialize and cleanup services that are stored
in FastAPI's app.state instead of using module-level globals.

Services are stored in app.state during the lifespan startup and can be accessed
via helper functions throughout the application.
"""

from typing import TYPE_CHECKING

from ringier_a2a_sdk.oauth.client import OidcOAuth2Client

from .config import config
from .services import SecretsService, SessionService, SocketSessionService, UserService
from .services.conversation_service import ConversationService
from .services.messages_service import MessagesService
from .utils.orchestrator_cookie_cache import OrchestratorCookieCache

if TYPE_CHECKING:
    from fastapi import FastAPI


async def initialize_services(app: "FastAPI") -> None:
    """Initialize all services and store them in app.state.

    Called during FastAPI lifespan startup.

    Args:
        app: The FastAPI application instance
    """
    # Initialize auth services
    app.state.session_service = SessionService()
    app.state.socket_session_service = SocketSessionService()
    app.state.user_service = UserService()

    # Initialize conversation and message services
    app.state.conversation_service = ConversationService()
    app.state.messages_service = MessagesService(conversation_service=app.state.conversation_service)

    # Initialize secrets service
    app.state.secrets_service = SecretsService()

    # Initialize OAuth service
    oidc_config = config.oidc
    app.state.oauth_service = OidcOAuth2Client(
        client_id=oidc_config.client_id,
        client_secret=oidc_config.client_secret.get_secret_value(),
        issuer=oidc_config.issuer,
    )

    # Initialize orchestrator cookie cache
    app.state.orchestrator_cookie_cache = OrchestratorCookieCache(
        session_service=app.state.session_service,
        ttl=60,  # 60 second cache TTL
        maxsize=10000,  # 10k max entries
    )


async def cleanup_services(app: "FastAPI") -> None:
    """Clean up service resources from app.state.

    Called during FastAPI lifespan shutdown.

    Args:
        app: The FastAPI application instance
    """
    if hasattr(app.state, "oauth_service") and app.state.oauth_service is not None:
        await app.state.oauth_service.close()


__all__ = [
    "cleanup_services",
    "initialize_services",
]
