"""Service initialization and management for FastAPI app.state.

This module provides functions to initialize and cleanup services that are stored
in FastAPI's app.state instead of using module-level globals.

Services are stored in app.state during the lifespan startup and can be accessed
via helper functions throughout the application.
"""

import logging
import os
from typing import TYPE_CHECKING

from ringier_a2a_sdk.oauth.client import OidcOAuth2Client

from .config import config
from .db.connection import get_async_session_factory
from .repositories.delivery_channel_repository import DeliveryChannelRepository
from .repositories.rate_card_repository import RateCardRepository
from .repositories.scheduled_job_repository import ScheduledJobRepository
from .repositories.secrets_repository import SecretsRepository
from .repositories.sub_agent_repository import SubAgentRepository
from .repositories.usage_repository import UsageRepository
from .repositories.user_group_repository import UserGroupRepository
from .repositories.user_repository import UserRepository
from .services import SecretsService, SessionService, UserService
from .services.in_memory_socket_session_service import InMemorySocketSessionService
from .services.audit_service import AuditService
from .services.conversation_service import ConversationService
from .services.file_storage_service import FileStorageService
from .services.keycloak_admin_service import KeycloakAdminService
from .services.messages_service import MessagesService
from .services.notification_service import NotificationService
from .services.rate_card_service import RateCardService
from .services.scheduler_engine import SchedulerEngine
from .services.scheduler_service import SchedulerService
from .services.scheduler_token_service import SchedulerTokenService
from .services.sub_agent_service import SubAgentService
from .services.usage_service import UsageService
from .services.user_group_service import UserGroupService
from .services.user_settings_service import UserSettingsService
from .utils.orchestrator_cookie_cache import OrchestratorCookieCache

logger = logging.getLogger(__name__)

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

    # Initialize Keycloak Admin service for group synchronization (optional)
    keycloak_secret = config.keycloak_admin.admin_client_secret.get_secret_value()
    if config.keycloak_admin.admin_client_id and keycloak_secret:
        app.state.keycloak_admin_service = KeycloakAdminService(
            issuer=config.oidc.issuer,
            admin_client_id=config.keycloak_admin.admin_client_id,
            admin_client_secret=keycloak_secret,
            oidc_client_id=config.oidc.client_id,
            group_name_prefix=config.keycloak_admin.group_name_prefix,
        )
        await app.state.keycloak_admin_service.ensure_group_mapper_configured()
        app.state.user_group_service.set_keycloak_service(app.state.keycloak_admin_service)
    else:
        logger.warning("Keycloak Admin credentials not set — group sync disabled")
        app.state.keycloak_admin_service = None

    app.state.rate_card_service = RateCardService()
    app.state.rate_card_service.set_repository(app.state.rate_card_repository)

    app.state.usage_service = UsageService()
    app.state.usage_service.set_repository(app.state.usage_repository)
    app.state.usage_service.set_rate_card_service(app.state.rate_card_service)

    # Socket sessions are always in-memory: they are ephemeral per-connection state
    # tied to a specific pod via Socket.IO sticky sessions. PG persistence adds
    # latency and risks stale-row accumulation with no benefit.
    app.state.socket_session_service = InMemorySocketSessionService()

    # Initialize PostgreSQL-backed or in-memory services depending on configuration
    use_in_memory = bool(os.getenv("USE_IN_MEMORY_STORE"))
    if not use_in_memory:
        app.state.session_service = SessionService()
        app.state.conversation_service = ConversationService()
        app.state.messages_service = MessagesService(conversation_service=app.state.conversation_service)
    else:
        logger.warning(
            "USE_IN_MEMORY_STORE is set — using in-memory stores for sessions, "
            "conversations, and messages. Data will be lost on restart."
        )
        from .services.in_memory_conversation_service import InMemoryConversationService
        from .services.in_memory_messages_service import InMemoryMessagesService
        from .services.in_memory_session_service import InMemorySessionService

        app.state.session_service = InMemorySessionService()
        app.state.conversation_service = InMemoryConversationService()
        app.state.messages_service = InMemoryMessagesService(conversation_service=app.state.conversation_service)

    # Initialize file storage (S3 or local filesystem)
    use_s3 = bool(os.getenv("FILES_S3_BUCKET"))
    if use_s3:
        app.state.file_storage_service = FileStorageService()
    else:
        logger.warning("FILES_S3_BUCKET not set — using local filesystem for file storage")
        from .services.local_file_storage_service import LocalFileStorageService

        app.state.file_storage_service = LocalFileStorageService()

    # Initialize OAuth service
    oidc_config = config.oidc
    app.state.oauth_service = OidcOAuth2Client(
        client_id=oidc_config.client_id,
        client_secret=oidc_config.client_secret.get_secret_value(),
        issuer=oidc_config.issuer,
    )

    # Initialize delivery channel repository
    app.state.delivery_channel_repository = DeliveryChannelRepository()
    app.state.delivery_channel_repository.set_audit_service(app.state.audit_service)

    # Initialize scheduler services
    app.state.scheduled_job_repository = ScheduledJobRepository()
    app.state.scheduled_job_repository.set_audit_service(app.state.audit_service)

    app.state.scheduler_token_service = SchedulerTokenService(
        oidc_issuer=config.oidc.issuer,
        oidc_client_id=config.oidc.client_id,
        oidc_client_secret=config.oidc.client_secret.get_secret_value(),
    )

    app.state.scheduler_service = SchedulerService()
    app.state.scheduler_service.set_repository(app.state.scheduled_job_repository)
    app.state.scheduler_service.set_sub_agent_service(app.state.sub_agent_service)

    app.state.scheduler_engine = SchedulerEngine(
        repo=app.state.scheduled_job_repository,
        delivery_channel_repo=app.state.delivery_channel_repository,
        token_service=app.state.scheduler_token_service,
        agent_runner_url=config.scheduler.agent_runner_url,
        db_session_factory=get_async_session_factory(),
        socket_notification_manager=app.state.socket_notification_manager,
        tick_interval_seconds=config.scheduler.tick_interval_seconds,
        claim_limit=config.scheduler.claim_limit,
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
