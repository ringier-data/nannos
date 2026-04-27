"""Voice Agent — A2A + Twilio server.

Exposes two interfaces on a single port (default 8001):

1. **A2A task endpoint** (POST /)
   Standard A2A protocol so voice-agent can be used as a remote sub-agent
   by the scheduler via agent-runner.  Send a JSON config string as the user
   message with a ``"phone_number"`` key to trigger an outbound Twilio call.
   The stream stays open (``working``) until the call ends, then returns the
   full transcript as the ``completed`` artifact.

2. **Twilio routes** (/twilio/*)
   TwiML webhook + Media Streams WebSocket for Twilio integration.

JWT authentication is enforced when ``OIDC_ISSUER`` is set in the environment.
When ``OIDC_ISSUER`` is absent the server runs without auth (local dev).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
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
from rcplus_alloy_common.logging import configure_existing_logger, configure_logger
from ringier_a2a_sdk.middleware import (
    JWTValidatorMiddleware,
    SubAgentIdMiddleware,
    UserContextFromRequestStateMiddleware,
)
from ringier_a2a_sdk.server.context_builder import AuthRequestContextBuilder
from ringier_a2a_sdk.server.executor import BaseAgentExecutor

from voice_agent.a2a_agent import JSON_SCHEMA
from voice_agent.twilio_transport import _voice_agent, twilio_router

load_dotenv()

logger = configure_logger("main")
configure_existing_logger(logging.getLogger("voice_agent"))
configure_existing_logger(logging.getLogger("ringier_a2a_sdk"))

# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle hook."""
    logger.info("Voice Agent startup complete")
    yield
    await _voice_agent.close()
    logger.info("Voice Agent shutdown complete")


# ── App factory ───────────────────────────────────────────────────────────────


