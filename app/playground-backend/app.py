import asyncio
import json
import logging
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import bleach
import httpx
import socketio
import yaml
from a2a.client import A2ACardResolver, A2AClientHTTPError
from a2a.client.client import ClientEvent
from a2a.types import (
    FilePart,
    FileWithUri,
    Message,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_mcp import FastApiMCP
from rcplus_alloy_common.logging import (
    configure_existing_logger,
    configure_logger,
)
from starlette.middleware.sessions import SessionMiddleware

from playground_backend.config import config
from playground_backend.db import close_db, get_async_session_factory, init_db
from playground_backend.dependencies import require_auth
from playground_backend.exceptions import ConversationOwnershipError
from playground_backend.middleware import OrchestratorAuth, ProxyHeadersMiddleware
from playground_backend.middleware import SessionMiddleware as CustomSessionMiddleware
from playground_backend.models.user import User
from playground_backend.routers.admin_audit_router import router as admin_audit_router
from playground_backend.routers.admin_group_router import router as admin_group_router
from playground_backend.routers.admin_user_router import router as admin_user_router
from playground_backend.routers.auth_router import router as auth_router
from playground_backend.routers.conversation_router import router as conversation_router
from playground_backend.routers.file_router import router as file_router
from playground_backend.routers.group_router import router as group_router
from playground_backend.routers.mcp_router import router as mcp_router
from playground_backend.routers.message_router import router as message_router
from playground_backend.routers.notification_router import router as notification_router
from playground_backend.routers.rate_card_router import router as rate_card_router
from playground_backend.routers.secrets_router import router as secrets_router
from playground_backend.routers.sub_agent_router import router as sub_agent_router
from playground_backend.routers.usage_router import router as usage_router
from playground_backend.service_instances import cleanup_services, initialize_services
from playground_backend.services.messages_service import MessagesService
from playground_backend.utils.connection_pool import connection_pool
from playground_backend.utils.cookie_signer import verify_cookie
from playground_backend.utils.fastapi_mcp_patch import apply_patch
from playground_backend.utils.socket_errors import (
    SocketError,
    create_error_response,
    create_success_response,
)
from playground_backend.utils.socket_events import SocketEvents
from playground_backend.utils.socketio_auth import require_auth as require_socket_auth
from playground_backend.validators import validate_agent_card, validate_message

# NOTE: Apply fastapi_mcp patch (waiting for https://github.com/tadata-org/fastapi_mcp/pull/156)
apply_patch()


logger = configure_logger("chat-inspector")
configure_existing_logger(logging.getLogger("playground_backend"))


# ==============================================================================
# Constants
# ==============================================================================

STANDARD_HEADERS = {
    "host",
    "user-agent",
    "accept",
    "content-type",
    "content-length",
    "connection",
    "accept-encoding",
}

# Maximum text message size in bytes (100KB)
# Note: This limit only applies to text content, not file attachments or artifacts
# which use the Socket.IO max_http_buffer_size limit (10MB)
MAX_TEXT_MESSAGE_SIZE = 100_000


# ==============================================================================
# Graceful Shutdown
# ==============================================================================
async def shutdown_handler() -> None:
    """Handle graceful shutdown of the application."""
    logger.info("Initiating graceful shutdown...")

    try:
        # Notify all connected clients
        await sio.emit(
            SocketEvents.SERVER_SHUTDOWN,
            {
                "message": "Server is shutting down. Please reconnect in a moment.",
            },
        )

        # Give clients time to receive the message
        await asyncio.sleep(1.0)

        # Clean up all connections
        await connection_pool.clear_all()

        logger.info("Graceful shutdown complete")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info("Application starting up...")

    # Initialize PostgreSQL database connection
    await init_db()
    logger.info("PostgreSQL database initialized")

    # Initialize services and store in app.state
    await initialize_services(app)
    logger.info("Services initialized")

    # Start connection pool cleanup task
    connection_pool.start_cleanup_task()
    logger.info("Connection pool cleanup task started")

    logger.info("Application startup complete")

    yield

    # Shutdown - called automatically when Uvicorn receives SIGTERM/SIGINT
    logger.info("Application shutting down...")
    await shutdown_handler()
    await cleanup_services(app)
    await close_db()
    logger.info("Application shutdown complete")


app = FastAPI(
    lifespan=lifespan,
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json",
)

# Add ProxyHeadersMiddleware to properly handle X-Forwarded-* headers from load balancers
# This ensures request.url_for generates URLs with the correct scheme (https) when behind a proxy
if not config.is_local():
    app.add_middleware(ProxyHeadersMiddleware)

# Add CORS middleware for dev/local environments to allow localhost origins
if config.is_local() or config.is_dev():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5001",
            "http://127.0.0.1:5001",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Add Starlette's SessionMiddleware for OAuth state management
# This is required by Authlib to store temporary OAuth state
# Restricted to /api/v1/auth/ path since it's only used during OAuth flow
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    max_age=600,  # OAuth state expires in 10 minutes
    same_site="lax",
    https_only=not config.is_local(),
    path="/api/v1/auth/",
)

