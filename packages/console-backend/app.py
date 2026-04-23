import asyncio
import json
import logging
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import bleach
import httpx
import socketio
import yaml
from a2a.client import A2ACardResolver, A2AClientHTTPError
from a2a.client.client import Client, ClientEvent
from a2a.types import (
    FilePart,
    FileWithUri,
    Message,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
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
from sqlalchemy import text as sa_text
from starlette.middleware.sessions import SessionMiddleware

from playground_backend.config import config
from playground_backend.db import close_db, get_async_session_factory, init_db
from playground_backend.dependencies import require_auth
from playground_backend.exceptions import ConversationOwnershipError
from playground_backend.middleware import OrchestratorAuth, ProxyHeadersMiddleware
from playground_backend.middleware import SessionMiddleware as CustomSessionMiddleware
from playground_backend.models.socket_session import SocketSession
from playground_backend.models.user import User
from playground_backend.routers.admin_audit_router import router as admin_audit_router
from playground_backend.routers.admin_group_router import router as admin_group_router
from playground_backend.routers.admin_user_router import router as admin_user_router
from playground_backend.routers.auth_router import router as auth_router
from playground_backend.routers.catalog_router import router as catalog_router
from playground_backend.routers.conversation_router import router as conversation_router
from playground_backend.routers.delivery_channel_router import router as delivery_channel_router
from playground_backend.routers.file_router import router as file_router
from playground_backend.routers.group_router import router as group_router
from playground_backend.routers.mcp_router import router as mcp_router
from playground_backend.routers.message_router import router as message_router
from playground_backend.routers.models_router import router as models_router
from playground_backend.routers.notification_router import router as notification_router
from playground_backend.routers.rate_card_router import router as rate_card_router
from playground_backend.routers.scheduler_router import router as scheduler_router
from playground_backend.routers.secrets_router import router as secrets_router
from playground_backend.routers.sub_agent_router import router as sub_agent_router
from playground_backend.routers.usage_router import router as usage_router
from playground_backend.service_instances import cleanup_services, initialize_services
from playground_backend.services.conversation_service import ConversationService
from playground_backend.services.messages_service import MessagesService
from playground_backend.services.socket_notification_manager import SocketNotificationManager
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


# ── Agent URL sync at startup ────────────────────────────────────────────────
# Maps env var → agent name seeded in migration 040.
_REMOTE_AGENT_ENV_MAP: dict[str, str] = {
    "VOICE_AGENT_URL": "voice-agent",
    "AGENT_CREATOR_URL": "agent-creator",
}


async def _sync_remote_agent_urls() -> None:
    """Update agent_url for system-owned remote agents from environment variables.

    Called once during startup so that migration-seeded placeholder URLs are
    replaced with the real service URLs configured in the environment.
    """

    session_factory = get_async_session_factory()
    async with session_factory() as db:
        for env_var, agent_name in _REMOTE_AGENT_ENV_MAP.items():
            url = os.environ.get(env_var)
            if not url:
                logger.debug("Skipping agent URL sync for %s (env %s not set)", agent_name, env_var)
                continue
            result = await db.execute(
                sa_text("""
                    UPDATE sub_agent_config_versions cv
                    SET agent_url = :url
                    FROM sub_agents sa
                    WHERE cv.sub_agent_id = sa.id
                      AND sa.name = :name
                      AND sa.owner_user_id = 'system'
                      AND cv.version = sa.default_version
                      AND cv.agent_url IS DISTINCT FROM :url
                """),
                {"url": url, "name": agent_name},
            )
            if result.rowcount:
                logger.info("Synced agent_url for '%s' → %s", agent_name, url)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info("Application starting up...")

    # Initialize PostgreSQL database connection
    await init_db()
    logger.info("PostgreSQL database initialized")

    # Sync system-owned remote agent URLs from environment variables
    await _sync_remote_agent_urls()

    # Initialize services and store in app.state
    await initialize_services(app)
    logger.info("Services initialized")

    # Start scheduler engine
    await app.state.scheduler_engine.start()

    # Start catalog sync engine (if auto-sync is enabled)
    # Always heal stuck jobs (manual syncs can leave orphaned running jobs on restart)
    await app.state.catalog_sync_engine.heal_stuck_jobs()

    # Start the task queue (workers that execute sync jobs)
    if hasattr(app.state, "sync_task_queue"):
        await app.state.sync_task_queue.start(
            handler=app.state.catalog_service.handle_sync_task,
        )
        logger.info("Sync task queue started")

    if config.catalog.auto_sync_enabled:
        await app.state.catalog_sync_engine.start()

    # Start internal cost logger for catalog sync cost tracking
    from playground_backend.services.llm_cost_tracking import _internal_cost_logger

    if _internal_cost_logger is not None:
        await _internal_cost_logger.start()
        logger.info("Internal cost logger started")

    # Start connection pool cleanup task
    connection_pool.start_cleanup_task()
    logger.info("Connection pool cleanup task started")

    logger.info("Application startup complete")

    yield

    # Shutdown - called automatically when Uvicorn receives SIGTERM/SIGINT
    logger.info("Application shutting down...")
    if hasattr(app.state, "scheduler_engine"):
        await app.state.scheduler_engine.stop()
        logger.info("Scheduler engine stopped")
    if hasattr(app.state, "catalog_sync_engine"):
        await app.state.catalog_sync_engine.stop()
        logger.info("Catalog sync engine stopped")
    if hasattr(app.state, "sync_task_queue"):
        await app.state.sync_task_queue.stop()
        logger.info("Sync task queue stopped")
    from playground_backend.catalog.executor import shutdown_sync_executor

    shutdown_sync_executor()
    if _internal_cost_logger is not None:
        await _internal_cost_logger.shutdown()
        logger.info("Internal cost logger stopped")
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
# This is required by Authlib to store temporary OAuth state during login,
# and by the catalog Google OAuth connect flow.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key,
    max_age=600,  # OAuth state expires in 10 minutes
    same_site="lax",
    https_only=not config.is_local(),
    path="/api/v1/",
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
app.include_router(models_router)
app.include_router(notification_router)
# Scheduler router MUST be registered before FastApiMCP instantiation below
app.include_router(scheduler_router)
app.include_router(delivery_channel_router)
app.include_router(catalog_router)

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

# Initialize Socket notification manager for real-time scheduler notifications
socket_notification_manager = SocketNotificationManager(sio)
# Store in app.state for access by services
app.state.socket_notification_manager = socket_notification_manager

# Track active send_message tasks for cancellation: "{sid}:{context_id}" → ActiveTaskInfo


@dataclass
class ActiveTaskInfo:
    """Tracks an active A2A streaming task."""

    asyncio_task: asyncio.Task[Any]
    a2a_client: Client | None = None
    a2a_task_id: str | None = None


active_tasks: dict[str, ActiveTaskInfo] = {}

# Buffer for accumulating streaming artifact chunks per conversation.
# Keyed by context_id. Chunks are assembled here and persisted as a
# single message when the completion status arrives.
# Intermediate output (with urn:nannos:a2a:intermediate-output:1.0 extensions) are NOT accumulated.
_streaming_buffers: dict[str, str] = {}

# Buffer for accumulating intermediate-output chunks (sub-agent thoughts) per conversation.
# Keyed by "{context_id}:{agent_name}". Persisted when the conversation turn ends
# (terminal status or main artifact last_chunk) so reasoning blocks survive page reload.
_intermediate_buffers: dict[str, str] = {}


# ==============================================================================
# Active Task Helpers
# ==============================================================================


def _extract_a2a_task_id(stream_result: ClientEvent | Message) -> str | None:
    """Extract the A2A task_id from a stream event, if present."""
    if isinstance(stream_result, tuple):
        event = stream_result[1] if len(stream_result) > 1 and stream_result[1] else stream_result[0]
    else:
        event = stream_result

    # TaskStatusUpdateEvent and TaskArtifactUpdateEvent have taskId
    if isinstance(event, (TaskStatusUpdateEvent, TaskArtifactUpdateEvent, Message)):
        return event.task_id

    # Task objects have .id
    return event.id


async def _cancel_active_task(task_info: ActiveTaskInfo, key: str, reason: str) -> None:
    """Cancel an active task via A2A protocol + asyncio cancellation.

    Sends tasks/cancel to the orchestrator (best-effort) for clean shutdown,
    then cancels the local asyncio task for immediate cleanup.
    """
    # Send A2A cancel_task if we have a task_id (best-effort, fire-and-forget)
    if task_info.a2a_task_id and task_info.a2a_client:
        try:
            logger.info(f"Sending A2A cancel_task for key={key} task_id={task_info.a2a_task_id} ({reason})")
            await task_info.a2a_client.cancel_task(TaskIdParams(id=task_info.a2a_task_id))
        except Exception:
            logger.warning(f"A2A cancel_task failed for key={key}", exc_info=True)

    # Cancel local asyncio task for immediate UI feedback
    if not task_info.asyncio_task.done():
        task_info.asyncio_task.cancel()
        logger.info(f"Cancelled asyncio task: key={key} ({reason})")


async def _deferred_connection_cleanup(sid: str) -> None:
    """Wait for all tasks of a disconnected socket to finish, then clean up the connection pool."""
    prefix = f"{sid}:"
    while True:
        running = [
            info.asyncio_task
            for key, info in active_tasks.items()
            if key.startswith(prefix) and not info.asyncio_task.done()
        ]
        if not running:
            break
        # Wait for any one task to complete, then re-check
        await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)

    # All tasks finished — safe to tear down the connection
    await connection_pool.remove(sid)
    logger.info(f"Deferred connection cleanup complete for {sid}")


