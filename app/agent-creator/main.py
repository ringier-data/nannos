"""Agent Creator A2A Server - Entry point for the FastAPI application."""

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
    OpenIdConnectSecurityScheme,
    SecurityScheme,
)
from dotenv import load_dotenv
from ringier_a2a_sdk.middleware import (
    OidcUserinfoMiddleware,
    UserContextFromRequestStateMiddleware,
)
from ringier_a2a_sdk.server.context_builder import AuthRequestContextBuilder
from ringier_a2a_sdk.server.executor import BaseAgentExecutor

from agent import AgentCreator

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Global agent instance
agent: AgentCreator | None = None


@asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    """Lifespan context manager for the FastAPI application.

    Handles cleanup on shutdown. Agent is initialized in main() before app creation.

    Args:
        app: The FastAPI application instance

    Yields:
        None
    """
    # Startup
    logger.info("Application startup complete")

    yield

    # Shutdown: cleanup the agent
    if agent:
        await agent.close()
        logger.info("Application shutdown - Agent Creator closed")


def create_app():
    """Create and configure the FastAPI application.

    Returns:
        FastAPI application instance
    """
    if agent is None:
        msg = "Agent not initialized. This should not happen in production."
        raise RuntimeError(msg)

    # Agent capabilities
    capabilities = AgentCapabilities(streaming=True, push_notifications=True)

    # Agent skill definition
    skill = AgentSkill(
        id="create-agents",
        name="Create Agents",
        description="Design and create specialized AI agents through conversation",
        tags=["agent-creation", "agent-design", "configuration", "system-prompts"],
        examples=[
            "Create an agent that helps with JIRA ticket management",
            "I need an agent specialized in Python code review",
            "Design an agent for analyzing customer support data",
            "Create a new agent that can help with SQL queries",
            "Show me the existing agents and help me create a new one",
            "I want to update the system prompt for my data-analyst agent",
        ],
    )

    # Configure OIDC authentication for token exchange (preserves user identity)
    oidc_scheme = OpenIdConnectSecurityScheme(
        type="openIdConnect",
        open_id_connect_url="https://login.alloy.ch/realms/a2a/.well-known/openid-configuration",
        description="OIDC authentication with token exchange (RFC 8693) - preserves user identity from orchestrator",
    )

    # Support both local dev and production deployment
    agent_base_url = os.getenv("AGENT_BASE_URL", "http://localhost:8080")

    # Agent card
    agent_card = AgentCard(
        name="Agent Creator",
        description=(
            "Expert AI Agent Creator specializing in designing and creating specialized "
            "AI agents for the Alloy Infrastructure Agents platform. Can list existing agents, "
            "create new agents with custom configurations, update existing agents, and discover "
            "available MCP tools. Helps users design agent architecture, write system prompts, "
            "select appropriate models and tools, and follow agent creation best practices. "
            "Keywords: create agent, new agent, design agent, configure agent, agent setup, "
            "sub-agent, system prompt, agent tools, agent capabilities."
        ),
        url=agent_base_url,
        version="1.0.0",
        default_input_modes=agent.SUPPORTED_CONTENT_TYPES,
        default_output_modes=agent.SUPPORTED_CONTENT_TYPES,
        capabilities=capabilities,
        skills=[skill],
        security_schemes={"agent-creator": SecurityScheme(root=oidc_scheme)},
        security=[{"agent-creator": ["openid"]}],
        supports_authenticated_extended_card=False,
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

    # UserContextFromRequestStateMiddleware runs AFTER OIDC (transfers user to A2A context)
    app.add_middleware(UserContextFromRequestStateMiddleware)

    # OidcUserinfoMiddleware runs FIRST (validates JWT, sets request.state.user)
    app.add_middleware(
        OidcUserinfoMiddleware,
        issuer=os.environ["OIDC_ISSUER"],
        jwt_secret_key=os.environ.get("JWT_SECRET_KEY", ""),
        client_id=os.environ.get("OIDC_CLIENT_ID", "agent-creator"),
        client_secret=os.environ.get("OIDC_CLIENT_SECRET"),
    )
    return app


@click.command()
@click.option(
    "--host",
    default="0.0.0.0",
    help="Host to bind the server to",
    show_default=True,
)
@click.option(
    "--port",
    default=8080,
    help="Port to bind the server to",
    show_default=True,
    type=int,
)
def main(host: str, port: int):
    """Start the Agent Creator A2A Server.

    Args:
        host: Host to bind the server to
        port: Port to bind the server to
    """
    # Initialize agent before creating app
    global agent
    agent = AgentCreator()
    logger.info("Agent Creator initialized")

    # Create the app
    app = create_app()

    # Load log configuration
    log_conf_path = "log_conf.yml"
    with open(log_conf_path) as f:
        log_config = yaml.safe_load(f)

    uvicorn.run(app, host=host, port=port, log_config=log_config, access_log=False)


if __name__ == "__main__":
    main()