# Add custom session middleware to load user from cookies
# Services are accessed from app.state (populated during lifespan startup)
app.add_middleware(CustomSessionMiddleware)

app.include_router(auth_router)
app.include_router(conversation_router)
app.include_router(message_router)
app.include_router(file_router)
app.include_router(mcp_router)
app.include_router(secrets_router)
app.include_router(sub_agent_router)
app.include_router(admin_user_router)
app.include_router(admin_group_router)
app.include_router(admin_audit_router)
app.include_router(group_router)
app.include_router(usage_router)
app.include_router(rate_card_router)
app.include_router(notification_router)

# Configure CORS origins for Socket.IO
# In development, allow localhost. In production, use BASE_DOMAIN env var.
if config.is_local():
    # Allow both http and https for localhost development
    # Include Vite dev server port (5173) for frontend development
    cors_origins = [
        "http://localhost:5001",
        "http://127.0.0.1:5001",
        "https://localhost:5001",
        "https://127.0.0.1:5001",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
else:
    # In production, use the configured base domain
    cors_origins = [f"https://{os.environ['BASE_DOMAIN']}"]

# Create FastAPI-MCP server without auth_config
# Authentication is handled by individual tool endpoints via require_auth_or_bearer_token
# The MCP protocol handshake doesn't require authentication - only tool calls do
mcp = FastApiMCP(
    app,
    include_tags=["MCP"],
)

# Mount the MCP server directly to your FastAPI app using HTTP transport
mcp.mount_http()
# Initialize Socket.IO server with optimized settings
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=cors_origins,
    compression_threshold=1024,  # Compress messages larger than 1KB
    max_http_buffer_size=10_000_000,  # 10MB max payload size
)

# Attach a reference to app so Socket.IO handlers can access app.state
# This allows Socket.IO event handlers to access services, even though is an unconventional pattern
# this should be safe in our setup.
sio.app_instance = app  # type: ignore[attr-defined]


# ==============================================================================
# Socket.IO Event Helpers
# ==============================================================================


async def _emit_debug_log(sid: str, event_id: str, log_type: str, data: Any) -> None:
    """Helper to emit a structured debug log event to the client."""
    await sio.emit(SocketEvents.DEBUG_LOG, {"type": log_type, "data": data, "id": event_id}, to=sid)