# ==============================================================================
# Socket.IO Event Helpers
# ==============================================================================


async def _emit_debug_log(sid: str, event_id: str, log_type: str, data: Any) -> None:
    """Helper to emit a structured debug log event to the client."""
    await sio.emit(SocketEvents.DEBUG_LOG, {"type": log_type, "data": data, "id": event_id}, to=sid)


async def _flush_intermediate_buffers(
    context_id: str,
    messages_service: Any,
    user_id: str,
    task_id: str = "",
) -> None:
    """Persist accumulated intermediate-output buffers for a conversation.

    Flushes all agent thought buffers matching the context_id prefix,
    saving each as an artifact-update message with the intermediate-output
    extension so the frontend can reconstruct reasoning blocks from history.
    """
    prefix = f"{context_id}:"
    keys_to_flush = [k for k in _intermediate_buffers if k.startswith(prefix)]
    for buf_key in keys_to_flush:
        content = _intermediate_buffers.pop(buf_key)
        if not content.strip():
            continue
        agent_name = buf_key.split(":", 1)[1] if ":" in buf_key else "unknown"
        logger.info(
            f"[STREAMING] Persisting intermediate output ({len(content)} chars) from {agent_name} "
            f"for context {context_id}"
        )
        # Build a synthetic artifact-update payload with the intermediate-output extension
        # so reconstructTimelineFromMessage can reconstruct the reasoning block
        synthetic_payload = json.dumps(
            {
                "kind": "artifact-update",
                "artifact": {
                    "parts": [{"kind": "text", "text": content}],
                    "extensions": ["urn:nannos:a2a:intermediate-output:1.0"],
                    "metadata": {"agent_name": agent_name},
                },
            }
        )
        await messages_service.insert_message(
            conversation_id=context_id,
            user_id=user_id,
            role="assistant",
            parts=[{"kind": "text", "text": content}],
            task_id=task_id,
            state=TaskState.completed,
            kind="artifact-update",
            raw_payload=synthetic_payload,
            metadata={"agent_name": agent_name},
        )


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
        logger.info("Agent response full JSON: %s", json.dumps(response_data, default=str))
    except Exception:
        logger.info("Agent response (sid=%s) id=%s", sid, response_id)

    effective_context_id = context_id or response_data.get("contextId")

    # EMIT IMMEDIATELY for real-time streaming - do this BEFORE database access
    # Artifact chunks with append=true need to appear on the client immediately
    is_streaming_chunk = response_data.get("kind") == "artifact-update" and response_data.get("append")
    if is_streaming_chunk:
        logger.info(
            f"[STREAMING] Emitting artifact chunk immediately: sid={sid}, "
            f"append={response_data.get('append')}, last_chunk={response_data.get('lastChunk')}, context={effective_context_id}"
        )
        await sio.emit(SocketEvents.AGENT_RESPONSE, response_data, to=sid)
        await _emit_debug_log(sid, response_id, "artifact_chunk", response_data)
        # Don't return yet - we still need to accumulate for persistence below

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

                # Detect work-plan events via extensions on the status message
                status_message = (response_data.get("status") or {}).get("message") or {}
                message_extensions = status_message.get("extensions", []) if isinstance(status_message, dict) else []
                is_work_plan = "urn:nannos:a2a:work-plan:1.0" in message_extensions
                is_artifact_append = response_data.get("kind") == "artifact-update" and response_data.get("append")
                # Handle both camelCase and snake_case, check explicitly for boolean value
                # Don't use 'or' because False would fallback to checking second field
                last_chunk_value = response_data.get("lastChunk")
                if last_chunk_value is None:
                    last_chunk_value = response_data.get("last_chunk")
                is_last_chunk = last_chunk_value is True

                # Extract status object early for use in multiple checks below
                status_obj = response_data.get("status", {})
                status_state = status_obj.get("state") if isinstance(status_obj, dict) else None

                # Accumulate streaming artifact text for persistence.
                # Individual chunks are transient (not saved to DB); the assembled
                # content is persisted when last_chunk arrives.
                # IMPORTANT: Do NOT accumulate intermediate output (sub-agent thoughts).
                # Those are display-only events, not part of the final persisted message.
                if is_artifact_append:
                    artifact = response_data.get("artifact", {})
                    artifact_metadata = artifact.get("metadata", {}) if isinstance(artifact, dict) else {}
                    # Detect intermediate output via extensions array on the artifact
                    artifact_extensions = artifact.get("extensions", []) if isinstance(artifact, dict) else []
                    is_intermediate_output = "urn:nannos:a2a:intermediate-output:1.0" in artifact_extensions

                    if not is_intermediate_output:
                        # Only accumulate orchestrator content, not intermediate output
                        parts = artifact.get("parts", []) if isinstance(artifact, dict) else []
                        chunk_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text"))

                        # Always update buffer on artifact-append, even if chunk is empty (for last_chunk handling)
                        current_buffer = _streaming_buffers.get(effective_context_id, "")
                        _streaming_buffers[effective_context_id] = current_buffer + chunk_text
                        if chunk_text:  # Only log if there's actual content
                            logger.info(
                                f"[STREAMING] Accumulated chunk ({len(chunk_text)} chars) for context {effective_context_id}. "
                                f"Total buffer: {len(_streaming_buffers[effective_context_id])} chars. last_chunk={is_last_chunk}. "
                                f"chunk_preview: {chunk_text[:50]}..."
                            )
                        elif is_last_chunk:
                            logger.info(
                                f"[STREAMING] Received final empty chunk for context {effective_context_id}. "
                                f"Buffer size: {len(_streaming_buffers[effective_context_id])} chars"
                            )
                    else:
                        # Accumulate intermediate output for persistence at turn end
                        parts = artifact.get("parts", []) if isinstance(artifact, dict) else []
                        chunk_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text"))
                        if chunk_text:
                            agent_name = artifact_metadata.get("agent_name", "unknown")
                            buf_key = f"{effective_context_id}:{agent_name}"
                            _intermediate_buffers[buf_key] = _intermediate_buffers.get(buf_key, "") + chunk_text
                            logger.info(
                                f"[STREAMING] Intermediate output chunk ({len(chunk_text)} chars) from {agent_name}, "
                                f"buffer: {len(_intermediate_buffers[buf_key])} chars"
                            )

                # Persist accumulated content when the artifact stream closes
                safety_net_saved = False
                if is_last_chunk and effective_context_id in _streaming_buffers:
                    accumulated = _streaming_buffers.pop(effective_context_id)
                    # Flush intermediate-output buffers (sub-agent thoughts) BEFORE
                    # the final response so DB ordering matches logical order
                    await _flush_intermediate_buffers(
                        effective_context_id,
                        messages_service,
                        socket_session.user_id,
                        task_id=response_data.get("taskId", ""),
                    )
                    if accumulated.strip():
                        logger.info(
                            f"[STREAMING] Saving accumulated artifact content ({len(accumulated)} chars) for context {effective_context_id}"
                        )
                        await messages_service.insert_message(
                            conversation_id=effective_context_id,
                            user_id=socket_session.user_id,
                            role="assistant",
                            parts=[{"kind": "text", "text": accumulated}],
                            task_id=response_data.get("taskId", ""),
                            state=TaskState.completed,
                            kind="artifact-update",
                        )
                    else:
                        logger.warning(
                            f"[STREAMING] last_chunk=True but accumulated content is empty for context {effective_context_id}"
                        )
                elif is_last_chunk:
                    logger.warning(
                        f"[STREAMING] last_chunk=True but no buffer found for context {effective_context_id}. "
                        f"kind={response_data.get('kind')}, append={response_data.get('append')}"
                    )
                # Safety net: persist any buffered content on terminal failure
                # in case last_chunk was never sent (e.g. unhandled error)
                elif not is_artifact_append:
                    if status_state in ("failed", "canceled") and effective_context_id in _streaming_buffers:
                        accumulated = _streaming_buffers.pop(effective_context_id)
                        if accumulated.strip():
                            await messages_service.insert_message(
                                conversation_id=effective_context_id,
                                user_id=socket_session.user_id,
                                role="assistant",
                                parts=[{"kind": "text", "text": accumulated}],
                                task_id=response_data.get("taskId", ""),
                                state=TaskState(status_state),
                                kind="status-update",
                            )
                            safety_net_saved = True
                        # Also flush intermediate-output buffers on failure
                        await _flush_intermediate_buffers(
                            effective_context_id,
                            messages_service,
                            socket_session.user_id,
                            task_id=response_data.get("taskId", ""),
                        )

                # Save non-streaming responses to database
                # Skip if:
                # 1. is_work_plan - transient state updates
                # 2. is_artifact_append - streaming chunks (saved when last_chunk arrives)
                # 3. Terminal status without content - pure completion signal after streaming finished
                # 4. safety_net_saved - buffer already flushed for this failed/canceled event
                is_terminal_status_only = (
                    response_data.get("kind") == "status-update"
                    and status_obj
                    and status_obj.get("state") in ("completed", "failed", "canceled")
                    and not status_obj.get("message")  # No nested message content
                )

                # Flush intermediate-output buffers on any terminal status
                if is_terminal_status_only:
                    await _flush_intermediate_buffers(
                        effective_context_id,
                        messages_service,
                        socket_session.user_id,
                        task_id=response_data.get("taskId", ""),
                    )

                if not is_work_plan and not is_artifact_append and not is_terminal_status_only and not safety_net_saved:
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

    # Emit the response to the client (skip if already emitted as streaming chunk)
    if not is_streaming_chunk:
        logger.info(f"[BACKEND_RESPONSE] Emitting response: sid={sid}, kind={response_data.get('kind')}")
        await sio.emit(SocketEvents.AGENT_RESPONSE, response_data, to=sid)
    else:
        logger.debug(
            f"[BACKEND_RESPONSE] Skipping double-emit for streaming chunk: sid={sid}, kind={response_data.get('kind')}, append={response_data.get('append')}"
        )


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

    # Register connection for scheduler notifications
    socket_notification_manager.register_connection(user.id, sid)

    logger.debug(f"Socket.IO connection authenticated for {sid}: {user.email}")
    return True  # Accept the connection


