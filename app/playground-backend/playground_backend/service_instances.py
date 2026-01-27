"""Service initialization and management for FastAPI app.state.

This module provides functions to initialize and cleanup services that are stored
in FastAPI's app.state instead of using module-level globals.

Services are stored in app.state during the lifespan startup and can be accessed
via helper functions throughout the application.
"""

from typing import TYPE_CHECKING

from ringier_a2a_sdk.oauth.client import OidcOAuth2Client

from .config import config
from .repositories.rate_card_repository import RateCardRepository
from .repositories.secrets_repository import SecretsRepository
from .repositories.sub_agent_repository import SubAgentRepository
from .repositories.usage_repository import UsageRepository
from .repositories.user_group_repository import UserGroupRepository
from .repositories.user_repository import UserRepository
from .services import SecretsService, SessionService, SocketSessionService, UserService
from .services.audit_service import AuditService
from .services.conversation_service import ConversationService
from .services.keycloak_admin_service import KeycloakAdminService
from .services.messages_service import MessagesService
from .services.notification_service import NotificationService
from .services.rate_card_service import RateCardService
from .services.sub_agent_service import SubAgentService
from .services.usage_service import UsageService
from .services.user_group_service import UserGroupService
from .services.user_settings_service import UserSettingsService
from .utils.orchestrator_cookie_cache import OrchestratorCookieCache

if TYPE_CHECKING:
    from fastapi import FastAPI


async def initialize_services(app: "FastAPI") -> None:
    """Initialize all services and store them in app.state.

    Called during FastAPI lifespan startup.

    Args:
        app: The FastAPI application instance
    """
    # Initialize audit service first (required by repositories)
    app.state.audit_service = AuditService()

    # Initialize notification service first (required by user group service)
    app.state.notification_service = NotificationService()

    # Initialize repositories and inject audit service
    app.state.user_repository = UserRepository()
    app.state.user_repository.set_audit_service(app.state.audit_service)

    app.state.secrets_repository = SecretsRepository()
    app.state.secrets_repository.set_audit_service(app.state.audit_service)

    app.state.sub_agent_repository = SubAgentRepository()
    app.state.sub_agent_repository.set_audit_service(app.state.audit_service)

    app.state.user_group_repository = UserGroupRepository()
    app.state.user_group_repository.set_audit_service(app.state.audit_service)

    app.state.rate_card_repository = RateCardRepository()
    app.state.rate_card_repository.set_audit_service(app.state.audit_service)

    app.state.usage_repository = UsageRepository()

    # Initialize services with repositories
    app.state.user_settings_service = UserSettingsService()

    app.state.user_service = UserService()
    app.state.user_service.set_repository(app.state.user_repository)
    app.state.user_service.set_audit_service(app.state.audit_service)

    app.state.secrets_service = SecretsService()
    app.state.secrets_service.set_repository(app.state.secrets_repository)
    app.state.secrets_service.set_notification_service(app.state.notification_service)

    app.state.sub_agent_service = SubAgentService()
    app.state.sub_agent_service.set_repository(app.state.sub_agent_repository)
    app.state.sub_agent_service.set_notification_service(app.state.notification_service)

    app.state.user_group_service = UserGroupService()
    app.state.user_group_service.set_repository(app.state.user_group_repository)
    app.state.user_group_service.set_sub_agent_service(app.state.sub_agent_service)
    app.state.user_group_service.set_notification_service(app.state.notification_service)

    # Initialize Keycloak Admin service for group synchronization
    app.state.keycloak_admin_service = KeycloakAdminService(
        issuer=config.oidc.issuer,
        admin_client_id=config.keycloak_admin.admin_client_id,
        admin_client_secret=config.keycloak_admin.admin_client_secret.get_secret_value(),
        oidc_client_id=config.oidc.client_id,
        group_name_prefix=config.keycloak_admin.group_name_prefix,
    )
    # Automatically configure group mapper on startup
    await app.state.keycloak_admin_service.ensure_group_mapper_configured()
    # Inject Keycloak service into user group service
    app.state.user_group_service.set_keycloak_service(app.state.keycloak_admin_service)

    app.state.rate_card_service = RateCardService()
    app.state.rate_card_service.set_repository(app.state.rate_card_repository)

    app.state.usage_service = UsageService()
    app.state.usage_service.set_repository(app.state.usage_repository)
    app.state.usage_service.set_rate_card_service(app.state.rate_card_service)

    # Initialize other services
    app.state.session_service = SessionService()
    app.state.socket_session_service = SocketSessionService()

    # Initialize conversation and message services
    app.state.conversation_service = ConversationService()
    app.state.messages_service = MessagesService(conversation_service=app.state.conversation_service)

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
