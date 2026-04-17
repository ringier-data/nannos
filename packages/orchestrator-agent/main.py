import logging
import os
import sys
from contextlib import asynccontextmanager

# Load .env BEFORE any agent_common imports (MODEL_CONFIG is built at import time)
from dotenv import load_dotenv

load_dotenv()

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

from agent_common.core.model_factory import MODEL_CONFIG, get_available_models_metadata, get_default_model

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

    # Log available LLM providers at startup
    if MODEL_CONFIG:
        logger.info("Available LLM models: %s", ", ".join(MODEL_CONFIG.keys()))
        logger.info("Default model: %s", get_default_model())
    else:
        logger.error(
            "No LLM models available! Set cloud credentials or "
            "OPENAI_COMPATIBLE_BASE_URL to enable at least one model."
        )

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

    # Configure OIDC authentication (optional for local development)
    oidc_issuer = os.getenv("OIDC_ISSUER")
    if oidc_issuer:
        oidc_oidc = OpenIdConnectSecurityScheme(
            type="openIdConnect",
            open_id_connect_url=f"{oidc_issuer}/.well-known/openid-configuration",
        )
        security_schemes = {"orchestrator": SecurityScheme(root=oidc_oidc)}
        security = [{"orchestrator": ["openid", "profile", "email"]}]
    else:
        security_schemes = {}
        security = []
        logger.warning("OIDC_ISSUER not set – running without authentication (local dev mode)")

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
        security_schemes=security_schemes,
        security=security,
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

    if oidc_issuer:
        # UserContextFromRequestStateMiddleware runs AFTER JWT validation (transfers user to A2A context)
        app.add_middleware(UserContextFromRequestStateMiddleware)

        # JWTValidatorMiddleware runs FIRST (validates JWT locally, sets request.state.user)
        app.add_middleware(
            JWTValidatorMiddleware,
            issuer=oidc_issuer,
            # TODO: re-enable expected_aud check once https://github.com/alloy-ch/rcplus-alloy-a2a-slack-client/issues/28 is resolved
            # expected_aud="orchestrator",  # Token audience must be orchestrator
            # NOTE: we do not validate azp here because we might use different clients to call the orchestrator
            additional_public_paths=["/models"],
        )
    else:
        logger.warning("Authentication middleware disabled – all requests will be unauthenticated")

    return app


# Create app instance for uvicorn to import
app = create_app()


@app.get("/models")
async def list_available_models():
    """Return the models available on this orchestrator instance.

    For local OpenAI-compatible providers (LM Studio, Ollama, vLLM) the list of
    loaded models is fetched live from the LLM server so the frontend always sees
    the current selection regardless of what was available at startup.
    """
    metadata = get_available_models_metadata()

    llm_base_url = os.getenv("OPENAI_COMPATIBLE_BASE_URL", "").rstrip("/")
    if llm_base_url:
        v1_base = llm_base_url + "/v1" if not llm_base_url.endswith("/v1") else llm_base_url
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{v1_base}/models")
                resp.raise_for_status()
                data = resp.json()
                live_ids: list[str] = [m["id"] for m in data.get("data", []) if "id" in m]

            if live_ids:
                # Strip any stale local entries that were registered at import time
                metadata = [m for m in metadata if not m.get("value", "").startswith("local")
                            and m.get("provider") != "OpenAI Compatible"]
                is_default_local = not any(m.get("is_default") for m in metadata)
                for i, model_id in enumerate(live_ids):
                    metadata.append({
                        "value": model_id,
                        "label": model_id,
                        "provider": "OpenAI Compatible",
                        "supports_thinking": False,
                        "is_default": is_default_local and i == 0,
                    })
        except Exception as e:
            logger.debug("Could not fetch live models from LLM server: %s", e)

    return metadata


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
