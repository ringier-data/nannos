import logging
import os
import sys

import click
import uvicorn
import yaml
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import (
    InMemoryTaskStore,
)
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    AuthorizationCodeOAuthFlow,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityScheme,
)
from rcplus_alloy_common.logging import configure_existing_logger, configure_logger

from app.core.agent import OrchestratorDeepAgent
from app.core.executor import OrchestratorDeepAgentExecutor
from app.handlers import OrchestratorRequestContextBuilder
from app.middleware import OktaAuthMiddleware, UserContextMiddleware

logger = configure_logger("main")
configure_existing_logger(logging.getLogger("app"))


class MissingAPIKeyError(Exception):
    """Exception for missing API key."""


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

        # Configure Okta OIDC authentication
        okta_oauth2 = OAuth2SecurityScheme(
            type="oauth2",
            flows=OAuthFlows(
                authorization_code=AuthorizationCodeOAuthFlow(
                    authorization_url="https://login.alloy.ch/realms/a2a/protocol/openid-connect/auth",
                    token_url="https://login.alloy.ch/realms/a2a/protocol/openid-connect/token",
                    scopes={
                        "openid": "OpenID Connect authentication",
                        "profile": "Access to user profile information",
                        "email": "Access to user email address",
                    },
                )
            ),
        )

        # Support both local dev and production deployment
        # In production, AGENT_BASE_URL should be set to the full URL (e.g., https://domain.com/api/orchestrator)
        agent_base_url = os.getenv("AGENT_BASE_URL", f"http://{host}:{port}")

        agent_card = AgentCard(
            name="Orchestrator Agent",
            description="Intelligent orchestrator that plans and coordinates complex tasks by discovering and delegating to specialized sub-agents. Requires Okta authentication.",
            url=agent_base_url,
            version="1.0.0",
            default_input_modes=OrchestratorDeepAgent.SUPPORTED_CONTENT_TYPES,
            default_output_modes=OrchestratorDeepAgent.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[skill],
            security_schemes={"okta_oauth2": SecurityScheme(root=okta_oauth2)},
            security=[{"okta_oauth2": ["openid", "profile", "email"]}],
            supports_authenticated_extended_card=False,
        )

        # ZERO-TRUST ARCHITECTURE:
        # 1. OktaAuthMiddleware validates JWT and extracts user info
        # 2. UserContextMiddleware transfers user info to A2A context
        # 3. OrchestratorRequestContextBuilder reads user info from context
        # 4. Agent executor uses verified user_id for all operations

        # TODO: do we need a task store?
        # TODO: do we need push notifications?
        # httpx_client = httpx.AsyncClient()
        # push_config_store = InMemoryPushNotificationConfigStore()
        # push_sender = BasePushNotificationSender(httpx_client=httpx_client, config_store=push_config_store)
        request_handler = DefaultRequestHandler(
            agent_executor=OrchestratorDeepAgentExecutor(),
            task_store=InMemoryTaskStore(),
            # push_config_store=push_config_store,
            # push_sender=push_sender,
            request_context_builder=OrchestratorRequestContextBuilder(),
        )
        server = A2AStarletteApplication(agent_card=agent_card, http_handler=request_handler)

        # Add authentication middleware stack (EXECUTION ORDER: bottom to top for requests)
        app = server.build()

        # UserContextMiddleware runs AFTER Okta (transfers user to A2A context)
        app.add_middleware(UserContextMiddleware)

        # OktaAuthMiddleware runs FIRST (validates JWT, sets request.state.user)
        app.add_middleware(
            OktaAuthMiddleware,
            client_id=os.getenv("OKTA_CLIENT_ID", "orchestrator"),
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