def create_app():
    """Build and return the configured FastAPI application."""
    agent_base_url = os.getenv("VOICE_AGENT_BASE_URL", "http://localhost:8002")
    oidc_issuer = os.getenv("OIDC_ISSUER")

    # ── Agent card ────────────────────────────────────────────────────────────
    capabilities = AgentCapabilities(streaming=True, push_notifications=True)
    skill = AgentSkill(
        id="voice-call",
        name="Voice Phone Call",
        tags=["voice", "phone", "twilio", "gemini", "transcript"],
        description=(
            "Initiates an outbound phone call."
            "Input MUST be a structured JSON DataPart (application/json). "
            "The phone number is resolved from the authenticated user's profile. "
            "Holds the A2A stream open while the call is active and returns the "
            "full conversation transcript when the call ends.\n\n"
            "Expected JSON schema:\n"
            f"{json.dumps(JSON_SCHEMA, indent=2)}\n\n"
            "System-prompt resolution order: sub_agent_id config > system_prompt > default."
        ),
    )

    # OIDC security scheme — only advertised when auth is configured
    security_schemes = None
    security = None
    if oidc_issuer:
        oidc_scheme = OpenIdConnectSecurityScheme(
            type="openIdConnect",
            open_id_connect_url=f"{oidc_issuer}/.well-known/openid-configuration",
            description="OIDC authentication with token exchange (RFC 8693)",
        )
        security_schemes = {"voice-agent": SecurityScheme(root=oidc_scheme)}
        security = [{"voice-agent": ["openid"]}]

    agent_card = AgentCard(
        name="voice-agent",
        description=(
            "Outbound Twilio phone call agent powered by Gemini Live AI. "
            "Initiates a phone to your configured number, connects the call to Gemini Live Agent, "
            "using the configured system prompt, and returns the full call transcript."
        ),
        url=agent_base_url,
        version="1.0.0",
        default_input_modes=["application/json"],
        default_output_modes=["text", "text/plain"],
        capabilities=capabilities,
        skills=[skill],
        supports_authenticated_extended_card=False,
        security_schemes=security_schemes,
        security=security,
    )

    # ── Request handler + A2A app ─────────────────────────────────────────────
    # Push notification infrastructure: when the caller includes
    # `configuration.pushNotificationConfig` in the A2A message/send request,
    # the SDK stores it and automatically POSTs the completed Task JSON to the
    # registered webhook URL (with X-A2A-Notification-Token) once the stream
    # ends.  This is how agent-runner notifies agent-console after a call.
    _httpx_client = httpx.AsyncClient()
    push_config_store = InMemoryPushNotificationConfigStore()
    push_sender = BasePushNotificationSender(
        httpx_client=_httpx_client,
        config_store=push_config_store,
    )
    request_handler = DefaultRequestHandler(
        agent_executor=BaseAgentExecutor(agent=_voice_agent),
        task_store=InMemoryTaskStore(),
        push_config_store=push_config_store,
        push_sender=push_sender,
        request_context_builder=AuthRequestContextBuilder(),
    )
    server = A2AFastAPIApplication(agent_card=agent_card, http_handler=request_handler)
    app = server.build(lifespan=lifespan)

    # ── Middleware stack ──────────────────────────────────────────────────────
    # LangSmith tracing — always on when LANGSMITH_API_KEY is available
    try:
        from langsmith.middleware import TracingMiddleware  # noqa: PLC0415

        app.add_middleware(TracingMiddleware)
    except ImportError:
        pass

    if oidc_issuer:
        # Twilio webhook/stream requests come from Twilio's servers without a JWT —
        # they must be public.  Subclass to extend the hardcoded PUBLIC_PATHS list.
        class _VoiceAgentJWTMiddleware(JWTValidatorMiddleware):
            PUBLIC_PATHS = JWTValidatorMiddleware.PUBLIC_PATHS + [
                "/twilio/voice",  # TwiML webhook called by Twilio when call connects
                "/twilio/stream",  # Media Streams WebSocket (WS upgrade, no auth header)
                "/twilio/call",  # Direct outbound call trigger (internal use)
                "/twilio/call/custom",
            ]

        # Middleware is added last-first; execution order: JWT → SubAgentId → UserContext
        app.add_middleware(UserContextFromRequestStateMiddleware)
        app.add_middleware(SubAgentIdMiddleware)
        # Accept tokens from both orchestrator (direct A2A calls) and
        # agent-runner (scheduled jobs via SmartTokenInterceptor).
        _allowed_azp = [
            os.getenv("ORCHESTRATOR_CLIENT_ID", "orchestrator"),
            os.getenv("AGENT_RUNNER_CLIENT_ID", "agent-runner"),
        ]
        app.add_middleware(
            _VoiceAgentJWTMiddleware,
            issuer=oidc_issuer,
            expected_azp=_allowed_azp,
        )
        logger.info("JWT authentication enabled (issuer=%s)", oidc_issuer)
    else:
        # DEV MODE: inject fake user context so the executor gets user_name/user_sub
        # without a real JWT.  This lets you test the full A2A flow locally with curl.
        from ringier_a2a_sdk.middleware.user_context_middleware import current_user_context  # noqa: PLC0415
        from starlette.types import ASGIApp, Receive, Scope, Send  # noqa: PLC0415

        class _DevUserContextMiddleware:
            """Inject a fake user context for unauthenticated local development."""

            def __init__(self, app: ASGIApp):
                self.app = app

            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                if scope["type"] == "http":
                    current_user_context.set(
                        {
                            "user_sub": "dev-user",
                            "email": "dev@localhost",
                            "name": "Dev User",
                            "token": None,
                            "scopes": [],
                            "groups": [],
                            "sub_agent_id": None,
                            "phone_number": "+475555555",
                        }
                    )
                try:
                    await self.app(scope, receive, send)
                finally:
                    if scope["type"] == "http":
                        current_user_context.set(None)

        app.add_middleware(_DevUserContextMiddleware)
        logger.info("JWT authentication disabled (OIDC_ISSUER not set) — DEV MODE with fake user context")

    # ── Twilio routes ─────────────────────────────────────────────────────────
    app.include_router(twilio_router)

    return app


# ── App instance (imported by uvicorn) ───────────────────────────────────────

app = create_app()


# ── Entrypoint ────────────────────────────────────────────────────────────────


def main(reload: bool = False) -> None:
    host = os.getenv("HOST", "localhost")
    port = int(os.getenv("PORT", "8002"))
    logger.info("Starting voice-agent on %s:%s", host, port)
    uvicorn.run("voice_agent.server:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
