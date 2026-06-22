import logging
import os
import sys
from contextlib import asynccontextmanager

import jwt

# Load .env BEFORE any agent_common imports (LLM_GATEWAY_URL etc. read at import time)
from dotenv import load_dotenv

load_dotenv()

import asyncio

import click
import httpx
import uvicorn
import yaml
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_jsonrpc_routes,
)
from a2a.server.tasks import (
    BasePushNotificationSender,
    DatabaseTaskStore,
    InMemoryPushNotificationConfigStore,
    TaskStore,
)
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    OpenIdConnectSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from agent_common.core.model_factory import get_available_models, get_available_models_metadata, get_default_model
from agent_common.core.sandbox_pool import SandboxPool
from agent_common.core.tool_risk_cache import ToolRiskCache
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from gatana_client import GatanaClient
from gatana_langchain import GatanaSandbox
from google.protobuf.json_format import MessageToDict
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
    HUMAN_IN_THE_LOOP_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    WORK_PLAN_EXTENSION,
)
from app.core.agent import OrchestratorDeepAgent
from app.core.budget_guard import init_budget_guard
from app.core.discovery_cache import (
    invalidate_all as invalidate_discovery_caches,
)
from app.core.discovery_cache import (
    invalidate_users as invalidate_discovery_for_users,
)
from app.core.executor import OrchestratorDeepAgentExecutor
from app.core.risk_score_api_client import HttpRiskScoreAPIClient
from app.core.task_store import create_task_store
from app.models.config import AgentSettings

logger = configure_logger("main")
configure_existing_logger(logging.getLogger("app"))
configure_existing_logger(logging.getLogger("ringier_a2a_sdk"))
configure_existing_logger(logging.getLogger("agent_common"))
# configure_existing_logger(logging.getLogger("a2a"), log_level=logging.DEBUG)


class MissingAPIKeyError(Exception):
    """Exception for missing API key."""