@sio.on(SocketEvents.DISCONNECT)  # type: ignore
async def handle_disconnect(sid: str, reason: str | None = None) -> None:
    """Handle the 'disconnect' socket.io event with reconnection guidance."""
    logger.debug(f"Client disconnected: {sid} (reason: {reason})")

    # Do NOT cancel active agent tasks on disconnect.
    # The user may reconnect (network glitch, page refresh) and results are
    # persisted to DynamoDB regardless.  Explicit cancellation is handled by
    # handle_cancel_task.
    #
    # If tasks are still running we must keep the connection pool entry alive —
    # the httpx client backs the SSE stream the task is consuming.  We schedule
    # deferred cleanup so the connection is removed once all tasks finish.
    has_running_tasks = False
    prefix = f"{sid}:"
    for key in list(active_tasks.keys()):
        if key.startswith(prefix):
            task_info = active_tasks.get(key)
            if task_info and not task_info.asyncio_task.done():
                has_running_tasks = True
            else:
                active_tasks.pop(key, None)

    # Get socket session to  unregister from notification manager
    socket_session = await sio.app_instance.state.socket_session_service.get_session(sid)  # type: ignore[attr-defined]
    if socket_session:
        socket_notification_manager.unregister_connection(socket_session.user_id, sid)

    # Clean up socket session from DynamoDB
    await sio.app_instance.state.socket_session_service.destroy_session(sid)  # type: ignore[attr-defined]

    # Clean up cached connections — defer if tasks are still streaming
    if has_running_tasks:
        logger.info(f"Deferring connection cleanup for {sid} — tasks still running")
        asyncio.create_task(_deferred_connection_cleanup(sid))
    else:
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

    # Playground UI supports all extensions — always request them from the orchestrator
    custom_headers["X-A2A-Extensions"] = (
        "urn:nannos:a2a:activity-log:1.0, urn:nannos:a2a:work-plan:1.0, urn:nannos:a2a:intermediate-output:1.0"
    )

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


