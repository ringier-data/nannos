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
    the playground backend and uses them for the Gemini Live session.

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
from enum import Enum
from typing import AsyncIterable

import httpx
from a2a.types import Message, Task, TaskState
from langsmith import traceable
from pydantic import BaseModel, Field
from ringier_a2a_sdk.agent.base import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

from voice_agent.agent import SYSTEM_PROMPT as DEFAULT_SYSTEM_PROMPT
from voice_agent.agent import GeminiLiveAgent
from voice_agent.call_bridge import (
    _CALL_FUTURES,
    _PENDING_CALLS,
    OutboundCallRequest,
    make_outbound_call,
)

_PLAYGROUND_BACKEND_URL = os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")

logger = logging.getLogger(__name__)


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


JSON_SCHEMA = VoiceCallRequest.model_json_schema()


class VoiceAgent(BaseAgent):
    """A2A voice agent wrapping GeminiLiveAgent."""

    SUPPORTED_CONTENT_TYPES = ["application/json"]

    def __init__(self):
        super().__init__()
        self._active_sessions: dict[str, dict] = {}

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
                    state=TaskState.failed,
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
                    state=TaskState.failed,
                    content=f"Invalid payload: {validation_err}\nExpected schema:\n{JSON_SCHEMA}",
                )
                return

            async for event in self._handle_phone_call(call_request, user_config, context_messages):
                yield event
        except Exception as e:
            logger.exception(f"Unexpected error in voice agent: {session_key}")
            yield AgentStreamResponse(state=TaskState.failed, content=f"Error: {str(e)}")

    async def _start_audio_session(
        self,
        init_config: dict,
        session_key: str,
    ) -> AsyncIterable[AgentStreamResponse]:
        """Start a Gemini Live audio session (Twilio / browser WebSocket path).

        This is intentionally separate from ``_stream_impl`` so that the A2A
        endpoint always requires a phone number, while Twilio/WebSocket callers
        can still start a real-time session by calling this method directly.

        Args:
            init_config: Dict with optional ``system_prompt``, ``voice_name``.
            session_key: Unique key (call_sid or WebSocket session id).
        """
        system_prompt = init_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        system_prompt += (
            " IMPORTANT: Keep responses short and conversational. Do NOT use markdown, lists, or special characters."
        )
        voice_name = init_config.get("voice_name") or "Kore"

        logger.info(f"Audio session starting: {session_key}")

        audio_in: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        event_out: asyncio.Queue[dict] = asyncio.Queue()

        agent = GeminiLiveAgent(system_prompt=system_prompt, voice_name=voice_name)
        agent_task = asyncio.create_task(agent.run(audio_in, event_out))

        self._active_sessions[session_key] = {
            "audio_in": audio_in,
            "event_out": event_out,
            "agent": agent,
            "agent_task": agent_task,
        }

        yield AgentStreamResponse(
            state=TaskState.working,
            content="Voice session initialized",
            metadata={"session_id": session_key, "voice_name": voice_name},
        )

        try:
            while True:
                try:
                    event = await asyncio.wait_for(event_out.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                event_type = event.get("type")

                if event_type == "audio_chunk":
                    yield AgentStreamResponse(
                        state=TaskState.working, content="", metadata={"type": "audio_chunk", "audio": event["audio"]}
                    )
                elif event_type == "output_transcript":
                    yield AgentStreamResponse(
                        state=TaskState.working,
                        content=event.get("text", ""),
                        metadata={"type": "transcript", "role": "assistant"},
                    )
                elif event_type == "input_transcript":
                    yield AgentStreamResponse(
                        state=TaskState.working,
                        content=event.get("text", ""),
                        metadata={"type": "transcript", "role": "user"},
                    )
                elif event_type == "turn_complete":
                    yield AgentStreamResponse(
                        state=TaskState.working, content="Turn complete", metadata={"type": "turn_complete"}
                    )
                elif event_type == "interrupted":
                    yield AgentStreamResponse(
                        state=TaskState.working, content="Interrupted", metadata={"type": "interrupted"}
                    )
                elif event_type == "error":
                    error_msg = event.get("message", "Unknown error")
                    logger.error(f"Gemini error: {error_msg}")
                    yield AgentStreamResponse(state=TaskState.failed, content=f"Voice processing error: {error_msg}")
                    await self._end_session(session_key)
                    return
        except asyncio.CancelledError:
            logger.info(f"Audio session cancelled: {session_key}")
            await self._end_session(session_key)
            raise
        except Exception as e:
            logger.exception(f"Unexpected error in audio session {session_key}")
            await self._end_session(session_key)
            yield AgentStreamResponse(state=TaskState.failed, content=f"Error: {str(e)}")

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
        mcp_tools: list = []

        if sub_agent_id is not None:
            # Tier 1 — fetch the named sub-agent's config from the backend.
            agent_config = await self._fetch_sub_agent_config(sub_agent_id, user_config)
            if agent_config is None:
                yield AgentStreamResponse(
                    state=TaskState.failed,
                    content=f"Cannot fetch sub-agent {sub_agent_id} config: "
                    "no access token available. Ensure the caller provides a Bearer token.",
                )
                return

            if agent_config.get("name") not in ("voice-agent", "voice_agent"):
                system_prompt = agent_config.get("system_prompt")
                voice_name = voice_name or agent_config.get("voice_name")
                mcp_tools = agent_config.get("mcp_tools") or []
                yield AgentStreamResponse(
                    state=TaskState.working,
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

        if system_prompt:
            system_prompt += """ IMPORTANT: Keep responses short and conversational. Do NOT use markdown, lists, or special characters.
            Ignore any phone number in the prompt, and start by introducing yourself and the meaning of your call."""

        # ── Resolve phone number ──────────────────────────────────────────────
        # Security: only the authenticated user's own phone number is allowed.
        #
        # The phone_number JWT claim is computed by a Keycloak script mapper:
        # phoneNumberOverride ?? phoneNumber (already the resolved value).
        phone_number = user_config.phone_number
        logger.info(f"User config: {user_config.model_dump()}")

        if not phone_number:
            yield AgentStreamResponse(
                state=TaskState.failed,
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
            context="\n".join(context_messages) if context_messages else None,
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
            token = os.environ.get("PLAYGROUND_BACKEND_DEV_TOKEN")
            if token:
                logger.info(
                    "sub_agent_id=%s: no request token; using PLAYGROUND_BACKEND_DEV_TOKEN for config fetch",
                    sub_agent_id,
                )

        if not token:
            logger.warning(
                "sub_agent_id=%s supplied but no access_token available",
                sub_agent_id,
            )
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{_PLAYGROUND_BACKEND_URL}/api/v1/sub-agents/{sub_agent_id}",
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

    @traceable(name="voice-phone-call", run_type="tool")
    async def _stream_phone_call(
        self,
        *,
        phone_number: str,
        system_prompt: str | None,
        voice_name: str | None,
        mcp_tools: list[str],
        context: str | None,
    ) -> AsyncIterable[AgentStreamResponse]:
        """Initiate a Twilio phone call and hold the A2A stream open until it ends.

        Flow:
          1. Call make_outbound_call() in a thread executor (sync Twilio REST).
          2. Register OutboundCallRequest in _PENDING_CALLS so the Twilio Media
             Stream WebSocket picks up system_prompt / voice_name / mcp_tools.
          3. Register an asyncio.Future in _CALL_FUTURES keyed by call_sid.
          4. Await the future — resolved by twilio_stream's finally block.
          5. Yield ``completed`` with the formatted transcript.
        """
        public_url: str = os.environ.get("PUBLIC_URL") or os.environ.get("VOICE_AGENT_BASE_URL", "")
        timeout: int = int(os.getenv("CALL_TIMEOUT_SECONDS", "60"))

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
                state=TaskState.failed,
                content="Call failed: PUBLIC_URL is not set to a publicly reachable URL. "
                "Twilio needs to reach this server to connect the call. "
                "Set PUBLIC_URL to your ngrok or production URL and restart.",
            )
            return

        yield AgentStreamResponse(
            state=TaskState.working,
            content=f"Initiating call to {phone_number}...",
            metadata={"type": "call_initiating", "phone_number": phone_number},
        )

        # Initiate the call via Twilio REST API (sync → thread executor)
        try:
            loop = asyncio.get_event_loop()
            call_sid: str = await loop.run_in_executor(None, make_outbound_call, phone_number, public_url)
        except Exception as exc:
            logger.error("Failed to initiate Twilio call to %s: %s", phone_number, exc)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=f"Call initiation failed: {exc}",
            )
            return

        # Register config so twilio_stream picks it up when the call connects.
        _PENDING_CALLS[call_sid] = OutboundCallRequest(
            to=phone_number,
            system_prompt=system_prompt,
            voice_name=voice_name,
            mcp_tools=mcp_tools,
            context=context,
        )

        # Register future — twilio_stream finally block will resolve it
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        _CALL_FUTURES[call_sid] = future

        logger.info(
            "Call %s initiated, ringing %s (timeout=%ds)",
            call_sid,
            phone_number,
            timeout,
        )
        yield AgentStreamResponse(
            state=TaskState.working,
            content=f"Call ringing (call_sid={call_sid}). Waiting for the call to complete...",
            metadata={"type": "call_ringing", "call_sid": call_sid},
        )

        # Wait for the Twilio stream to end
        try:
            result: dict = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            _CALL_FUTURES.pop(call_sid, None)
            logger.warning("Call %s timed out after %ds", call_sid, timeout)
            yield AgentStreamResponse(
                state=TaskState.failed,
                content=f"Call timed out after {timeout}s without completion.",
            )
            return
        except Exception as exc:
            _CALL_FUTURES.pop(call_sid, None)
            logger.exception("Unexpected error waiting for call %s", call_sid)
            yield AgentStreamResponse(state=TaskState.failed, content=f"Call error: {exc}")
            return

        # Format the transcript for the caller
        transcript: list[dict] = result.get("transcript", [])
        if transcript:
            lines = "\n".join(f"{'Caller' if t['role'] == 'user' else 'Agent'}: {t['text']}" for t in transcript)
            content = f"Call completed.\n\nTranscript:\n{lines}"
        else:
            content = "Call completed — no transcript recorded."

        logger.info("Call %s finished with %d transcript entries", call_sid, len(transcript))
        yield AgentStreamResponse(state=TaskState.completed, content=content)

    async def _end_session(self, session_key: str):
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
