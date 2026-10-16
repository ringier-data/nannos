"""A2A-compliant voice agent wrapping GeminiLiveAgent.

A2A message contract for phone calls
-------------------------------------
Callers send a JSON object as the A2A message text::

    {
        "sub_agent_id": 42,               // optional — ID of another sub-agent
                                           //   whose system_prompt / voice_name /
                                           //   mcp_tools should be fetched from
                                           //   the backend and used for the call.
        "system_prompt": "You are …",     // optional — explicit prompt (only used
                                           //   when sub_agent_id is absent).
        "mcp_tools": ["gcal_list_events"] // optional — gateway tools the call may
                                           //   use. Empty / absent = no tools; a
                                           //   sub_agent_id persona's own list (even
                                           //   an empty one) replaces it.
    }

Phone-number resolution (security)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The destination phone number is resolved exclusively from the authenticated
user's JWT claims. The ``phone_number`` JWT claim is computed by a Keycloak
script mapper (phoneNumberOverride ?? phoneNumber), so it already reflects
any override configured in the user's settings.

If no phone number is available the agent returns a failure message
guiding the orchestrator to ask the user for their number.

This "own number only" policy prevents callers from dialing arbitrary numbers.

System-prompt resolution order
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. Backend config fetched via ``sub_agent_id`` → ``system_prompt`` field
2. Explicit ``system_prompt`` in the message body (fallback even when
   ``sub_agent_id`` was set but the stored persona has no prompt — the
   orchestrator always injects the LLM description here)
3. ``DEFAULT_SYSTEM_PROMPT`` hard-coded fallback

Usage patterns
~~~~~~~~~~~~~~
**Orchestrator → voice-agent (case 1)**
    The orchestrator LLM supplies a description and phone number.  No
    ``sub_agent_id`` is needed; the description becomes the call prompt.

**Scheduler with explicit prompt (case 2a)**
    Schedule a job whose payload is
    ``{"phone_number": "…", "system_prompt": "…"}``.

**Scheduler borrowing another sub-agent's config (case 2b)**
    Schedule a job whose payload is
    ``{"phone_number": "…", "sub_agent_id": <N>}``.  Voice-agent fetches
    sub-agent N's ``system_prompt`` / ``voice_name`` / ``mcp_tools`` from
    the console backend and uses them for the Gemini Live session.

Audio-session path (browser WebSocket)
---------------------------------------
When the message does NOT contain ``phone_number``, the agent falls back to
an interactive audio session driven by WebSocket frames.  This path uses
default system prompt / voice settings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from enum import Enum
from typing import AsyncIterable

import httpx
from a2a.types import Message, Task, TaskState
from langsmith import traceable
from pydantic import BaseModel, Field
from ringier_a2a_sdk.agent.base import BaseAgent
from ringier_a2a_sdk.cost_tracking.logger import set_request_access_token, set_request_user_sub
from ringier_a2a_sdk.middleware.credential_injector import BaseCredentialInjector, TokenExchangeCredentialInjector
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig
from ringier_a2a_sdk.oauth.client import OidcOAuth2Client
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

from voice_agent.agent import SYSTEM_PROMPT as DEFAULT_SYSTEM_PROMPT
from voice_agent.agent import GeminiLiveAgent
from voice_agent.call_bridge import (
    _CALL_ANSWERED,
    _CALL_FUTURES,
    _PENDING_CALLS,
    OutboundCallRequest,
    make_outbound_call,
    send_sms,
    build_effective_prompt
)

_CONSOLE_BACKEND_URL = os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001")
_CONSOLE_FRONTEND_URL = os.getenv("CONSOLE_FRONTEND_URL", "http://localhost:5173")
_MCP_GATEWAY_URL: str | None = os.getenv("MCP_GATEWAY_URL") or None

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget tasks — prevents GC before completion.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class VoiceName(str, Enum):
    """
    Zephyr -- Bright
    Kore -- Firm
    Orus -- Firm
    Autonoe -- Bright
    Umbriel -- Easy-going
    Erinome -- Clear
    Laomedeia -- Upbeat
    Schedar -- Even
    Achird -- Friendly
    Sadachbia -- Lively	Puck -- Upbeat
    Fenrir -- Excitable
    Aoede -- Breezy
    Enceladus -- Breathy
    Algieba -- Smooth
    Algenib -- Gravelly
    Achernar -- Soft
    Gacrux -- Mature
    Zubenelgenubi -- Casual
    Sadaltager -- Knowledgeable	Charon -- Informative
    Leda -- Youthful
    Callirrhoe -- Easy-going
    Iapetus -- Clear
    Despina -- Smooth
    Rasalgethi -- Informative
    Alnilam -- Firm
    Pulcherrima -- Forward
    Vindemiatrix -- Gentle
    Sulafat -- Warm
    """

    Zephyr = "Zephyr"
    Kore = "Kore"
    Orus = "Orus"
    Autonoe = "Autonoe"
    Umbriel = "Umbriel"
    Erinome = "Erinome"
    Laomedeia = "Laomedeia"
    Schedar = "Schedar"
    Achird = "Achird"
    Sadachbia = "Sadachbia"
    Puck = "Puck"
    Fenrir = "Fenrir"
    Aoede = "Aoede"
    Enceladus = "Enceladus"
    Algieba = "Algieba"
    Algenib = "Algenib"
    Achernar = "Achernar"
    Gacrux = "Gacrux"
    Zubenelgenubi = "Zubenelgenubi"
    Sadaltager = "Sadaltager"
    Charon = "Charon"
    Leda = "Leda"
    Callirrhoe = "Callirrhoe"
    Iapetus = "Iapetus"
    Despina = "Despina"
    Rasalgethi = "Rasalgethi"
    Alnilam = "Alnilam"
    Pulcherrima = "Pulcherrima"
    Vindemiatrix = "Vindemiatrix"
    Sulafat = "Sulafat"


class VoiceCallRequest(BaseModel):
    """Pydantic model describing the expected JSON payload for phone calls.

    The destination number is resolved exclusively from the
    authenticated user's profile (JWT claim or user-settings override).
    """

    sub_agent_id: int | None = Field(
        default=None,
        description="ID of another sub-agent to borrow config from",
    )
    system_prompt: str | None = Field(
        default=None,
        description="Explicit system prompt for the call",
    )
    voice_name: VoiceName | None = Field(
        default=None,
        description="Gemini Live voice name, e.g. 'Kore'",
    )
    mcp_tools: list[str] = Field(
        default_factory=list,
        description=(
            "Exact names of the MCP gateway tools the call may use, e.g. ['gcal_list_events']. "
            "Leave empty when the call needs no tools: the call then gets none."
        ),
    )


JSON_SCHEMA = VoiceCallRequest.model_json_schema()


def _redact_call_inputs(inputs: dict) -> dict:
    """Trace inputs of a phone call without the caller's bearer token."""
    return {key: ("<redacted>" if key == "access_token" and value else value) for key, value in inputs.items()}