async def _process_a2a_response(
    client_event: ClientEvent | Message,
    sid: str,
    request_id: str,
    context_id: str | None = None,
) -> None:
    """Processes a response from the A2A client, validates it, and emits events.

     This function handles the incoming ClientEvent or Message object,
     correlating it with the original request using the session ID and request

    Args:
    client_event: The event or message received.
    sid: The session ID associated with the original request.
    request_id: The unique ID of the original request.
    context_id: The context ID (conversation ID) from the original message request.
    """
    # The response payload 'event' (Task, Message, etc.) may have its own 'id',
    # which can differ from the JSON-RPC request/response 'id'. We prioritize
    # the payload's ID for client-side correlation if it exists.

    event: TaskStatusUpdateEvent | TaskArtifactUpdateEvent | Task | Message
    if isinstance(client_event, tuple):
        event = client_event[1] if client_event[1] else client_event[0]
    else:
        event = client_event

    response_id = getattr(event, "id", request_id)

    response_data = event.model_dump(exclude_none=True)
    response_data["id"] = response_id

    validation_errors = validate_message(response_data)
    response_data["validation_errors"] = validation_errors

    try:
        logger.debug("Agent response full JSON: %s", json.dumps(response_data, default=str))
    except Exception:
        logger.debug("Agent response (sid=%s) id=%s", sid, response_id)

    effective_context_id = context_id or response_data.get("contextId")
    if effective_context_id:
        try:
            socket_session = await sio.app_instance.state.socket_session_service.get_session(sid)  # type: ignore[attr-defined]
            if socket_session:
                # Verify conversation exists and belongs to user
                conversation_service = sio.app_instance.state.conversation_service  # type: ignore[attr-defined]

                conversation = await conversation_service.get_conversation(
                    conversation_id=effective_context_id,
                    user_id=socket_session.user_id,
                )

                if not conversation:
                    raise ConversationOwnershipError(
                        f"Conversation {effective_context_id} does not exist or does not belong to user {socket_session.user_id}"
                    )

                messages_service = sio.app_instance.state.messages_service  # type: ignore[attr-defined]

                # Save agent response
                await messages_service.save_agent_response(
                    response_data=response_data,
                    conversation_id=effective_context_id,
                    user_id=socket_session.user_id,
                )
        except ConversationOwnershipError as ownership_error:
            logger.error(
                f"Conversation ownership violation in agent response: sid={sid}, response_id={response_id}, "
                f"context_id={effective_context_id}, error={ownership_error!s}"
            )
        except Exception as db_error:
            # Log but don't fail the response if DB write fails
            logger.error(f"Failed to save agent response to DynamoDB: {db_error}", exc_info=True)
    else:
        logger.warning(
            f"Agent response for sid={sid} id={response_id} has no contextId - message will not be saved to database"
        )

    await _emit_debug_log(sid, response_id, "response", response_data)
    await sio.emit(SocketEvents.AGENT_RESPONSE, response_data, to=sid)


