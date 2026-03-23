import logging
import os
import sys
from contextlib import asynccontextmanager

import click
import httpx
import uvicorn
import yaml
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
)
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentSkill,
    OpenIdConnectSecurityScheme,
    SecurityScheme,
)
from rcplus_alloy_common.logging import configure_existing_logger, configure_logger
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.cost_tracking.logger import get_request_access_token
from ringier_a2a_sdk.middleware import (
    JWTValidatorMiddleware,
    UserContextFromRequestStateMiddleware,
)
from ringier_a2a_sdk.server import AuthRequestContextBuilder

from app.core.a2a_extensions import (
    ACTIVITY_LOG_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    WORK_PLAN_EXTENSION,
)
from app.core.agent import OrchestratorDeepAgent
from app.core.budget_guard import init_budget_guard
from app.core.executor import OrchestratorDeepAgentExecutor
from app.models.config import AgentSettings

logger = configure_logger("main")
configure_existing_logger(logging.getLogger("app"))
configure_existing_logger(logging.getLogger("ringier_a2a_sdk"))
# configure_existing_logger(logging.getLogger("a2a"), log_level=logging.DEBUG)


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

        # Start cost logger for tracking LLM usage
        if agent_executor.agent._graph_factory.cost_logger is not None:
            await agent_executor.agent._graph_factory.cost_logger.start()
            logger.info(
                f"Cost logger started with backend: {agent_executor.agent._graph_factory.cost_logger.backend_url}"
            )

        # Setup document store database schema (creates tables if they don't exist)
        logger.info("Setting up document store database schema...")
        await agent_executor.agent._graph_factory.ensure_store_setup()
        logger.info("Document store ready")

        logger.info("Application startup complete")

        yield  # Application runs here

        # Shutdown: Stop budget guard and clean up graph factory resources
        logger.info("Shutting down application...")
        await budget_guard.stop_polling()
        logger.info("Budget guard shutdown complete")

        # Close agent (includes cost logger and database connection pool cleanup)
        await agent_executor.agent.close()
        logger.info("Agent resources cleaned up")

        logger.info("Application shutdown complete")

    return lifespan


def create_app():
    """Factory function to create the FastAPI app instance."""
    if os.getenv("model_source", "azure") == "google":
        if not os.getenv("GOOGLE_API_KEY"):
            raise MissingAPIKeyError("GOOGLE_API_KEY environment variable not set.")

    capabilities = AgentCapabilities(
        streaming=True,
        push_notifications=True,
        extensions=[
            AgentExtension(
                uri=ACTIVITY_LOG_EXTENSION,
                description="Emits tool-usage and delegation status events as a timeline via Message.extensions on status updates.",
            ),
            AgentExtension(
                uri=WORK_PLAN_EXTENSION,
                description="Emits structured todo-checklist progress via DataPart in status updates.",
            ),
            AgentExtension(
                uri=INTERMEDIATE_OUTPUT_EXTENSION,
                description="Streams draft content from sub-agents via Artifact.extensions on artifact updates.",
            ),
        ],
    )
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
    # For reload, default to localhost:10001 if not set
    agent_base_url = os.getenv("AGENT_BASE_URL", "http://localhost:10001")

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

    # Initialize cost logger for tracking LLM usage
    backend_url = os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")
    cost_logger = CostLogger(backend_url=backend_url, access_token_provider=get_request_access_token)
    logger.info(f"Cost logger initialized with backend: {backend_url}")

    # TODO: do we need a persistent task store?
    httpx_client = httpx.AsyncClient()
    push_config_store = InMemoryPushNotificationConfigStore()
    push_sender = BasePushNotificationSender(httpx_client=httpx_client, config_store=push_config_store)
    agent_executor = OrchestratorDeepAgentExecutor(cost_logger=cost_logger)
    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor,
        task_store=InMemoryTaskStore(),
        push_config_store=push_config_store,
        push_sender=push_sender,
        request_context_builder=AuthRequestContextBuilder(),
    )
    server = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)

    # Add authentication middleware stack (EXECUTION ORDER: bottom to top for requests)
    app = server.build(lifespan=create_lifespan(agent_executor))

    # UserContextFromRequestStateMiddleware runs AFTER JWT validation (transfers user to A2A context)
    app.add_middleware(UserContextFromRequestStateMiddleware)

    # JWTValidatorMiddleware runs FIRST (validates JWT locally, sets request.state.user)
    app.add_middleware(
        JWTValidatorMiddleware,
        issuer=os.environ["OIDC_ISSUER"],
        # TODO: re-enable expected_aud check once https://github.com/alloy-ch/rcplus-alloy-a2a-slack-client/issues/28 is resolved
        # expected_aud="orchestrator",  # Token audience must be orchestrator
        # NOTE: we do not validate azp here because we might use different clients to call the orchestrator
    )

    return app


# Create app instance for uvicorn to import
app = create_app()


@click.command()
@click.option("--host", "host", default="0.0.0.0")
@click.option("--port", "port", default=10001)
@click.option("--reload", "reload", is_flag=True, default=False, help="Enable auto-reload for development")
def main(host, port, reload):
    """Starts the Orchestrator Agent server."""
    try:
        log_config = yaml.safe_load("log_conf.yml")

        if reload:
            # Use import string for reload support
            uvicorn.run("main:app", host=host, port=port, log_config=log_config, access_log=False, reload=True)
        else:
            # Use app instance directly for production
            uvicorn.run(app, host=host, port=port, log_config=log_config, access_log=False, reload=False)

    except MissingAPIKeyError as e:
        logger.error(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An error occurred during server startup: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
