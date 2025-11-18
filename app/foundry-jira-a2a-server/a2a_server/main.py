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

from a2a_server.handlers import AuthRequestContextBuilder
from a2a_server.handlers.executor import BaseAgentExecutor
from a2a_server.middleware import OktaAuthMiddleware, UserContextMiddleware
from agent.agent import FoundryJiraTicketAgent

logger = configure_logger("main")
configure_existing_logger(logging.getLogger("app"))


# Global reference to agent for cleanup
_agent_instance = None


class MissingAPIKeyError(Exception):
    """Exception for missing API key."""


@asynccontextmanager
async def lifespan(app):
    """Manage application lifespan - startup and shutdown."""
    # Startup
    logger.info("Application startup complete")

    yield

    # Shutdown: cleanup the agent
    # The agent instance is stored in the app's dependency chain
    # We'll access it via the global reference set during initialization
    if _agent_instance:
        await _agent_instance.close()
        logger.info("Application shutdown - Foundry Jira Ticket Agent closed")


@click.command()
@click.option("--host", "host", default="0.0.0.0")
@click.option("--port", "port", default=8080)
def main(host, port):
    """Starts the Agent server."""
    try:
        capabilities = AgentCapabilities(streaming=True, push_notifications=True)
        skill = AgentSkill(
            id="create_jira_ticket",
            name="Create Jira Ticket",
            description="Creates a Jira ticket in Foundry based on a message description",
            tags=["jira", "ticket", "foundry"],
            examples=[
                "Create a ticket for bug in login",
                "Open a Jira ticket about performance issue",
                "Make a ticket: user cannot reset password",
            ],
        )
        openid_connect = OpenIdConnectSecurityScheme(
            type="openIdConnect",
            open_id_connect_url="https://login.alloy.ch/realms/a2a/.well-known/openid-configuration",
        )

        # Support both local dev and production deployment
        agent_base_url = os.getenv("AGENT_BASE_URL", f"http://{host}:{port}")
        global _agent_instance
        _agent_instance = FoundryJiraTicketAgent()
        logger.info("Agent initialized")

        agent_card = AgentCard(
            name="Foundry Jira Ticket Agent",
            description="Creates Jira tickets in Palantir Foundry ontology",
            url=agent_base_url,
            version="1.0.0",
            default_input_modes=_agent_instance.SUPPORTED_CONTENT_TYPES,
            default_output_modes=_agent_instance.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[skill],
            # NOTE: we assume that the key is the client_id
            security_schemes={"foundry-jira-ticket-agent": SecurityScheme(root=openid_connect)},
            security=[{"foundry-jira-ticket-agent": ["openid", "profile", "email"]}],
            supports_authenticated_extended_card=False,
        )

        request_handler = DefaultRequestHandler(
            agent_executor=BaseAgentExecutor(agent=_agent_instance),
            task_store=InMemoryTaskStore(),
            request_context_builder=AuthRequestContextBuilder(),
        )
        server = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)

        # Add authentication middleware stack (EXECUTION ORDER: bottom to top for requests)
        # Pass lifespan to build() method to manage agent lifecycle
        app = server.build(lifespan=lifespan)

        # UserContextMiddleware runs AFTER Okta (transfers user to A2A context)
        app.add_middleware(UserContextMiddleware)

        # OktaAuthMiddleware runs FIRST (validates JWT, sets request.state.user)
        app.add_middleware(
            OktaAuthMiddleware,
            client_id=os.getenv("OKTA_CLIENT_ID", "foundry-jira-ticket-agent"),
        )

        # Load log configuration
        log_conf_path = "a2a_server/log_conf.yml"
        with open(log_conf_path) as f:
            log_config = yaml.safe_load(f)

        uvicorn.run(app, host=host, port=port, log_config=log_config, access_log=False)

    except MissingAPIKeyError as e:
        logger.error(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An error occurred during server startup: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