class VoiceAgent(BaseAgent):
    """A2A voice agent wrapping GeminiLiveAgent."""

    SUPPORTED_CONTENT_TYPES = ["application/json"]

    def __init__(self):
        oauth2_client = OidcOAuth2Client(
            client_id=os.getenv("OIDC_CLIENT_ID", "voice-agent"),
            client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
            issuer=os.getenv("OIDC_ISSUER", ""),
        )

        # Create credential injection interceptor with token exchange
        self._credential_injector = TokenExchangeCredentialInjector(
            oidc_client=oauth2_client,
            target_client_id=os.environ.get("MCP_GATEWAY_CLIENT_ID", "gatana"),
            requested_scopes=["openid", "profile", "offline_access"],
        )
        super().__init__()
        self._active_sessions: dict[str, dict] = {}
        # Tracks in-progress pre-warm tasks keyed by call_sid.
        # Populated by _stream_phone_call; consumed by _start_audio_session.
        self._prewarm_tasks: dict[str, asyncio.Task] = {}
        # Tracks sessions where MCP auth failed (set by _start_audio_session
        # on receiving an mcp_auth_failed event from GeminiLiveAgent).
        self._pending_mcp_auth: dict[str, bool] = {}

    def _get_tool_interceptors(self) -> list[BaseCredentialInjector]:
        return [self._credential_injector]

    def _get_tool_interceptors(self) -> list[BaseCredentialInjector]:
        return [self._credential_injector]

    @traceable(name="voice-agent", run_type="chain")
    async def _stream_impl(
        self, messages: list[Message], user_config: UserConfig, task: Task
    ) -> AsyncIterable[AgentStreamResponse]:
        session_key = task.context_id or task.id

        # Extract DataPart (config) and TextParts (context messages) from A2A messages.
        # DataPart carries VoiceCallRequest config (sub_agent_id, voice_name, etc.).
        # TextParts carry human messages to inject into the Gemini Live session.
        config: dict = {}
        context_messages: list[str] = []
        for msg in messages:
            for block in a2a_parts_to_content(msg.parts or []):
                if isinstance(block, dict) and block.get("type") == "non_standard":
                    config = block.get("value", {}).get("data", {})
                elif isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        context_messages.append(text)

        try:
            if not config:
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_FAILED,
                    content=(
                        "Voice agent requires a structured JSON payload (DataPart) with at least "
                        "'phone_number'. Plain text input is not supported."
                        f"Json schema:\n{JSON_SCHEMA}"
                    ),
                )
                return

            # Validate payload through the Pydantic model
            try:
                call_request = VoiceCallRequest(**config)
            except Exception as validation_err:
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_FAILED,
                    content=f"Invalid payload: {validation_err}\nExpected schema:\n{JSON_SCHEMA}",
                )
                return

            async for event in self._handle_phone_call(call_request, user_config, context_messages):
                yield event
        except Exception as e:
            logger.exception(f"Unexpected error in voice agent: {session_key}")
            yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=f"Error: {str(e)}")

    async def _create_audio_session(
        self,
        session_key: str,
        system_prompt: str | None,
        voice_name: str | None,
        mcp_tools: list[str],
        access_token: str | None,
        phone_number: str | None = None,
        session_resumption_handle: str | None = None,
        voice_session_id: str | None = None,
        mcp_headers: dict[str, str] | None = None,
    ) -> None:
        """Create a GeminiLiveAgent, start it, and register in ``_active_sessions``.

        Single source of truth for session creation — used by both
        ``_prewarm_audio_session`` (outbound calls, fired during ringing) and
        ``_start_audio_session`` (cold-start fallback for inbound / browser).

        ``mcp_headers`` may be supplied pre-built (inbound calls, where the backend
        mints a user-scoped gateway token from the stored offline token). Otherwise
        they are derived from ``access_token`` via the credential injector (outbound).
        """
        if session_key in self._active_sessions:
            return

        prompt = (system_prompt or DEFAULT_SYSTEM_PROMPT)

        voice = voice_name or "Kore"

        # No tool list means no tools — never the user's whole gateway catalogue. That
        # catalogue can run to hundreds of tools whose declarations overflow the Live
        # context window (1007 mid-call) and cost an LLM risk score each at setup.
        if not mcp_tools:
            mcp_headers = None
            access_token = None

        if mcp_headers:
            logger.info("Using pre-built MCP headers (session=%s)", session_key)
        elif access_token:
            set_request_user_sub(session_key)
            set_request_access_token(access_token)
            try:
                mcp_headers = await self._credential_injector.get_headers()
                logger.info("Token exchanged for MCP access (session=%s)", session_key)
            except Exception as exc:
                logger.warning("Token exchange failed (%s) — continuing without MCP headers", exc)

        audio_in: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        event_out: asyncio.Queue[dict] = asyncio.Queue()

        agent = GeminiLiveAgent(
            system_prompt=prompt,
            voice_name=voice,
            mcp_gateway_url=_MCP_GATEWAY_URL if mcp_headers else None,
            mcp_headers=mcp_headers,
            mcp_tool_filter=mcp_tools if mcp_tools else None,
            session_resumption_handle=session_resumption_handle,
            session_id=session_key,
        )
        agent_task = asyncio.create_task(agent.run(audio_in, event_out))

        self._active_sessions[session_key] = {
            "audio_in": audio_in,
            "event_out": event_out,
            "agent": agent,
            "agent_task": agent_task,
            "phone_number": phone_number,
            "voice_session_id": voice_session_id,
        }
        logger.info(
            "Gemini session created: %s (voice=%s, mcp_tools=%s, gateway=%s, resume=%s)",
            session_key,
            voice,
            mcp_tools,
            _MCP_GATEWAY_URL,
            bool(session_resumption_handle),
        )

    async def _prewarm_audio_session(
        self,
        session_key: str,
        system_prompt: str,
        voice_name: str | None,
        mcp_tools: list[str],
        access_token: str | None,
        phone_number: str | None = None,
    ) -> None:
        """Pre-warm a Gemini session while Twilio is ringing the callee.

        Fired by ``_stream_phone_call`` *before* ``make_outbound_call()`` is called,
        using a temporary prewarm_key.  Blocks until MCP tools are initialised and
        the Gemini Live WebSocket is open so the session is fully ready the instant
        the callee answers.  After ``make_outbound_call()`` returns the real
        call_sid, the caller re-keys both ``_active_sessions`` and
        ``_prewarm_tasks`` to that call_sid.
        """
        await self._create_audio_session(
            session_key=session_key,
            system_prompt=system_prompt,
            voice_name=voice_name,
            mcp_tools=mcp_tools,
            access_token=access_token,
            phone_number=phone_number,
        )

        session = self._active_sessions.get(session_key)
        if not session:
            return
        agent = session["agent"]

        # agent.run() was just scheduled as a task — yield once so it can start
        # executing and set agent.mcp_status to a Future before we await it.
        await asyncio.sleep(0)

        if agent.mcp_status is not None:
            await asyncio.shield(agent.mcp_status)
        # Wait for the Live connection itself, so a session that fails to connect is
        # caught before dialling instead of ringing the callee only to hang up.
        if agent.session_ready is not None:
            await asyncio.shield(agent.session_ready)

    async def _start_audio_session(
        self,
        init_config: dict,
        session_key: str,
    ) -> AsyncIterable[AgentStreamResponse]:
        """Start a Gemini Live audio session (Twilio / browser WebSocket path).

        For outbound calls the Gemini session is usually pre-warmed by
        ``_stream_phone_call`` while the phone was ringing. This method
        attaches to it, eliminating the cold-start delay after the callee answers.
        Falls back to full setup for inbound calls and browser WebSocket sessions.

        Args:
            init_config: Dict with optional ``system_prompt``, ``voice_name``.
            session_key: Unique key (call_sid or WebSocket session id).
        """
        # ── Attach to pre-warmed session if available ─────────────────────────
        if session_key in self._active_sessions:
            voice_name = self._active_sessions[session_key]["agent"].voice_name
            logger.info("Attaching to pre-warmed Gemini session: %s (voice=%s)", session_key, voice_name)
        else:
            # Pre-warm task may still be connecting (callee answered very quickly)
            prewarm_task = self._prewarm_tasks.pop(session_key, None)
            if prewarm_task is not None and not prewarm_task.done():
                logger.info("Pre-warm still in progress for %s — waiting up to 5 s", session_key)
                try:
                    await asyncio.wait_for(asyncio.shield(prewarm_task), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as exc:
                    logger.warning("Pre-warm wait failed (%s) — falling back to full setup", exc)

            if session_key in self._active_sessions:
                voice_name = self._active_sessions[session_key]["agent"].voice_name
                logger.info("Attached to pre-warmed session after wait: %s (voice=%s)", session_key, voice_name)
            else:
                # ── Cold start (inbound calls, browser WebSocket, pre-warm failed) ──
                await self._create_audio_session(
                    session_key=session_key,
                    system_prompt=init_config.get("system_prompt"),
                    voice_name=init_config.get("voice_name"),
                    mcp_tools=init_config.get("mcp_tools") or [],
                    access_token=init_config.get("access_token"),
                    phone_number=None,
                    session_resumption_handle=init_config.get("gemini_session_handle"),
                    voice_session_id=init_config.get("voice_session_id"),
                    mcp_headers=init_config.get("mcp_headers"),
                )

        voice_name = self._active_sessions[session_key]["agent"].voice_name
        audio_in = self._active_sessions[session_key]["audio_in"]
        event_out = self._active_sessions[session_key]["event_out"]
        yield AgentStreamResponse(
            state=TaskState.TASK_STATE_WORKING,
            content="Voice session initialized",
            metadata={"session_id": session_key, "voice_name": voice_name},
        )

        # Trigger the opening greeting immediately by injecting a text turn.
        # Without this, Gemini waits silently for user audio to clear VAD,
        # causing 3-4 s of dead air after the callee answers.
        await audio_in.put("[call_connected]")
        logger.debug("Injected greeting trigger for session %s", session_key)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(event_out.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                event_type = event.get("type")

                if event_type == "audio_chunk":
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_WORKING, content="", metadata={"type": "audio_chunk", "audio": event["audio"]}
                    )
                elif event_type == "output_transcript":
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_WORKING,
                        content=event.get("text", ""),
                        metadata={"type": "transcript", "role": "assistant"},
                    )
                elif event_type == "input_transcript":
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_WORKING,
                        content=event.get("text", ""),
                        metadata={"type": "transcript", "role": "user"},
                    )
                elif event_type == "turn_complete":
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_WORKING, content="Turn complete", metadata={"type": "turn_complete"}
                    )
                elif event_type == "interrupted":
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_WORKING, content="Interrupted", metadata={"type": "interrupted"}
                    )
                elif event_type == "mcp_auth_failed":
                    authorize_url = event.get("authorize_url", "")
                    caller_number = self._active_sessions.get(session_key, {}).get("phone_number")
                    logger.warning(
                        "MCP auth failed for session %s: %s (authorize_url=%s)",
                        session_key,
                        event.get("message"),
                        authorize_url,
                    )
                    if caller_number and authorize_url:
                        sms_body = f"Your AI assistant needs authorization to use a tool. Please visit: {authorize_url}"
                        try:
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(None, send_sms, caller_number, sms_body)
                            logger.info("MCP auth URL SMS sent (session=%s)", session_key)
                        except Exception as sms_exc:
                            logger.warning("Failed to send MCP auth SMS: %s", sms_exc)
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_WORKING,
                        content="Tool authorization required — SMS sent with instructions.",
                        metadata={"type": "mcp_auth_failed", "authorize_url": authorize_url},
                    )
                elif event_type == "session_resumption_handle":
                    handle = event.get("handle", "")
                    voice_session_id = self._active_sessions.get(session_key, {}).get("voice_session_id")
                    if voice_session_id and handle:
                        from voice_agent.console_client import update_session_handle  # noqa: PLC0415
                        _fire_and_forget(update_session_handle(voice_session_id, handle))
                        logger.debug("Queued session handle update (session=%s)", session_key)
                elif event_type == "error":
                    error_msg = event.get("message", "Unknown error")
                    logger.error(f"Gemini error: {error_msg}")
                    yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=f"Voice processing error: {error_msg}")
                    await self._end_session(session_key)
                    return
        except asyncio.CancelledError:
            logger.info(f"Audio session cancelled: {session_key}")
            await self._end_session(session_key)
            raise
        except Exception as e:
            logger.exception(f"Unexpected error in audio session {session_key}")
            await self._end_session(session_key)
            yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=f"Error: {str(e)}")

    # ── Phone-call orchestration ──────────────────────────────────────────────

    async def _handle_phone_call(
        self, call_request: VoiceCallRequest, user_config: UserConfig, context_messages: list[str] | None = None
    ) -> AsyncIterable[AgentStreamResponse]:
        """Validate inputs, fetch sub-agent config, and initiate the phone call.

        This is the single entry-point for the phone-call path.  It:
          1. Resolves the system prompt using the priority chain:
             sub_agent_id (backend fetch) → explicit system_prompt → context → default.
             Note: Tier 2 is checked even when Tier 1 ran, so the orchestrator's
             LLM description is used when the stored persona has no system_prompt.
          2. Validates that ``phone_number`` is present.
          3. Delegates to ``_stream_phone_call()`` for Twilio + Future handling.
        """
        # ── Resolve system prompt (four-tier priority) ───────────────────────
        #
        # Tier 1 — sub_agent_id: borrow another sub-agent's full config from the
        #   backend.  Comes from the message body.
        #   If the stored persona has no system_prompt, falls through to Tier 2/3.
        #
        # Tier 2 — explicit system_prompt in the message body (orchestrator case 1
        #   and scheduler case 2a).  The orchestrator always injects the LLM
        #   description here so the call context is never lost even when
        #   sub_agent_id is set but the stored persona has no system_prompt or is
        #   voice agent itself.
        #
        # Tier 3 — DEFAULT_SYSTEM_PROMPT hard-coded fallback.
        sub_agent_id = call_request.sub_agent_id
        system_prompt: str | None = None
        voice_name: str | None = call_request.voice_name
        mcp_tools: list[str] = list(call_request.mcp_tools)
        # Unknown names are refused only when the caller typed them and can retry; a
        # persona's stored list runs with whatever subset this user has, as inbound does.
        refuse_unknown_tools = True

        if sub_agent_id is not None:
            # Tier 1 — fetch the named sub-agent's config from the backend.
            agent_config = await self._fetch_sub_agent_config(sub_agent_id, user_config)
            if agent_config is None:
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_FAILED,
                    content=f"Cannot fetch sub-agent {sub_agent_id} config: "
                    "no access token available. Ensure the caller provides a Bearer token.",
                )
                return

            if agent_config.get("name") not in ("voice-agent", "voice_agent"):
                system_prompt = agent_config.get("system_prompt")
                voice_name = voice_name or agent_config.get("voice_name")
                if not agent_config.get("fetch_failed"):
                    mcp_tools = agent_config.get("mcp_tools") or []
                    refuse_unknown_tools = False
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_WORKING,
                    content=f"Loaded sub-agent '{agent_config.get('name', sub_agent_id)}' config.",
                )

        # Tier 2 — explicit system_prompt in the message body.
        # Checked even when sub_agent_id was set, so the orchestrator's LLM
        # description is used as a fallback when the stored persona has no prompt.

        if not system_prompt and call_request.system_prompt:
            system_prompt = call_request.system_prompt
            logger.info(
                "Using explicit system_prompt from message body%s.",
                " (Tier 1 persona had no prompt)" if sub_agent_id is not None else "",
            )

        if not system_prompt:
            system_prompt = DEFAULT_SYSTEM_PROMPT
            logger.info("No system_prompt source found — will use DEFAULT_SYSTEM_PROMPT.")

        # ── Resolve phone number ──────────────────────────────────────────────
        # Security: only the authenticated user's own phone number is allowed.
        #
        # Prefer the JWT claim (pre-resolved by Keycloak mapper). If absent (e.g.
        # the token is an exchanged orchestrator/scheduler token that doesn't carry
        # the phone_number claim), fall back to GET /me on the backend which returns
        # phone_number_override ?? phone_number_idp from the DB.
        phone_number = user_config.phone_number

        if not phone_number:
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content=(
                    "No phone number is configured for your account. "
                    "Please ask the user for their phone number and store it "
                    "in their settings (phone number override) so the voice "
                    "agent can reach them."
                ),
            )
            return

        # ── Initiate the call ─────────────────────────────────────────────────
        async for event in self._stream_phone_call(
            phone_number=phone_number,
            system_prompt=system_prompt,
            voice_name=voice_name,
            mcp_tools=mcp_tools,
            access_token=user_config.access_token.get_secret_value() if user_config.access_token else None,
            context_messages=context_messages or [],
            sub_agent_id=sub_agent_id,
            user_sub=user_config.user_sub,
            refuse_unknown_tools=refuse_unknown_tools,
        ):
            yield event

    async def _fetch_sub_agent_config(self, sub_agent_id: int, user_config: UserConfig) -> dict | None:
        """Fetch sub-agent config from agent-console backend.

        Returns a dict with keys ``name``, ``system_prompt``, ``voice_name``,
        ``mcp_tools`` on success, or a dict with defaults on HTTP failure.
        Returns ``None`` when no access token is available (caller must fail).
        """
        token: str | None = user_config.access_token.get_secret_value() if user_config.access_token else None

        # Local-dev bypass: if no token came in via the request, fall back to a
        # static dev token set in the environment (e.g. obtained via `start-dev.sh`).

        if not token:
            logger.warning(
                "sub_agent_id=%s supplied but no access_token available",
                sub_agent_id,
            )
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{_CONSOLE_BACKEND_URL}/api/v1/sub-agents/{sub_agent_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning(
                "Failed to fetch sub-agent %s config: %s — proceeding with defaults",
                sub_agent_id,
                exc,
            )
            return {"name": f"sub-agent-{sub_agent_id}", "fetch_failed": True}

        cfg = data.get("config_version") or {}

        result = {
            "name": data.get("name", f"sub-agent-{sub_agent_id}"),
            "system_prompt": cfg.get("system_prompt"),
            # voice_name is not yet a backend field; reserved for future schema addition
            "voice_name": cfg.get("voice_name"),
            "mcp_tools": cfg.get("mcp_tools") or [],
        }

        logger.info(
            "Loaded sub-agent %s ('%s'): system_prompt=%s, mcp_tools=%s",
            sub_agent_id,
            result["name"],
            bool(result["system_prompt"]),
            result["mcp_tools"],
        )
        return result

    @traceable(name="voice-phone-call", run_type="tool", process_inputs=_redact_call_inputs)
    async def _stream_phone_call(
        self,
        *,
        phone_number: str,
        system_prompt: str | None,
        voice_name: str | None,
        mcp_tools: list[str],
        access_token: str | None,
        context_messages: list[str] | None,
        sub_agent_id: int | None = None,
        user_sub: str | None = None,
        refuse_unknown_tools: bool = True,
    ) -> AsyncIterable[AgentStreamResponse]:
        """Initiate a Twilio phone call and hold the A2A stream open until it ends.

        Flow:
          1. Pre-warm the Gemini session and await it fully (token exchange +
             WebSocket connect + MCP handshake) — all before the call starts
             ringing so the session is ready the instant the callee answers.
          2. Check MCP status and send SMS if auth failed.
          3. Call make_outbound_call() in a thread executor (sync Twilio REST).
          4. Re-key the prewarm session from the temporary key to call_sid.
          5. Register OutboundCallRequest in _PENDING_CALLS so the Twilio Media
             Stream WebSocket picks up system_prompt / voice_name / mcp_tools.
          6. Register an asyncio.Future in _CALL_FUTURES keyed by call_sid.
          7. Await the future — resolved by twilio_stream's finally block.
          8. Yield ``completed`` with the formatted transcript.
        """
        public_url: str = os.environ.get("PUBLIC_URL") or os.environ.get("VOICE_AGENT_BASE_URL", "")
        connect_timeout: int = int(os.getenv("CALL_CONNECT_TIMEOUT_SECONDS", "60"))
        duration_timeout: int = int(os.getenv("CALL_DURATION_TIMEOUT_SECONDS", "3600"))

        # Validate PUBLIC_URL — Twilio must be able to reach /twilio/voice via this URL.
        # localhost / 127.0.0.1 will cause Twilio to fail silently when the call is answered.
        if not public_url or "localhost" in public_url or "127.0.0.1" in public_url:
            logger.error(
                "PUBLIC_URL is '%s' — Twilio cannot reach this server! "
                "Set PUBLIC_URL to your ngrok URL (e.g. https://xxxx.ngrok-free.app). "
                "Without a reachable URL, Twilio will drop the call when answered.",
                public_url,
            )
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content="Call failed: PUBLIC_URL is not set to a publicly reachable URL. "
                "Twilio needs to reach this server to connect the call. "
                "Set PUBLIC_URL to your ngrok or production URL and restart.",
            )
            return

        yield AgentStreamResponse(
            state=TaskState.TASK_STATE_WORKING,
            content=f"Initiating call to {phone_number}...",
            metadata={"type": "call_initiating", "phone_number": phone_number},
        )

        effective_prompt = build_effective_prompt(system_prompt or DEFAULT_SYSTEM_PROMPT, context_messages)

        # Pre-warm the Gemini session and wait for it to be fully ready before
        # placing the call.  Any latency here (token exchange, WebSocket connect,
        # MCP handshake) is invisible to the caller; the same work done after
        # make_outbound_call() would risk a cold-start if the callee answers fast.
        # asyncio.create_task inherits the current context so contextvars
        # (user token etc.) are available inside _prewarm_audio_session.
        prewarm_key = str(uuid.uuid4())
        prewarm_task = asyncio.create_task(
            self._prewarm_audio_session(
                session_key=prewarm_key,
                system_prompt=effective_prompt,
                voice_name=voice_name,
                mcp_tools=mcp_tools,
                access_token=access_token,
                phone_number=phone_number,
            )
        )
        self._prewarm_tasks[prewarm_key] = prewarm_task
        logger.info("Pre-warming Gemini session before dialling (prewarm_key=%s)", prewarm_key)

        # Wait for the agent's mcp_status future to be resolved before dialling.
        # _prewarm_audio_session creates the agent and starts agent.run(), which
        # sets mcp_status as soon as the MCP handshake succeeds or fails.
        try:
            await asyncio.wait_for(asyncio.shield(prewarm_task), timeout=60.0)
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("Pre-warm did not complete before call: %s — will cold-start on answer", exc)

        # MCP status check — prewarm has had its full time budget so
        # mcp_status should already be resolved or close to it.
        mcp_auth_failed = False
        session = self._active_sessions.get(prewarm_key)
        ready = session["agent"].session_ready if session else None
        if session and (session["agent_task"].done() or (ready is not None and ready.done() and not ready.result())):
            # The Gemini session already died during setup — dialling would connect
            # the callee to silence.
            reason = session["agent"].session_error or "Gemini Live session ended during setup"
            logger.error("Pre-warmed session %s is dead before dialling: %s", prewarm_key, reason)
            self._prewarm_tasks.pop(prewarm_key, None)
            await self._end_session(prewarm_key)
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content=f"Call not placed: the voice session failed to start ({reason}).",
            )
            return
        if session and session["agent"].unknown_tools and not refuse_unknown_tools:
            logger.warning(
                "Persona tools not offered to this user, calling without them: %s",
                sorted(session["agent"].unknown_tools),
            )
        elif session and session["agent"].unknown_tools:
            # Guessed tool names: the call would connect but every tool call would fail
            # ("Unknown tool"). Refuse before dialling and hand back the real names.
            unknown = session["agent"].unknown_tools
            similar = list(dict.fromkeys(name for names in unknown.values() for name in names))
            self._prewarm_tasks.pop(prewarm_key, None)
            await self._end_session(prewarm_key)
            content = (
                "Call not placed: mcp_tools contains names that are not tools of this user: "
                f"{', '.join(sorted(unknown))}. Tool names must match exactly."
            )
            if similar:
                content += (
                    f" Existing tools with similar names: {', '.join(similar)}."
                    " Retry with exact names from that list, or without mcp_tools if the call needs no tools."
                )
            else:
                content += " This user has no similarly named tools; retry without them."
            yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=content)
            return
        if session:
            agent = session["agent"]
            if agent.mcp_status is not None:
                try:
                    mcp_ok = await asyncio.wait_for(asyncio.shield(agent.mcp_status), timeout=15.0)
                    mcp_auth_failed = not mcp_ok
                    logger.info("MCP status: %s", "ok" if mcp_ok else "failed")
                except (asyncio.TimeoutError, Exception) as exc:
                    logger.debug("MCP status check timed out or failed: %s", exc)

        if mcp_auth_failed:
            sms_body = (
                "Your AI assistant is calling but couldn't connect to your tools. "
                f"Please open {_CONSOLE_FRONTEND_URL} and authorize your tool "
                "connections, then try again."
            )
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, send_sms, phone_number, sms_body)
                logger.info("MCP auth failure SMS sent")
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_WORKING,
                    content="SMS sent to caller with tool authorization instructions.",
                    metadata={"type": "mcp_auth_sms_sent"},
                )
            except Exception as sms_exc:
                logger.warning("Failed to send MCP auth SMS: %s", sms_exc)

        # Initiate the call — Gemini session is ready for when the callee answers.
        try:
            loop = asyncio.get_event_loop()
            call_sid: str = await loop.run_in_executor(None, make_outbound_call, phone_number, public_url)
        except Exception as exc:
            logger.error("Failed to initiate Twilio call: %s", exc)
            self._prewarm_tasks.pop(prewarm_key, None)
            if prewarm_key in self._active_sessions:
                await self._end_session(prewarm_key)
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content=f"Call initiation failed: {exc}",
            )
            return

        # Re-key from temporary prewarm_key to call_sid.
        # Prewarm is already complete so the session is guaranteed to be present.
        if prewarm_key in self._active_sessions:
            self._active_sessions[call_sid] = self._active_sessions.pop(prewarm_key)
        prewarm_task = self._prewarm_tasks.pop(prewarm_key, prewarm_task)
        self._prewarm_tasks[call_sid] = prewarm_task
        logger.info("Pre-warm session keyed to call_sid=%s", call_sid)

        # Create the voice session record so the Gemini resume handle is saved
        # (memory always on). Outbound calls never *offer* resume, but an inbound
        # callback can continue an outbound conversation. The handle is persisted
        # during the call via _active_sessions[call_sid]["voice_session_id"].
        voice_session_id = await self._create_outbound_voice_session(
            call_sid=call_sid,
            phone_number=phone_number,
            sub_agent_id=sub_agent_id,
            user_sub=user_sub,
        )

        # Register config so twilio_stream picks it up when the call connects.
        _PENDING_CALLS[call_sid] = OutboundCallRequest(
            to=phone_number,
            system_prompt=system_prompt,
            voice_name=voice_name,
            mcp_tools=mcp_tools,
            access_token=access_token,
            context_messages=context_messages,
            voice_session_id=voice_session_id,
        )

        # Register futures:
        #   _CALL_ANSWERED — resolved by twilio_stream on the "start" event (callee answered)
        #   _CALL_FUTURES  — resolved by twilio_stream's finally block (call ended)
        loop = asyncio.get_event_loop()
        answered_future: asyncio.Future = loop.create_future()
        _CALL_ANSWERED[call_sid] = answered_future
        future: asyncio.Future = loop.create_future()
        _CALL_FUTURES[call_sid] = future

        logger.info(
            "Call %s initiated (connect_timeout=%ds, duration_timeout=%ds)",
            call_sid, connect_timeout, duration_timeout,
        )
        yield AgentStreamResponse(
            state=TaskState.TASK_STATE_WORKING,
            content=f"Call ringing (call_sid={call_sid}). Waiting for the call to complete...",
            metadata={"type": "call_ringing", "call_sid": call_sid},
        )

        # Phase 1 — wait for the callee to answer.
        try:
            await asyncio.wait_for(asyncio.shield(answered_future), timeout=connect_timeout)
            logger.info("Call %s answered", call_sid)
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_WORKING,
                content="Call answered.",
                metadata={"type": "call_answered", "call_sid": call_sid},
            )
        except asyncio.TimeoutError:
            _CALL_ANSWERED.pop(call_sid, None)
            _CALL_FUTURES.pop(call_sid, None)
            logger.warning("Call %s not answered within %ds", call_sid, connect_timeout)
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content=f"Call not answered within {connect_timeout}s.",
            )
            return
        except Exception as exc:
            _CALL_ANSWERED.pop(call_sid, None)
            _CALL_FUTURES.pop(call_sid, None)
            logger.exception("Unexpected error waiting for call %s to be answered", call_sid)
            yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=f"Call error: {exc}")
            return

        # Phase 2 — wait for the call to end.
        try:
            result: dict = await asyncio.wait_for(future, timeout=duration_timeout)
        except asyncio.TimeoutError:
            _CALL_FUTURES.pop(call_sid, None)
            logger.warning("Call %s exceeded max duration of %ds", call_sid, duration_timeout)
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content=f"Call exceeded maximum duration of {duration_timeout}s.",
            )
            return
        except Exception as exc:
            _CALL_FUTURES.pop(call_sid, None)
            logger.exception("Unexpected error waiting for call %s", call_sid)
            yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=f"Call error: {exc}")
            return

        # Format the transcript for the caller
        transcript: list[dict] = result.get("transcript", [])
        lines = "\n".join(f"{'Caller' if t['role'] == 'user' else 'Agent'}: {t['text']}" for t in transcript)
        logger.info("Call %s finished with %d transcript entries", call_sid, len(transcript))

        session_error = result.get("error")
        if session_error:
            content = f"Call failed: the voice session broke off mid-call and the call was ended ({session_error})."
            if lines:
                content += f"\n\nPartial transcript:\n{lines}"
            yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=content)
            return

        if transcript:
            content = f"Call completed.\n\nTranscript:\n{lines}"
        else:
            content = "Call ended with no transcript — nothing either side said was transcribed."
        yield AgentStreamResponse(state=TaskState.TASK_STATE_COMPLETED, content=content)

    async def _create_outbound_voice_session(
        self,
        *,
        call_sid: str,
        phone_number: str,
        sub_agent_id: int | None,
        user_sub: str | None,
    ) -> str | None:
        """Create a voice session record for an outbound call. Returns its id or None.

        Resolves the stable user id from the dialled number (outbound always calls
        the user's own number) so records share the same user_id as inbound calls —
        letting an inbound callback resume an outbound conversation. Best-effort:
        on any failure the call still proceeds, just without a saved resume handle.
        """
        from voice_agent.console_client import (  # noqa: PLC0415
            create_voice_session,
            lookup_user_by_phone,
        )

        try:
            user = await lookup_user_by_phone(phone_number)
        except Exception as exc:
            logger.warning("Outbound voice session: phone lookup failed (%s)", exc)
            user = None
        if user is None:
            logger.warning(
                "Outbound voice session: caller not resolved (sub=%s, call_sid=%s, sub_agent_id=%s) — no record created",
                user_sub,
                call_sid,
                sub_agent_id,
            )
            return None

        try:
            session = await create_voice_session(
                user_id=user.id,
                phone_number=phone_number,
                sub_agent_id=sub_agent_id,
                call_sid=call_sid,
                use_session_memory=True,
            )
        except Exception as exc:
            logger.error("Outbound voice session creation failed: %s", exc)
            return None
        if session is None:
            logger.error("Outbound voice session creation returned None (call_sid=%s)", call_sid)
            return None

        # Make the id available to the resume-handle event handler during the call.
        if call_sid in self._active_sessions:
            self._active_sessions[call_sid]["voice_session_id"] = session.id
        logger.info("Outbound voice session created: id=%s call_sid=%s", session.id, call_sid)
        return session.id

    def get_session_agent(self, session_key: str):
        """Return the live GeminiLiveAgent for a session, or None if not active.

        Held across ``_end_session`` so usage can be read AFTER the session finishes:
        ``_end_session`` only pops the registry entry, the agent object itself stays
        alive. Reading before it returns loses the final turn — end-of-stream makes
        Gemini emit one last response and its usage report (see collect_usage_entries).
        """
        session = self._active_sessions.get(session_key)
        return session["agent"] if session else None

    def collect_usage_entries(self, session_key: str, agent=None) -> list[dict]:
        """Measured spend for a session, as voice-usage report entries.

        Pass `agent` (from ``get_session_agent`` before teardown) to read usage AFTER
        ``_end_session`` has drained the session — that ordering matters, because
        ``_end_session`` sends ``audio_stream_end`` and only then cancels the receive
        loop, so Gemini's final response and its usage_metadata arrive during teardown.
        Reading beforehand silently dropped that last turn.

        Falls back to the registry for callers that still read mid-session. Returns []
        when neither yields an agent, so callers can report unconditionally.
        """
        if agent is None:
            agent = self.get_session_agent(session_key)
        if agent is None:
            logger.warning("collect_usage_entries: no agent for session %s", session_key)
            return []
        try:
            return agent.build_usage_entries()
        except Exception:
            logger.exception("Failed to collect usage entries for session %s", session_key)
            return []

    async def _end_session(self, session_key: str):
        # Cancel any pending pre-warm task that never got picked up
        prewarm_task = self._prewarm_tasks.pop(session_key, None)
        if prewarm_task is not None and not prewarm_task.done():
            prewarm_task.cancel()
        if session_key not in self._active_sessions:
            return
        session = self._active_sessions.pop(session_key)
        try:
            await session["audio_in"].put(None)
        except Exception as e:
            logger.warning(f"Error signaling end-of-stream: {e}")
        if not session["agent_task"].done():
            session["agent_task"].cancel()
            try:
                await session["agent_task"]
            except asyncio.CancelledError:
                pass
        logger.info(f"Voice session ended: {session_key}")

    async def feed_audio(self, session_key: str, audio_data: bytes):
        if session_key in self._active_sessions:
            await self._active_sessions[session_key]["audio_in"].put(audio_data)

    async def inject_text(self, session_key: str, text: str):
        if session_key in self._active_sessions:
            await self._active_sessions[session_key]["audio_in"].put(text)

    async def close(self):
        logger.info("VoiceAgent closing")
        for session_key in list(self._active_sessions.keys()):
            await self._end_session(session_key)
