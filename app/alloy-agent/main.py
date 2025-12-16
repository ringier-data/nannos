"""Nanous Agent A2A Server - Entry point for the FastAPI application."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
import uvicorn
import yaml
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)
from dotenv import load_dotenv
from rcplus_alloy_common.logging import configure_existing_logger, configure_logger
from ringier_a2a_sdk.server.context_builder import AuthRequestContextBuilder
from ringier_a2a_sdk.server.executor import BaseAgentExecutor
from ringier_a2a_sdk.middleware import UserContextFromMetadataMiddleware

from agent import NanousAgent

# Load environment variables
load_dotenv()

# Configure logging early (before agent initialization)
logger = configure_logger("main")
configure_existing_logger(logging.getLogger("agent"))
configure_existing_logger(logging.getLogger("ringier_a2a_sdk"))

# Initialize agent globally for reload support
agent = NanousAgent()


@asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    """Lifespan context manager for the FastAPI application.

    Handles cleanup on shutdown.

    Args:
        app: The FastAPI application instance

    Yields:
        None
    """
    # Startup
    logger.info("Application startup complete")

    yield

    # Shutdown: cleanup the agent
    await agent.close()
    logger.info("Application shutdown - Nanous Agent closed")


def create_app():
    """Create and configure the FastAPI application.

    Returns:
        FastAPI application instance
    """

    # Agent capabilities
    capabilities = AgentCapabilities(streaming=True)

    # Agent skill definition
    skill = AgentSkill(
        id="manage-campaigns",
        name="Manage Campaigns",
        tags=["campaign management", "BYOK", "advertising", "Alloy", "ad operations", "advertising campaigns"],
        description=(
            "Expert campaign manager for BYOK (Bring Your Own KPI) campaigns on the Alloy platform. "
            "Manages complete campaign lifecycle including creating proposals from briefings, "
            "generating presentation slides, creating campaigns, syncing to Cockpit for deployment, "
            "and monitoring performance with KPI visualizations. Handles campaign updates and "
            "re-deployment. Keywords: campaign, BYOK, proposal, Cockpit, KPI, advertising, "
            "deployment, monitoring, briefing, creative, targeting."
        ),
    )

    # Agent base URL
    agent_base_url = os.getenv("AGENT_BASE_URL", "http://localhost:5004")

    # Agent card - No authentication required (VPN-protected)
    agent_card = AgentCard(
        name="Nanous Agent",
        description=(
            "Expert campaign manager for BYOK (Bring Your Own KPI) campaigns on the Alloy platform. "
            "Specializes in the complete campaign lifecycle: proposal creation, slide generation, "
            "campaign deployment to Cockpit, performance monitoring, and campaign updates. "
            "Provides natural language interface for all campaign management operations."
        ),
        url=agent_base_url,
        version="1.0.0",
        default_input_modes=agent.SUPPORTED_CONTENT_TYPES,
        default_output_modes=agent.SUPPORTED_CONTENT_TYPES,
        capabilities=capabilities,
        skills=[skill],
        supports_authenticated_extended_card=False,
        security=[],
        security_schemes={
            "bearerAuth": SecurityScheme(
                root=HTTPAuthSecurityScheme(
                    type="http",
                    scheme="bearer",
                    bearer_format="JWT",
                    description="Orchestrator JWT authentication"
                )
            )
        },
    )

    # Create request handler
    request_handler = DefaultRequestHandler(
        agent_executor=BaseAgentExecutor(agent=agent),
        task_store=InMemoryTaskStore(),
        request_context_builder=AuthRequestContextBuilder(),
    )

    # Create A2A FastAPI application
    server = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)


    # Build app with lifespan
    app = server.build(lifespan=lifespan)

    # No authentication middleware needed (VPN-protected)
    app.add_middleware(UserContextFromMetadataMiddleware)

    return app


# Create app instance for uvicorn to import
app = create_app()


@click.command()
@click.option(
    "--host",
    default="0.0.0.0",
    help="Host to bind the server to",
    show_default=True,
)
@click.option(
    "--port",
    default=5004,
    help="Port to bind the server to",
    show_default=True,
    type=int,
)
@click.option("--reload", "reload", is_flag=True, default=False, help="Enable auto-reload for development")
def main(host: str, port: int, reload: bool):
    """Start the Nanous Agent A2A Server.

    Args:
        host: Host to bind the server to
        port: Port to bind the server to
        reload: Enable auto-reload for development
    """
    # Load log configuration
    log_conf_path = "log_conf.yml"
    with open(log_conf_path) as f:
        log_config = yaml.safe_load(f)

    if reload:
        # Use import string for reload support
        uvicorn.run("main:app", host=host, port=port, log_config=log_config, access_log=False, reload=True)
    else:
        # Use app instance directly for production
        uvicorn.run(app, host=host, port=port, log_config=log_config, access_log=False, reload=False)


if __name__ == "__main__":
    main()