async def _save_user_message_to_db(
    conversation_service: ConversationService,
    messages_service: MessagesService,
    context_id: str,
    socket_session: SocketSession | None,
    message_text: str,
    file_attachments: list[dict[str, Any]],
    json_data: dict[str, Any],
    metadata: dict[str, Any],
    message_id: str,
) -> None:
    """Save user message to DynamoDB with conversation tracking.

    Args:
        conversation_service: ConversationService instance
        messages_service: MessagesService instance
        context_id: Conversation context ID
        socket_session: Socket session with user_id (guaranteed non-None after validation)
        message_text: Message text content
        file_attachments: File attachments list
        json_data: Original message data for raw_payload
        metadata: Message metadata
        message_id: Message ID
    """
    try:
        if not socket_session or not socket_session.user_id:
            raise ValueError("Socket session or user ID is missing")

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

        if message_text.strip():
            parts.append({"kind": "text", "text": message_text})

        if file_attachments and isinstance(file_attachments, list):
            for attachment in file_attachments:
                if isinstance(attachment, dict) and "s3Url" in attachment:
                    parts.append(
                        {
                            "kind": "file",
                            "file": {
                                "uri": attachment["s3Url"],
                                "mime_type": attachment.get("mimeType"),
                                "name": attachment.get("name"),
                            },
                        }
                    )
                    logger.info(f"Added file attachment: {attachment.get('name')} ({attachment.get('mimeType')})")

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