def get_card_resolver(client: httpx.AsyncClient, agent_card_url: str) -> A2ACardResolver:
    """Returns an A2ACardResolver for the given agent card URL."""
    parsed_url = urlparse(agent_card_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    path_with_query = urlunparse(("", "", parsed_url.path, "", parsed_url.query, ""))
    card_path = path_with_query.lstrip("/")
    if card_path:
        card_resolver = A2ACardResolver(client, base_url, agent_card_path=card_path)
    else:
        card_resolver = A2ACardResolver(client, base_url)

    return card_resolver


# ==============================================================================
# FastAPI Routes
# ==============================================================================


@app.get("/api/v1/health")
async def health_check() -> JSONResponse:
    """Health check endpoint with connection pool statistics."""
    pool_stats = connection_pool.get_stats()
    return JSONResponse(
        content={
            "status": "ok",
            "connection_pool": pool_stats,
        },
        status_code=200,
    )


@app.get("/api/v1/")
async def index() -> JSONResponse:
    """API root endpoint."""
    return JSONResponse(
        content={
            "name": "Playground Backend API",
            "status": "running",
        },
        status_code=200,
    )


@app.post("/api/v1/agent-card")
async def get_agent_card(request: Request, user: User = Depends(require_auth)) -> JSONResponse:
    """Fetch and validate the agent card from a given URL.

    Requires authentication to prevent unauthorized use as an HTTP proxy.
    """
    # 1. Parse request and get sid. If this fails, we can't do much.
    try:
        request_data = await request.json()
        agent_url = request_data.get("url")
        sid = request_data.get("sid")

        if not agent_url or not sid:
            return JSONResponse(
                content={"error": "Agent URL and SID are required."},
                status_code=400,
            )
    except Exception:
        logger.warning("Failed to parse JSON from /api/v1/agent-card request.")
        return JSONResponse(content={"error": "Invalid request body."}, status_code=400)

    # 2. Log the request.
    await _emit_debug_log(
        sid,
        "http-agent-card",
        "request",
        {"endpoint": "/api/v1/agent-card", "payload": request_data},
    )

    # 3. Perform the main action and prepare response.
    try:
        # Agent cards are public and don't require authorization
        async with httpx.AsyncClient(timeout=30.0) as client:
            card_resolver = get_card_resolver(client, agent_url)
            logger.info(f"Fetching agent card for sid={sid} url={agent_url}")
            card = await card_resolver.get_agent_card()

        card_data = card.model_dump(exclude_none=True)
        validation_errors = validate_agent_card(card_data)
        response_data = {
            "card": card_data,
            "validation_errors": validation_errors,
        }
        response_status = 200

    except httpx.RequestError as e:
        logger.error(f"Failed to connect to agent at {agent_url}", exc_info=True)
        response_data = {"error": f"Failed to connect to agent: {e}"}
        response_status = 502  # Bad Gateway
    except Exception as e:
        logger.error("An internal server error occurred", exc_info=True)
        response_data = {"error": f"An internal server error occurred: {e}"}
        response_status = 500

    # 4. Log the response and return it.
    await _emit_debug_log(
        sid,
        "http-agent-card",
        "response",
        {"status": response_status, "payload": response_data},
    )
    return JSONResponse(content=response_data, status_code=response_status)


# ==============================================================================
# Socket.IO Event Handlers & Helpers
# ==============================================================================


@sio.on(SocketEvents.CONNECT)  # type: ignore
async def handle_connect(sid: str, environ: dict[str, Any]) -> bool:
    """Handle the 'connect' socket.io event with authentication.

    Authenticates the connection using the same signed session cookie as HTTP requests.
    Returns False to reject unauthenticated connections.
    """
    # Extract cookies from ASGI environ
    cookie_header = None
    headers = environ.get("asgi.scope", {}).get("headers", [])
    for header_name, header_value in headers:
        if header_name == b"cookie":
            cookie_header = header_value.decode("utf-8")
            break

    if not cookie_header:
        logger.warning(f"Socket.IO connection rejected for {sid}: No cookies found")
        return False

    # Parse cookies to extract session cookie
    cookies = SimpleCookie()
    cookies.load(cookie_header)

    session_cookie = cookies.get(config.cookie_name)
    if not session_cookie:
        logger.warning(f"Socket.IO connection rejected for {sid}: No session cookie")
        return False

    # Verify the signed session cookie (same as HTTP middleware does)
    session_id = verify_cookie(session_cookie.value)
    if not session_id:
        logger.warning(f"Socket.IO connection rejected for {sid}: Invalid session signature")
        return False

    # Load session and user (same as HTTP middleware does)
    stored_session = await sio.app_instance.state.session_service.get_session(session_id)  # type: ignore[attr-defined]
    if not stored_session:
        logger.warning(f"Socket.IO connection rejected for {sid}: Session not found")
        return False

    # Get database session for user lookup
    session_factory = get_async_session_factory()
    async with session_factory() as db:
        user = await sio.app_instance.state.user_service.get_user(db, stored_session.user_id)  # type: ignore[attr-defined]
    if not user:
        logger.warning(f"Socket.IO connection rejected for {sid}: User not found")
        return False

    # Create socket session in DynamoDB (minimal data)
    await sio.app_instance.state.socket_session_service.create_session(  # type: ignore[attr-defined]
        socket_id=sid,
        user_id=user.id,
        http_session_id=session_id,
    )
    logger.debug(f"Socket.IO connection authenticated for {sid}: {user.email}")
    return True  # Accept the connection


@sio.on(SocketEvents.DISCONNECT)  # type: ignore
async def handle_disconnect(sid: str, reason: str | None = None) -> None:
    """Handle the 'disconnect' socket.io event with reconnection guidance."""
    logger.debug(f"Client disconnected: {sid} (reason: {reason})")

    # Clean up socket session from DynamoDB
    await sio.app_instance.state.socket_session_service.destroy_session(sid)  # type: ignore[attr-defined]

    # Clean up cached connections from memory
    await connection_pool.remove(sid)


@sio.on(SocketEvents.INITIALIZE_CLIENT)  # type: ignore
@require_socket_auth(sio)
async def handle_initialize_client(sid: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Handle the 'initialize_client' socket.io event.

    Fetches the agent card once and caches it in DynamoDB. Creates httpx and A2A
    clients and caches them in memory for reuse across messages.

    Args:
        sid: Socket.IO session ID
        data: Request data containing agent URL and optional custom headers

    Returns:
        Response dict sent to client's acknowledgment callback if present
    """
    agent_card_url = data.get("url")
    custom_headers = data.get("customHeaders", {})

    if custom_headers:
        logger.info(f"Received custom headers for {sid}: {list(custom_headers.keys())}")

    if not agent_card_url:
        error_response = create_error_response(SocketError.INIT_URL_REQUIRED)
        await sio.emit(SocketEvents.CLIENT_INITIALIZED, error_response, to=sid)
        return error_response

    httpx_client = None
    try:
        # Get socket session to access HTTP session for auth
        socket_session = await sio.app_instance.state.socket_session_service.get_session(sid)  # type: ignore[attr-defined]
        if not socket_session:
            error_response = create_error_response(SocketError.SESSION_NOT_FOUND)
            await sio.emit(SocketEvents.CLIENT_INITIALIZED, error_response, to=sid)
            return error_response

        # Get HTTP session for access token
        http_session = await sio.app_instance.state.session_service.get_session(socket_session.http_session_id)  # type: ignore[attr-defined]
        if not http_session:
            error_response = create_error_response(SocketError.SESSION_NOT_FOUND)
            await sio.emit(SocketEvents.CLIENT_INITIALIZED, error_response, to=sid)
            return error_response

        # Create httpx client with auth (reused for all messages)
        # Configure with optimized settings for persistent connections
        auth: OrchestratorAuth | None = None
        limits = httpx.Limits(
            max_connections=100,  # Total connections for this client
            max_keepalive_connections=20,  # Keep alive connections
            keepalive_expiry=30.0,  # Keep connections alive for 30s
        )

        if http_session.access_token:
            auth = OrchestratorAuth(
                user_token=http_session.access_token,
                session_id=socket_session.http_session_id,
                session_service=sio.app_instance.state.session_service,  # type: ignore[attr-defined]
                oauth_service=sio.app_instance.state.oauth_service,  # type: ignore[attr-defined]
                cookie_cache=sio.app_instance.state.orchestrator_cookie_cache,  # type: ignore[attr-defined]
                custom_headers=custom_headers,
            )
            httpx_client = httpx.AsyncClient(
                timeout=600.0,
                auth=auth,
                limits=limits,
                http2=True,  # Enable HTTP/2 for better multiplexing
            )
        else:
            logger.warning(f"Creating httpx client without auth for {sid}")
            httpx_client = httpx.AsyncClient(
                timeout=600.0,
                limits=limits,
                http2=True,
            )

        # Cache connection first - this will be used for A2A client creation
        connection_pool.set(sid, httpx_client, auth)

        # Create A2A client using connection pool (fetches agent card and caches client per connection)
        a2a_client = await connection_pool.get_or_create_a2a_client(sid, agent_card_url)
        logger.info(f"A2A client created: {a2a_client is not None}")

        # Get agent card information to send to client
        agent_info = None
        try:
            # Get agent card from connection pool (fetched on-demand)
            agent_card = await connection_pool.get_agent_card(sid, agent_card_url)
            if agent_card:
                logger.info(f"Agent card retrieved from cache: {agent_card.name}")
                # Convert card to dict and add URL
                agent_info = agent_card.model_dump(exclude_none=True)
                agent_info["url"] = agent_card_url
                logger.info(f"Agent card info extracted for {sid}: {agent_info.get('name', 'unknown')}")
            else:
                logger.warning(f"No agent card found in cache for {sid}")
        except Exception as card_error:
            logger.error(f"Failed to extract agent card info for {sid}: {card_error}", exc_info=True)

        try:
            # Store agent URL and custom headers in DynamoDB (for cache lookup and reconnection)
            await sio.app_instance.state.socket_session_service.initialize_client(  # type: ignore[attr-defined]
                socket_id=sid,
                agent_url=agent_card_url,
                custom_headers=custom_headers,
            )

            # Connection was already cached above for A2A client creation
            hostname = None
            try:
                hostname = socket.gethostname()
            except Exception:
                hostname = "unknown"
            instance_info = f"hostname={hostname}, pid={os.getpid()}"
            logger.info(f"Successfully cached connection for sid {sid} on instance {instance_info}")

            success_response = create_success_response({"agent": agent_info})
            await sio.emit(SocketEvents.CLIENT_INITIALIZED, success_response, to=sid)
            return success_response

        except Exception as db_error:
            # Rollback: Close clients if DynamoDB write failed
            logger.error(f"DynamoDB write failed for {sid}, rolling back: {db_error}", exc_info=True)
            await httpx_client.aclose()
            raise

    except Exception as e:
        # Clean up httpx client if initialization failed
        if httpx_client is not None:
            try:
                await httpx_client.aclose()
            except Exception as cleanup_error:
                logger.error(f"Error during cleanup: {cleanup_error}", exc_info=True)

        logger.error(f"Failed to initialize client for {sid}: {e}", exc_info=True)
        error_response = create_error_response(SocketError.INIT_FAILED)
        await sio.emit(SocketEvents.CLIENT_INITIALIZED, error_response, to=sid)
        return error_response


@sio.on(SocketEvents.SEND_MESSAGE)  # type: ignore
@require_socket_auth(sio)
async def handle_send_message(sid: str, json_data: dict[str, Any]) -> dict[str, Any] | None:
    """Handle the 'send_message' socket.io event.

    Uses connection pool for client management and message routing.

    Args:
        sid: Socket.IO session ID
        json_data: Message data with 'message' (string) and optional 'fileAttachments'

    Returns:
        Response dict sent to client's acknowledgment callback if present
    """
    message_id = json_data.get("id", str(uuid4()))

    # Get message text and validate
    message_text = json_data.get("message", "")
    if isinstance(message_text, str):
        message_text = bleach.clean(message_text)
    else:
        message_text = ""

    # Get file attachments
    file_attachments = json_data.get("fileAttachments", [])
    has_text = bool(message_text.strip())
    has_files = bool(file_attachments and isinstance(file_attachments, list) and len(file_attachments) > 0)

    if not has_text and not has_files:
        error_response = create_error_response(
            SocketError.MSG_SEND_FAILED,
            details={"reason": "Message must contain either text content or file attachments"},
        )
        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
        return error_response

    if len(message_text) > MAX_TEXT_MESSAGE_SIZE:
        error_response = create_error_response(
            SocketError.MSG_SIZE_EXCEEDED,
            details={"max_size": MAX_TEXT_MESSAGE_SIZE, "actual_size": len(message_text)},
        )
        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
        return error_response

    context_id = json_data.get("conversationId")

    # Require context_id for all messages
    if not context_id:
        error_response = create_error_response(
            SocketError.MSG_SEND_FAILED,
            details={"reason": "conversationId is required"},
        )
        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
        return error_response

    metadata = json_data.get("metadata", {})

    try:
        # Get agent URL from session
        socket_session = await sio.app_instance.state.socket_session_service.get_session(sid)  # type: ignore[attr-defined]
        if not socket_session or not socket_session.agent_url:
            error_response = create_error_response(
                SocketError.INIT_NOT_INITIALIZED,
                details={"reason": "Client not initialized or agent URL missing"},
            )
            error_response["id"] = message_id
            await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
            return error_response

        # Get A2A client from connection pool (handles creation and recovery automatically)
        a2a_client = await connection_pool.get_or_create_a2a_client(sid, socket_session.agent_url)
        if not a2a_client:
            error_response = create_error_response(
                SocketError.INIT_NOT_INITIALIZED,
                details={"reason": "Connection not found. Please reinitialize the client."},
            )
            error_response["id"] = message_id
            await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
            return error_response

        try:
            logger.info(
                "Outgoing message payload: %s",
                json.dumps(
                    {
                        "sid": sid,
                        "id": message_id,
                        "contextId": context_id,
                        "metadata": metadata,
                        "message_preview": message_text[:200],
                    },
                    default=str,
                ),
            )
        except Exception:
            logger.info("Outgoing message for sid=%s id=%s", sid, message_id)

        try:
            if not isinstance(metadata, dict):
                metadata = {}

            if socket_session and socket_session.user_id:
                metadata["user_id"] = socket_session.user_id

            # Attach authoritative metadata back to original json_data so raw_payload is consistent
            try:
                if isinstance(json_data, dict):
                    json_data["metadata"] = metadata
            except Exception:
                logger.debug("Failed to attach authoritative metadata back to json_data for sid %s", sid)
        except Exception as val_err:
            logger.exception("Error enforcing authoritative metadata.user_id for sid %s: %s", sid, val_err)

        try:
            # Ensure conversation exists (creates if doesn't exist, validates ownership if exists)
            conversation_service = sio.app_instance.state.conversation_service  # type: ignore[attr-defined]

            # Extract sub_agent_config_hash from metadata for playground mode
            sub_agent_config_hash = metadata.get("subAgentConfigHash") if isinstance(metadata, dict) else None
            logger.info(
                f"Creating/getting conversation {context_id} with sub_agent_config_hash={sub_agent_config_hash}, metadata={metadata}"
            )

            await conversation_service.get_or_create_conversation(
                conversation_id=context_id,
                user_id=socket_session.user_id,
                agent_url=socket_session.agent_url or "",
                message=message_text,
                sub_agent_config_hash=sub_agent_config_hash,
            )

            # Build parts array: text part (if non-empty) + file parts (if any)
            parts = []

            # Add text part only if there's actual text content
            if message_text.strip():
                parts.append({"kind": "text", "text": message_text})

            # Add file parts from attachments
            if file_attachments and isinstance(file_attachments, list):
                for attachment in file_attachments:
                    if isinstance(attachment, dict) and "uri" in attachment:
                        parts.append(
                            {
                                "kind": "file",
                                "file": {
                                    "uri": attachment["uri"],
                                    "mime_type": attachment.get("mimeType"),
                                    "name": attachment.get("name"),
                                },
                            }
                        )
                        logger.info(f"Added file attachment: {attachment.get('name')} ({attachment.get('mimeType')})")

            messages_service: MessagesService = sio.app_instance.state.messages_service  # type: ignore[attr-defined]
            await messages_service.insert_message(
                conversation_id=context_id,
                user_id=socket_session.user_id,
                role="user",
                parts=parts,
                task_id="",
                state=TaskState.working,
                raw_payload=json.dumps(json_data, default=str),
                metadata=metadata,
                message_id=message_id,
            )
            logger.info(f"Saved user message {message_id} to conversation {context_id}")
        except Exception as db_error:
            logger.error(f"Failed to save user message to DynamoDB: {db_error}", exc_info=True)

        # Build A2A message with text and file parts
        a2a_parts = []

        # Add text part if there's content
        if message_text.strip():
            a2a_parts.append(TextPart(text=str(message_text)))  # type: ignore[arg-type]

        # Add file parts
        if file_attachments and isinstance(file_attachments, list):
            for attachment in file_attachments:
                if isinstance(attachment, dict) and "uri" in attachment:
                    file_part = FilePart(
                        file=FileWithUri(
                            uri=attachment["uri"],
                            mime_type=attachment.get("mimeType"),
                            name=attachment.get("name"),
                        )
                    )
                    a2a_parts.append(file_part)  # type: ignore[arg-type]

        message = Message(
            role=Role.user,
            parts=a2a_parts,
            message_id=message_id,
            context_id=context_id,
            metadata=metadata,
        )

        response_stream = a2a_client.send_message(message)
        try:
            async for stream_result in response_stream:
                await _process_a2a_response(stream_result, sid, message_id, message.context_id)
        except A2AClientHTTPError as http_err:
            logger.error(f"Runtime error during message send: {http_err}", exc_info=True)
            error_response = create_error_response(
                SocketError.MSG_SEND_FAILED,
                details={"reason": f"HTTP error during message send: {http_err}"},
            )
            error_response["id"] = message_id
            await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
            return error_response
        return create_success_response({"id": message_id})

    except ConversationOwnershipError as e:
        logger.warning(f"Conversation ownership violation: sid={sid}, message_id={message_id}, error={e!s}")
        error_response = create_error_response(
            SocketError.MSG_SEND_FAILED,
            details={"reason": "Access denied: You do not have permission to access this conversation."},
        )
        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
        return error_response

    except Exception as e:
        # Log error with structured fields
        log_extra = {
            "sid": sid,
            "message_id": message_id,
            "error_type": type(e).__name__,
        }

        # Extract upstream response details if available
        resp = getattr(e, "response", None) or getattr(getattr(e, "__cause__", None), "response", None)
        if resp and hasattr(resp, "status_code"):
            log_extra["status_code"] = resp.status_code
            log_extra["content_type"] = getattr(resp, "headers", {}).get("Content-Type")

            # Try to capture response body
            body = getattr(resp, "text", None) or getattr(resp, "content", None)
            if body:
                body_str = body[:1000] if isinstance(body, str) else str(body)[:1000]
                log_extra["body_preview"] = body_str
                # Also log body explicitly for visibility
                logger.error(f"Upstream error response body: {body_str}")

        logger.error("Failed to send message", extra=log_extra, exc_info=True)

        # Handle session expiration - requires reinitialization
        if "Session expired" in str(e) or "refresh access token" in str(e).lower():
            await connection_pool.remove(sid)
            error_response = create_error_response(
                SocketError.MSG_SEND_FAILED,
                details={"reason": "Your session has expired. Please refresh the page and log in again."},
            )
        else:
            # For other errors, return generic failure
            error_response = create_error_response(SocketError.MSG_SEND_FAILED)

        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
        return error_response


# ==============================================================================
# Main Execution
# ==============================================================================

# Wrap FastAPI app with Socket.IO
# This creates a combined ASGI app that handles both HTTP and WebSocket
# The socketio_path sets the URL path where Socket.IO listens (default is 'socket.io')
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app, socketio_path="/api/v1/socket.io")


if __name__ == "__main__":
    import uvicorn

    # NOTE: The 'reload=True' flag is for development purposes only.
    # In a production environment, use a proper process manager like Gunicorn.
    log_config = yaml.safe_load("log_conf.yml")
    # Run the combined ASGI app (Socket.IO + FastAPI)
    uvicorn.run("app:asgi_app", host="127.0.0.1", port=5001, reload=True, log_config=log_config, access_log=False)
