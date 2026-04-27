"""Agent Runner A2A Server - Entry point for executing scheduled sub-agent jobs.

This service is called by the agent-console scheduler engine to execute
automated sub-agent jobs. It follows the same A2A pattern as agent-creator and
alloy-agent for consistency and zero-trust security.
"""

import logging
import os
from collections.abc import AsyncIterator
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
    AgentSkill,
    OpenIdConnectSecurityScheme,
    SecurityScheme,
)
from dotenv import load_dotenv
from langsmith.middleware import TracingMiddleware
from rcplus_alloy_common.logging import configure_existing_logger, configure_logger
from ringier_a2a_sdk.middleware import (
    JWTValidatorMiddleware,
    SubAgentIdMiddleware,
    UserContextFromRequestStateMiddleware,
)
from ringier_a2a_sdk.server.context_builder import AuthRequestContextBuilder
from ringier_a2a_sdk.server.executor import BaseAgentExecutor

from agent import AgentRunner

# Load environment variables
load_dotenv()

# Configure logging early (before agent initialization)
logger = configure_logger("main")
configure_existing_logger(logging.getLogger("agent"))
configure_existing_logger(logging.getLogger("ringier_a2a_sdk"))

# Initialize agent globally for reload support
agent = AgentRunner()


@asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    """Lifespan context manager for the FastAPI application."""
    await agent.ensure_store_setup()
    logger.info("Application startup complete")

    yield

    await agent.close()
    logger.info("Application shutdown - Agent Runner closed")


def create_app():
    """Create and configure the A2A FastAPI application."""

    # Agent capabilities
    capabilities = AgentCapabilities(streaming=True, push_notifications=True)

    # Agent skill definition
    skill = AgentSkill(
        id="execute-scheduled-job",
        name="Execute Scheduled Job",
        description="Execute an automated sub-agent job on behalf of a user",
        tags=["scheduler", "automation", "job-execution"],
        examples=[
            "Execute a watch job for a user",
            "Run a task job with sub-agent configuration",
        ],
    )

    # Configure OIDC authentication (validates scheduler service JWT)
    oidc_issuer = os.environ["OIDC_ISSUER"]
    oidc_scheme = OpenIdConnectSecurityScheme(
        type="openIdConnect",
        open_id_connect_url=f"{oidc_issuer}/.well-known/openid-configuration",
        description="OIDC authentication — scheduler service token",
    )

    # Support both local dev and production deployment
    agent_base_url = os.getenv("AGENT_BASE_URL", "http://localhost:5005")

    # Agent card
    agent_card = AgentCard(
        name="Agent Runner",
        description=(
            "Internal service for executing scheduled automated sub-agent jobs. "
            "Called by the agent-console scheduler engine to perform watch "
            "condition evaluation, LangGraph agent execution, and push-notification "
            "delivery of results. Not intended for direct end-user interaction."
        ),
        url=agent_base_url,
        version="1.0.0",
        default_input_modes=agent.SUPPORTED_CONTENT_TYPES,
        default_output_modes=agent.SUPPORTED_CONTENT_TYPES,
        capabilities=capabilities,
        skills=[skill],
        security_schemes={"agent-runner": SecurityScheme(root=oidc_scheme)},
        security=[{"agent-runner": ["openid"]}],
        supports_authenticated_extended_card=False,
    )

    # Create push notification infrastructure so the framework delivers the
    # completed A2A task to the registered webhook automatically on completion.
    httpx_client = httpx.AsyncClient()
    push_config_store = InMemoryPushNotificationConfigStore()
    push_sender = BasePushNotificationSender(
        httpx_client=httpx_client,
        config_store=push_config_store,
    )

    # Create request handler
    request_handler = DefaultRequestHandler(
        agent_executor=BaseAgentExecutor(agent=agent),
        task_store=InMemoryTaskStore(),
        push_config_store=push_config_store,
        push_sender=push_sender,
        request_context_builder=AuthRequestContextBuilder(),
    )

    # Create A2A FastAPI application
    server = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)

    # Build app with lifespan
    app = server.build(lifespan=lifespan)

    # UserContextFromRequestStateMiddleware runs AFTER SubAgentId
    app.add_middleware(UserContextFromRequestStateMiddleware)

    # SubAgentIdMiddleware extracts sub_agent_id from request metadata for cost tracking
    app.add_middleware(SubAgentIdMiddleware)

    # JWTValidatorMiddleware validates the caller's JWT.
    # The scheduler (agent-console) exchanges the user token for agent-runner
    # audience, so expected_azp matches the scheduler's client ID.
    app.add_middleware(
        JWTValidatorMiddleware,
        issuer=oidc_issuer,
        expected_azp="agent-console",
    )

    # TracingMiddleware for LangSmith distributed tracing
    app.add_middleware(TracingMiddleware)

    return app


# Create app instance for uvicorn to import
app = create_app()


@click.command()
@click.option("--host", default="0.0.0.0", help="Host to bind the server to", show_default=True)
@click.option("--port", default=5005, help="Port to bind the server to", show_default=True, type=int)
@click.option("--reload", "reload", is_flag=True, default=False, help="Enable auto-reload for development")
def main(host: str, port: int, reload: bool) -> None:
    """Start the Agent Runner A2A Server."""
    log_conf_path = "log_conf.yml"
    with open(log_conf_path) as f:
        log_config = yaml.safe_load(f)

    if reload:
        uvicorn.run("main:app", host=host, port=port, log_config=log_config, access_log=False, reload=True)
    else:
        uvicorn.run(app, host=host, port=port, log_config=log_config, access_log=False)


if __name__ == "__main__":
    main()