def _build_a2a_message_parts(message_text: str, file_attachments: list[dict[str, Any]]) -> list[Any]:
    """Build A2A message parts from text and file attachments.

    Args:
        message_text: Message text content
        file_attachments: File attachments list

    Returns:
        List of A2A message parts (TextPart and/or FilePart)
    """
    a2a_parts: list[Any] = []

    if message_text.strip():
        a2a_parts.append(TextPart(text=str(message_text)))  # type: ignore[arg-type]

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

    return a2a_parts


async def _send_message_to_agent(
    a2a_client: Client | None,
    message: Message,
    sid: str,
    message_id: str,
    sio: socketio.AsyncServer,
    task_key: str | None = None,
) -> dict[str, Any] | None:
    """Send message to agent via A2A client and process stream response.

    Args:
        a2a_client: A2A client instance (guaranteed non-None after validation)
        message: Message to send
        sid: Socket.IO session ID
        message_id: Message ID
        sio: Socket.IO server
        task_key: Key in active_tasks to update with A2A task_id from stream

    Returns:
        Success response or error response if HTTP error occurs
    """
    try:
        assert a2a_client is not None, "a2a_client must not be None"

        response_stream = a2a_client.send_message(message)
        stream_item_count = 0
        a2a_task_id_captured = False
        async for stream_result in response_stream:
            stream_item_count += 1
            result_type = type(stream_result).__name__
            logger.info(f"[BACKEND_STREAM] Received item #{stream_item_count}: type={result_type}")

            # Capture A2A task_id from the first event that carries one
            if not a2a_task_id_captured and task_key:
                extracted_task_id = _extract_a2a_task_id(stream_result)
                if extracted_task_id:
                    task_info = active_tasks.get(task_key)
                    if task_info:
                        task_info.a2a_task_id = extracted_task_id
                        a2a_task_id_captured = True
                        logger.info(f"[BACKEND_STREAM] Captured A2A task_id={extracted_task_id} for {task_key}")

            # Log artifact-update events specifically
            if isinstance(stream_result, tuple):
                event = stream_result[1] if len(stream_result) > 1 and stream_result[1] else stream_result[0]
                if hasattr(event, "kind"):
                    logger.info(f"[BACKEND_STREAM] Event kind: {event.kind}")
                    if event.kind == "artifact-update":
                        artifact = getattr(event, "artifact", None)
                        if artifact and hasattr(artifact, "metadata"):
                            logger.info(f"[BACKEND_STREAM] artifact-update metadata: {artifact.metadata}")

            await _process_a2a_response(stream_result, sid, message_id, message.context_id)

        logger.info(f"[BACKEND_STREAM] Stream complete - received {stream_item_count} total items")
        return create_success_response({"id": message_id})
    except A2AClientHTTPError as http_err:
        logger.error(f"Runtime error during message send: {http_err}", exc_info=True)
        error_response = create_error_response(
            SocketError.MSG_SEND_FAILED,
            details={"reason": f"HTTP error during message send: {http_err}"},
        )
        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
        return error_response


