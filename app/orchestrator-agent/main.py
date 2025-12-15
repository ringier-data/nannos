import logging
import os
import sys
from contextlib import asynccontextmanager

import click
import uvicorn
import yaml
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import (
    InMemoryTaskStore,
)
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    OpenIdConnectSecurityScheme,
    SecurityScheme,
)
from rcplus_alloy_common.logging import configure_existing_logger, configure_logger
from ringier_a2a_sdk.middleware import (
    OidcUserinfoMiddleware,
    UserContextFromRequestStateMiddleware,
)
from ringier_a2a_sdk.server import AuthRequestContextBuilder

from app.core.agent import OrchestratorDeepAgent
from app.core.budget_guard import init_budget_guard
from app.core.executor import OrchestratorDeepAgentExecutor
from app.models.config import AgentSettings

logger = configure_logger("main")
configure_existing_logger(logging.getLogger("app"))
configure_existing_logger(logging.getLogger("ringier_a2a_sdk"))


class MissingAPIKeyError(Exception):
    """Exception for missing API key."""


def create_lifespan(agent_executor: OrchestratorDeepAgentExecutor):
    """Factory to create lifespan with access to agent_executor."""

    @asynccontextmanager
    async def lifespan(app):
        """Application lifespan manager for startup/shutdown tasks."""
        # Startup: Initialize and start budget guard singleton
        logger.info("Starting application lifespan...")

        budget_guard = init_budget_guard(
            project_name=AgentSettings.get_langsmith_project(),
            token_limit=AgentSettings.get_budget_monthly_token_limit(),
            check_interval_seconds=AgentSettings.get_budget_check_interval(),
            warning_thresholds=AgentSettings.get_budget_warning_thresholds(),
            enabled=AgentSettings.get_budget_enabled(),
        )

        # Start background polling
        await budget_guard.start_polling()

        # Setup document store database schema (creates tables if they don't exist)
        logger.info("Setting up document store database schema...")
        await agent_executor.agent._graph_factory.ensure_store_setup()
        logger.info("Document store ready")

        logger.info("Application startup complete")

        yield  # Application runs here

        # Shutdown: Stop budget guard polling
        logger.info("Shutting down application...")
        await budget_guard.stop_polling()
        logger.info("Application shutdown complete")

    return lifespan


@click.command()
@click.option("--host", "host", default="0.0.0.0")
@click.option("--port", "port", default=10001)
def main(host, port):
    """Starts the Orchestrator Agent server."""
    try:
        if os.getenv("model_source", "azure") == "google":
            if not os.getenv("GOOGLE_API_KEY"):
                raise MissingAPIKeyError("GOOGLE_API_KEY environment variable not set.")

        capabilities = AgentCapabilities(streaming=True, push_notifications=True)
        skill = AgentSkill(
            id="orchestrate_tasks",
            name="Task Orchestration",
            description="Plans and coordinates execution of complex tasks by delegating to specialized sub-agents",
            tags=["orchestration", "planning", "coordination", "task management", "multi-agent"],
            examples=[
                "Help me plan a trip to Paris",
                "Analyze this data and create a report",
                "Coordinate multiple tasks across different services",
            ],
        )

        # Configure OIDC authentication
        oidc_oidc = OpenIdConnectSecurityScheme(
            type="openIdConnect",
            open_id_connect_url=f"{os.getenv('OIDC_ISSUER')}/.well-known/openid-configuration",
        )

        # Support both local dev and production deployment
        # In production, AGENT_BASE_URL should be set to the full URL (e.g., https://domain.com/api/orchestrator)
        agent_base_url = os.getenv("AGENT_BASE_URL", f"http://{host}:{port}")

        agent_card = AgentCard(
            name="Orchestrator Agent",
            description="Intelligent orchestrator that plans and coordinates complex tasks by discovering and delegating to specialized sub-agents.",
            url=agent_base_url,
            version="1.0.0",
            default_input_modes=OrchestratorDeepAgent.SUPPORTED_CONTENT_TYPES,
            default_output_modes=OrchestratorDeepAgent.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[skill],
            security_schemes={"orchestrator": SecurityScheme(root=oidc_oidc)},
            security=[{"orchestrator": ["openid", "profile", "email"]}],
            supports_authenticated_extended_card=False,
        )

        # TODO: do we need a task store?
        # TODO: do we need push notifications?
        # httpx_client = httpx.AsyncClient()
        # push_config_store = InMemoryPushNotificationConfigStore()
        # push_sender = BasePushNotificationSender(httpx_client=httpx_client, config_store=push_config_store)
        agent_executor = OrchestratorDeepAgentExecutor()
        request_handler = DefaultRequestHandler(
            agent_executor=agent_executor,
            task_store=InMemoryTaskStore(),
            # push_config_store=push_config_store,
            # push_sender=push_sender,
            request_context_builder=AuthRequestContextBuilder(),
        )
        server = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)

        # Add authentication middleware stack (EXECUTION ORDER: bottom to top for requests)
        app = server.build(lifespan=create_lifespan(agent_executor))

        # UserContextFromRequestStateMiddleware runs AFTER OIDC (transfers user to A2A context)
        app.add_middleware(UserContextFromRequestStateMiddleware)

        # OidcUserinfoMiddleware runs FIRST (validates JWT, sets request.state.user)
        app.add_middleware(
            OidcUserinfoMiddleware,
            issuer=os.environ["OIDC_ISSUER"],
            jwt_secret_key=os.environ["JWT_SECRET_KEY"],
            client_id=os.environ.get("OIDC_CLIENT_ID", "orchestrator"),
            client_secret=os.environ.get("OIDC_CLIENT_SECRET"),
        )
        log_config = yaml.safe_load("log_conf.yml")
        uvicorn.run(app, host=host, port=port, log_config=log_config, access_log=False, use_colors=False)

    except MissingAPIKeyError as e:
        logger.error(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An error occurred during server startup: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