def create_lifespan(
    agent_executor: OrchestratorDeepAgentExecutor,
    task_store: TaskStore,
    task_store_engine=None,
):
    """Factory to create lifespan with access to agent_executor and the task store."""

    @asynccontextmanager
    async def lifespan(app):
        """Application lifespan manager for startup/shutdown tasks."""
        # Startup: Initialize and start budget guard singleton
        logger.info("Starting application lifespan...")

        # Fail fast if the Model Gateway isn't configured: it's the sole path
        # for LLM traffic, so surface a missing LLM_GATEWAY_URL loudly at boot rather than
        # as an opaque per-request failure (or a misleading "no models registered").
        from agent_common.core.model_factory import assert_gateway_configured

        assert_gateway_configured()

        budget_guard = init_budget_guard(
            base_url=os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001"),
            oauth2_client=agent_executor.agent.oauth2_client,
            audience=os.getenv("CONSOLE_BACKEND_CLIENT_ID", "agent-console"),
            check_interval_seconds=AgentSettings.get_budget_check_interval(),
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

        # Initialize the task store at startup (creates the tasks table if missing)
        # so database problems surface here rather than on the first request.
        if isinstance(task_store, DatabaseTaskStore):
            await task_store.initialize()
            logger.info("PostgreSQL task store ready")

        # Initialize sandbox pool if provider is configured
        sandbox_pool = None
        sandbox_provider_name = os.environ.get("SANDBOX_PROVIDER")
        if sandbox_provider_name:
            try:
                warm_ttl = float(os.environ.get("SANDBOX_WARM_TTL", "300"))

                if sandbox_provider_name == "gatana":
                    if not os.environ.get("GATANA_API_KEY") or not os.environ.get("GATANA_ORG_ID"):
                        raise ValueError(
                            "GATANA_API_KEY and GATANA_ORG_ID environment variables must be set for Gatana sandbox provider"
                        )
                    org_capacity = int(os.environ.get("GATANA_ORG_CAPACITY") or "10")

                    async def _create_sandbox():
                        client = GatanaClient()
                        return await asyncio.to_thread(GatanaSandbox, client=client)

                    capacity = int(os.environ.get("SANDBOX_POOL_CAPACITY") or "0") or max(1, org_capacity - 2)
                else:
                    raise ValueError(f"Unknown sandbox provider: {sandbox_provider_name!r}. Available: gatana")

                sandbox_pool = SandboxPool(
                    create_fn=_create_sandbox,
                    capacity=capacity,
                    warm_ttl=warm_ttl,
                    home="/home/ubuntu",
                )
                await sandbox_pool.start_reaper()
                app.state.sandbox_pool = sandbox_pool
                agent_executor.agent.sandbox_pool = sandbox_pool
                logger.info(
                    "Sandbox pool initialized (provider=%s, capacity=%d)",
                    sandbox_provider_name,
                    sandbox_pool.capacity,
                )
            except Exception as e:
                logger.error("Failed to initialize sandbox pool: %s", e)
                sandbox_pool = None
        else:
            app.state.sandbox_pool = None

        # Initialize tool risk cache for dynamic HITL scoring
        tool_risk_cache: ToolRiskCache | None = None
        try:
            backend_url_for_cache = os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001")
            risk_api_client = HttpRiskScoreAPIClient(
                base_url=backend_url_for_cache,
                oauth2_client=agent_executor.agent.oauth2_client,
                audience=os.getenv("CONSOLE_BACKEND_CLIENT_ID", "agent-console"),
            )

            tool_risk_cache = ToolRiskCache()
            # Start cache with API client for DB persistence and periodic refresh.
            # Initial load may return empty if no token is available yet (first request
            # will populate via LLM scoring + write-through).
            await tool_risk_cache.start(risk_api_client)

            app.state.tool_risk_cache = tool_risk_cache
            app.state.risk_api_client = risk_api_client
            agent_executor.agent.tool_risk_cache = tool_risk_cache
            logger.info("Tool risk cache initialized with API client")
        except Exception as e:
            logger.warning("Failed to initialize tool risk cache: %s", e)
            app.state.tool_risk_cache = None

        logger.info("Application startup complete")

        yield  # Application runs here

        # Shutdown: Stop budget guard and clean up graph factory resources
        logger.info("Shutting down application...")
        await budget_guard.stop_polling()
        logger.info("Budget guard shutdown complete")

        # Shutdown sandbox pool
        if sandbox_pool:
            await sandbox_pool.shutdown()

        # Shutdown tool risk cache and API client
        if tool_risk_cache:
            await tool_risk_cache.stop()
        if hasattr(app.state, "risk_api_client") and app.state.risk_api_client:
            await app.state.risk_api_client.close()

        # Close agent (includes cost logger and database connection pool cleanup)
        await agent_executor.agent.close()
        logger.info("Agent resources cleaned up")

        # Dispose task store engine (no-op for the in-memory fallback)
        if task_store_engine is not None:
            await task_store_engine.dispose()

        logger.info("Application shutdown complete")

    return lifespan


def create_app():
    """Factory function to create the FastAPI app instance."""

    # Models live on the Model Gateway; log the default + the live list
    # (best-effort — the gateway is the source of truth).
    logger.info("Default model: %s; gateway: %s", get_default_model(), os.getenv("LLM_GATEWAY_URL", "<unset>"))
    try:
        available = get_available_models()
        if available:
            logger.info("Models registered on the gateway: %s", ", ".join(available))
    except Exception as e:
        logger.debug("Could not list gateway models at startup: %s", e)

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
            AgentExtension(
                uri=HUMAN_IN_THE_LOOP_EXTENSION,
                description="Emits structured interrupt requests requiring human approval before tool execution. "
                "Response: send a DataPart with {decisions: [{type, ...}]}.",
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
            open_id_connect_url=f"{oidc_issuer}/.well-known/openid-configuration",
        )
        security_schemes = {"orchestrator": SecurityScheme(open_id_connect_security_scheme=oidc_oidc)}
        security_requirements = [
            SecurityRequirement(schemes={"orchestrator": StringList(list=["openid", "profile", "email"])})
        ]
    else:
        security_schemes = {}
        security_requirements = []
        logger.warning("OIDC_ISSUER not set – running without authentication (local dev mode)")

    # Support both local dev and production deployment
    # In production, AGENT_BASE_URL should be set to the full URL (e.g., https://domain.com/api/orchestrator)
    # For reload, default to localhost:10001 if not set
    agent_base_url = os.getenv("AGENT_BASE_URL", "http://localhost:10001")

    agent_card = AgentCard(
        name="Orchestrator Agent",
        description="Intelligent orchestrator that plans and coordinates complex tasks by discovering and delegating to specialized sub-agents.",
        supported_interfaces=[
            AgentInterface(url=agent_base_url, protocol_binding="JSONRPC"),
            # v0.3 backward-compat interface: advertised so legacy clients
            # (@a2a-js/sdk 0.3.x Slack/Google-Chat) know JSON-RPC v0.3 is served
            # here until a stable @a2a-js/sdk v1.0 ships.
            AgentInterface(url=agent_base_url, protocol_binding="JSONRPC", protocol_version="0.3"),
        ],
        version="1.0.0",
        default_input_modes=OrchestratorDeepAgent.SUPPORTED_CONTENT_TYPES,
        default_output_modes=OrchestratorDeepAgent.SUPPORTED_CONTENT_TYPES,
        capabilities=capabilities,
        skills=[skill],
        security_schemes=security_schemes,
        security_requirements=security_requirements,
    )

    # Initialize cost logger for tracking LLM usage
    backend_url = os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001")
    cost_logger = CostLogger(backend_url=backend_url, access_token_provider=get_request_access_token)
    logger.info(f"Cost logger initialized with backend: {backend_url}")

    httpx_client = httpx.AsyncClient()
    push_config_store = InMemoryPushNotificationConfigStore()
    push_sender = BasePushNotificationSender(httpx_client=httpx_client, config_store=push_config_store)
    agent_executor = OrchestratorDeepAgentExecutor(cost_logger=cost_logger)
    task_store, task_store_engine = create_task_store()
    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor,
        task_store=task_store,
        agent_card=agent_card,
        push_config_store=push_config_store,
        push_sender=push_sender,
        request_context_builder=AuthRequestContextBuilder(),
    )

    # Build the FastAPI app and mount A2A routes (A2A v1.0+ replaces A2AFastAPIApplication).
    # FastAPI is retained because the orchestrator serves a custom /models endpoint and a
    # hybrid agent-card route (below).
    app = FastAPI(lifespan=create_lifespan(agent_executor, task_store, task_store_engine))
    add_a2a_routes_to_fastapi(
        app,
        # enable_v0_3_compat lets the orchestrator accept legacy v0.3 JSON-RPC on the same
        # endpoint so the @a2a-js/sdk 0.3.x Slack/Google-Chat clients keep working until a
        # stable @a2a-js/sdk v1.0 ships. Internal agents + console-backend speak pure v1.0.
        jsonrpc_routes=create_jsonrpc_routes(request_handler, "/", enable_v0_3_compat=True),
    )

    # Serve a hybrid agent card at the well-known path. The v1.0 card has no top-level
    # `url`, but v0.3 clients resolve the service endpoint from `url` (not from
    # `supported_interfaces`), so we inject it. v1.0 clients ignore the extra field.
    # NOTE (pre-prod gate): the a2a-js 0.3 <-> a2a-python 1.1 v0.3-compat path is
    # cross-implementation and unverified here — smoke-test one real Slack message
    # against a v1.0 orchestrator before relying on it.
    _agent_card_json = MessageToDict(agent_card)
    _agent_card_json["url"] = agent_base_url

    @app.get("/.well-known/agent-card.json")
    async def well_known_agent_card() -> JSONResponse:
        return JSONResponse(_agent_card_json)

    # Add authentication middleware stack (EXECUTION ORDER: bottom to top for requests)

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

    The Model Gateway is the single source of truth for the live model list,
    including any local OpenAI-compatible server (LM Studio, Ollama, vLLM), which is
    registered as a gateway alias rather than probed directly.
    """
    return get_available_models_metadata()


def _caller_azp(request: Request) -> str | None:
    """Read the validated bearer's ``azp`` (authorized party) claim, or None.

    The JWT was already verified by ``JWTValidatorMiddleware`` (signature/issuer/expiry),
    which stores the raw token on ``request.state.user``; here we only read a claim, so
    decoding without re-verifying is fine.
    """
    user = getattr(request.state, "user", None)
    token = user.get("token") if isinstance(user, dict) else None
    if not token:
        return None
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("azp")
    except Exception:
        return None


@app.post("/internal/discovery-cache/invalidate")
async def invalidate_discovery_cache(request: Request) -> JSONResponse:
    """Flush per-user discovery + registry caches on this replica.

    console-backend calls this when a group→MCP-server / group→default-agent mapping or a
    per-user entitlement (role, tool whitelist, bypass rules) changes, so the affected
    users' entitlements take effect on their next turn instead of waiting out the TTL.

    Scope: the body may carry ``{"user_subs": [...]}`` to flush only those users' entries
    (the normal path — console-backend computes the affected set, e.g. the members of the
    changed group). An empty/absent body flushes everything (unscoped admin/maintenance
    flush). Scoping keeps a single group edit from evicting every active user's cache and
    triggering a re-discovery storm.

    Auth: behind the orchestrator's OIDC JWT middleware (not a public path) AND restricted
    here to the console-backend service client — the validated token's ``azp`` must equal
    ``CONSOLE_BACKEND_CLIENT_ID``. This stops any other authenticated principal (e.g. an
    end-user token) from flushing caches. When the auth middleware is disabled (no OIDC
    issuer configured, dev only) there is no token to check and the call is allowed.

    Multi-replica note: the cache is in-process, so one call flushes one replica. Behind a
    load balancer the other replicas fall back to the TTL (kept short for this reason) or a
    ``ENTITLEMENT_POLICY_VERSION`` bump for a fleet-wide flush. A fan-out/pub-sub broadcast
    is the follow-up for instant fleet-wide invalidation.
    """
    azp = _caller_azp(request)
    if azp is not None and azp != AgentSettings.CONSOLE_BACKEND_CLIENT_ID:
        logger.warning("Rejected discovery-cache invalidation from unexpected azp=%s", azp)
        return JSONResponse(status_code=403, content={"error": "forbidden", "message": "caller not permitted"})

    user_subs: list[str] | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            subs = body.get("user_subs")
            if isinstance(subs, list):
                user_subs = [s for s in subs if isinstance(s, str)]
    except Exception:
        body = None  # empty/invalid body → unscoped flush

    if user_subs:
        removed = invalidate_discovery_for_users(user_subs)
        logger.info("Scoped discovery-cache invalidation: %d users, %d entries dropped", len(user_subs), removed)
        return JSONResponse({"status": "ok", "scope": "users", "users": len(user_subs), "removed": removed})

    invalidate_discovery_caches()
    return JSONResponse({"status": "ok", "scope": "all"})


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