async def _send_steering_message_to_agent(
    a2a_client: Client | None,
    message: Message,
    sid: str,
    message_id: str,
    sio: socketio.AsyncServer,
) -> dict[str, Any] | None:
    """Send a steering message to an agent that is already processing.

    When the orchestrator has an active stream for the same context_id, its
    executor queues the message and returns an immediate acknowledgment.
    We drain that ack stream and notify the frontend that the steering
    message was accepted.

    Args:
        a2a_client: A2A client instance
        message: Steering message (same context_id as active task)
        sid: Socket.IO session ID
        message_id: Message ID

    Returns:
        Success response or error response
    """
    try:
        assert a2a_client is not None, "a2a_client must not be None"

        logger.info(f"[STEERING] Sending steering message for context_id={message.context_id}, message_id={message_id}")

        # Send the steering message — the executor will queue it and return
        # a single ack event (TaskStatusUpdateEvent).  Break after the first
        # event since the tapped child queue also receives parent events that
        # we must NOT consume here (they belong to the original stream).
        async for _ in a2a_client.send_message(message):
            break

        logger.info(f"[STEERING] Steering message accepted for context_id={message.context_id}")

        # Notify the frontend that the steering message was received
        await sio.emit(
            SocketEvents.AGENT_RESPONSE,
            {
                "id": message_id,
                "contextId": message.context_id,
                "steering": True,
                "status": {"state": "accepted"},
            },
            to=sid,
        )
        return create_success_response({"id": message_id, "steering": True})

    except A2AClientHTTPError as http_err:
        logger.error(f"[STEERING] Failed to send steering message: {http_err}", exc_info=True)
        error_response = create_error_response(
            SocketError.MSG_SEND_FAILED,
            details={"reason": f"Steering message failed: {http_err}"},
        )
        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
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

    try:
        socket_session: SocketSession = await sio.app_instance.state.socket_session_service.get_session(sid)  # type: ignore[attr-defined]
        if not socket_session:
            error_response = create_error_response(
                SocketError.SESSION_NOT_FOUND,
                details={"reason": "Client session not found. Please refresh and try again."},
            )
            error_response["id"] = message_id
            await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
            return error_response

        # Validate and clean message input (text and attachments)
        message_text = bleach.clean(json_data.get("message", "") if isinstance(json_data.get("message"), str) else "")
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
        if not isinstance(metadata, dict):
            metadata = {}

        if socket_session.user_id:
            metadata["user_id"] = socket_session.user_id
        else:
            error_response = create_error_response(
                SocketError.SESSION_NOT_FOUND,
                details={"reason": "User ID not found in session. Please refresh and try again."},
            )
            error_response["id"] = message_id
            await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
            return error_response

        # Get A2A client from connection pool (handles creation and recovery automatically)
        if not socket_session.agent_url:
            error_response = create_error_response(
                SocketError.INIT_NOT_INITIALIZED,
                details={"reason": "Client not initialized or agent URL missing"},
            )
            error_response["id"] = message_id
            await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
            return error_response
        a2a_client = await connection_pool.get_or_create_a2a_client(sid, socket_session.agent_url)
        if not a2a_client:
            error_response = create_error_response(
                SocketError.INIT_NOT_INITIALIZED,
                details={"reason": "Connection not found. Please reinitialize the client."},
            )
            error_response["id"] = message_id
            await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
            return error_response

        # Get services
        conversation_service: ConversationService = sio.app_instance.state.conversation_service  # type: ignore[attr-defined]
        messages_service: MessagesService = sio.app_instance.state.messages_service  # type: ignore[attr-defined]

        # Save message to database
        await _save_user_message_to_db(
            conversation_service,
            messages_service,
            context_id,
            socket_session,
            message_text,
            file_attachments,
            json_data,
            metadata,
            message_id,
        )

        # Build and send A2A message
        message = Message(
            role=Role.user,
            parts=_build_a2a_message_parts(message_text, file_attachments),
            message_id=message_id,
            context_id=context_id,
            metadata=metadata,
        )

        task_key = f"{sid}:{context_id}"

        # Steering: if there's already an active task for this context_id,
        # send as a steering message (continuous interaction turn) instead
        # of starting a new full stream.
        existing_task = active_tasks.get(task_key)
        if existing_task is not None and not existing_task.asyncio_task.done():
            logger.info(f"[STEERING] Active task detected for {task_key}, routing as steering message")
            return await _send_steering_message_to_agent(a2a_client, message, sid, message_id, sio)

        send_task = asyncio.create_task(
            _send_message_to_agent(a2a_client, message, sid, message_id, sio, task_key=task_key)
        )
        active_tasks[task_key] = ActiveTaskInfo(
            asyncio_task=send_task,
            a2a_client=a2a_client,
        )
        try:
            return await send_task
        except asyncio.CancelledError:
            logger.info(f"Send message task cancelled: sid={sid}, context_id={context_id}")
            cancelled_response = {
                "id": message_id,
                "contextId": context_id,
                "status": {"state": "cancelled"},
            }
            await sio.emit(SocketEvents.AGENT_RESPONSE, cancelled_response, to=sid)
            return cancelled_response
        finally:
            active_tasks.pop(task_key, None)

    except ValueError as e:
        # Validation errors - send specific error details
        error_response = create_error_response(
            SocketError.MSG_SEND_FAILED,
            details={"reason": str(e)},
        )
        error_response["id"] = message_id
        await sio.emit(SocketEvents.AGENT_RESPONSE, error_response, to=sid)
        return error_response

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


@sio.on(SocketEvents.CANCEL_TASK)  # type: ignore
@require_socket_auth(sio)
async def handle_cancel_task(sid: str, json_data: dict[str, Any]) -> dict[str, Any] | None:
    """Handle the 'cancel_task' socket.io event.

    Cancels an active send_message task for the given conversation.
    """
    conversation_id = json_data.get("conversationId")
    if not conversation_id:
        return create_error_response(SocketError.MSG_SEND_FAILED, details={"reason": "Missing conversationId"})

    # Look up the task by sid + conversationId (the same value used as
    # context_id when the task was created in handle_send_message).
    task_key = f"{sid}:{conversation_id}"
    task_info = active_tasks.pop(task_key, None)
    if task_info:
        await _cancel_active_task(task_info, task_key, reason="user requested")

    if not task_info:
        logger.info(f"No active task to cancel for sid={sid}, conversationId={conversation_id}")

    return create_success_response({"cancelled": task_info is not None})


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
