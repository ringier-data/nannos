"""Twilio Media Streams transport for voice-agent.

Adds the following endpoints to the FastAPI app:

  POST /twilio/voice          — TwiML webhook called by Twilio when a call
                                arrives (inbound) or connects (outbound).
                                Returns TwiML that opens a Media Stream.
  WS   /twilio/stream         — Twilio Media Streams WebSocket.
                                Bridges Twilio ↔ GeminiLiveAgent via asyncio Queues.
  POST /twilio/call           — Initiate an outbound call via the Twilio REST API.
                                Body: {"to": "+41791234567"}

Audio codec pipeline:
  Twilio → Gemini : µ-law 8 kHz  → PCM-16 8 kHz  → PCM-16 16 kHz
  Gemini → Twilio : PCM-16 24 kHz → PCM-16 8 kHz  → µ-law 8 kHz

Local testing (no real Twilio account needed):
  1.  Start the server:
        uv run python -m voice_agent.server
  2.  Run the simulation script:
        uv run python scripts/test_twilio_local.py --tone --output logs/response.wav

Inbound Twilio calls:
  1.  Start the server and expose it:
        uv run python -m voice_agent.server
        ngrok http 8001
  2.  In the Twilio console set your phone number's Voice webhook to:
        POST https://<ngrok-host>/twilio/voice
  3.  Call the number — the call is bridged to Gemini Live.

Outbound Twilio calls:
  1.  Set env vars: TWILIO_ACCOUNT_SID, TWILIO_API_KEY, TWILIO_API_SECRET, TWILIO_PHONE_NUMBER, PUBLIC_URL.
  2.  Start the server and expose it:
        uv run python -m voice_agent.server
        ngrok http 8001  # and set PUBLIC_URL=https://<ngrok-host>
  3.  Trigger a call:
        curl -X POST http://localhost:8002/twilio/call -H 'Content-Type: application/json' \\
             -d '{"to": "+41791234567"}'
     Or via the test script:
        uv run python scripts/test_twilio_local.py --call +41791234567
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

# audioop is stdlib on Python ≤ 3.12; audioop-lts (in dependencies) provides
# the same `audioop` module name on Python 3.13+ where it was removed.
import audioop
from a2a.types import TaskState
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from voice_agent.a2a_agent import VoiceAgent
from voice_agent.call_bridge import (
    _CALL_FUTURES,
    _PENDING_CALLS,
)

logger = logging.getLogger(__name__)

twilio_router = APIRouter(prefix="/twilio", tags=["twilio"])

# ── Audio constants ───────────────────────────────────────────────────────────

_TWILIO_RATE = 8_000  # µ-law sample rate Twilio uses
_GEMINI_IN_RATE = 16_000  # PCM rate Gemini expects
_GEMINI_OUT_RATE = 24_000  # PCM rate Gemini produces
_SW = 2  # sample width: 16-bit = 2 bytes

# Global A2A voice agent instance (shared across WebSocket connections)
_voice_agent = VoiceAgent()


# ── TwiML webhook ─────────────────────────────────────────────────────────────


@twilio_router.post("/voice")
async def twilio_voice_webhook(request: Request) -> Response:
    """Return TwiML that connects the call to our Media Streams WebSocket.

    The WebSocket URL is derived from the request Host header so this works
    transparently with ngrok (https → wss) and in production.
    """
    host = request.headers.get("host", "localhost:8002")
    # Detect TLS: check X-Forwarded-Proto (set by ngrok / load balancers) first,
    # then fall back to the request scheme.
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    scheme = "wss" if forwarded_proto == "https" else "ws"
    stream_url = f"{scheme}://{host}/twilio/stream"
    logger.info("Twilio voice webhook — stream_url=%s (x-forwarded-proto=%s)", stream_url, forwarded_proto)

    twiml = (
        f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="{stream_url}" /></Connect></Response>'
    )
    return Response(content=twiml, media_type="text/xml")


# ── Media Streams WebSocket ───────────────────────────────────────────────────


@twilio_router.websocket("/stream")
async def twilio_stream(websocket: WebSocket) -> None:
    """Handle Twilio Media Streams via A2A VoiceAgent.

    This WebSocket acts as a transport layer that bridges Twilio and the A2A VoiceAgent.
    All audio processing goes through the A2A layer:
    - Twilio audio → A2A agent → GeminiLiveAgent
    - GeminiLiveAgent → A2A agent → Twilio
    """
    await websocket.accept()
    logger.info("Twilio Media Stream connected")

    # State for this connection
    state: dict = {
        "stream_sid": None,
        "call_sid": None,
        "session_key": None,  # A2A session key
        "ratecv_in_state": None,  # audioop.ratecv carry: 8 kHz → 16 kHz
        "ratecv_out_state": None,  # audioop.ratecv carry: 24 kHz → 8 kHz
    }

    # Task for consuming A2A agent output
    agent_output_task = None

    # Transcript accumulated during the call (list of {role, text} dicts)
    transcript: list[dict] = []

    async def _twilio_to_agent() -> None:
        """Receive Twilio frames and feed audio to A2A agent."""
        try:
            async for text in websocket.iter_text():
                msg = json.loads(text)
                event = msg.get("event")

                if event == "connected":
                    logger.debug("Twilio: protocol handshake received")

                elif event == "start":
                    state["stream_sid"] = msg.get("streamSid") or msg["start"]["streamSid"]
                    state["call_sid"] = msg["start"].get("callSid")
                    state["session_key"] = state["call_sid"]  # Use call_sid as session key

                    # Check for custom config (registered by VoiceAgent._stream_phone_call)
                    call_config = _PENDING_CALLS.pop(state["call_sid"], None) if state["call_sid"] else None

                    # Initialize A2A session with config
                    if call_config and call_config.system_prompt:
                        logger.info(
                            "Starting A2A voice session with custom prompt (streamSid=%s, callSid=%s)",
                            state["stream_sid"],
                            state["call_sid"],
                        )
                        init_query = json.dumps(
                            {
                                "system_prompt": call_config.system_prompt,
                                "voice_name": call_config.voice_name or "Kore",
                                "mcp_tools": call_config.mcp_tools or [],
                                "access_token": call_config.access_token,
                            }
                        )
                    else:
                        logger.info(
                            "Starting A2A voice session with default config (streamSid=%s, callSid=%s)",
                            state["stream_sid"],
                            state["call_sid"],
                        )
                        init_query = json.dumps({})  # Use defaults


                    # Start A2A agent streaming (non-blocking)
                    nonlocal agent_output_task
                    agent_output_task = asyncio.create_task(
                        _agent_to_twilio(state["session_key"], init_query)
                    )

                    # Inject context messages as human turns after the session starts.
                    # These are TextParts from the scheduler payload, injected via
                    # inject_text() → audio_in queue → session.send_client_content().
                    # We yield control once so _start_audio_session registers the
                    # session in _active_sessions before we try to inject.
                    if call_config and call_config.context_messages:
                        await asyncio.sleep(0)  # yield to let _start_audio_session register the session
                        for cm in call_config.context_messages:
                            await _voice_agent.inject_text(state["session_key"], cm)
                            logger.info("Injected context message into session %s: %s", state["session_key"], cm[:80])

                elif event == "media":
                    if not state["session_key"]:
                        continue

                    media = msg["media"]
                    if media.get("track", "inbound") != "inbound":
                        continue

                    # Decode and resample audio: µ-law 8kHz → PCM-16 16kHz
                    raw_mulaw = base64.b64decode(media["payload"])
                    pcm_8k = audioop.ulaw2lin(raw_mulaw, _SW)
                    pcm_16k, state["ratecv_in_state"] = audioop.ratecv(
                        pcm_8k,
                        _SW,
                        1,
                        _TWILIO_RATE,
                        _GEMINI_IN_RATE,
                        state["ratecv_in_state"],
                    )

                    # Feed audio to A2A agent
                    await _voice_agent.feed_audio(state["session_key"], pcm_16k)

                elif event == "inject_text":
                    # Test extension: inject text
                    text_input = msg.get("text", "").strip()
                    if text_input and state["session_key"]:
                        logger.info("inject_text: %r", text_input[:120])
                        await _voice_agent.inject_text(state["session_key"], text_input)

                elif event == "stop":
                    logger.info("Twilio: stream stopped (streamSid=%s)", state["stream_sid"])
                    break

        except WebSocketDisconnect:
            logger.info("Twilio WebSocket disconnected")
        except Exception:
            logger.exception("twilio→agent error")
        finally:
            # Signal end of session to agent
            if state["session_key"]:
                await _voice_agent._end_session(state["session_key"])

    async def _agent_to_twilio(session_key: str, init_query: str) -> None:
        """Consume A2A agent output and send to Twilio."""
        try:
            # The call is already connected at this point — start a Gemini Live
            # audio session directly rather than going through _stream_impl
            # (which now requires a phone number and would fail here).
            init_config = json.loads(init_query) if init_query.strip().startswith("{") else {}
            async for response in _voice_agent._start_audio_session(init_config, session_key):
                if response.state == TaskState.failed:
                    logger.error(f"A2A agent failed: {response.content}")
                    break

                # Check for audio chunks
                if response.metadata and response.metadata.get("type") == "audio_chunk":
                    if state["stream_sid"] is None:
                        continue

                    # Get audio from metadata
                    audio_b64 = response.metadata.get("audio")
                    if not audio_b64:
                        continue

                    # Decode and resample: PCM-16 24kHz → PCM-16 8kHz → µ-law 8kHz
                    pcm_24k = base64.b64decode(audio_b64)
                    pcm_8k, state["ratecv_out_state"] = audioop.ratecv(
                        pcm_24k,
                        _SW,
                        1,
                        _GEMINI_OUT_RATE,
                        _TWILIO_RATE,
                        state["ratecv_out_state"],
                    )
                    mulaw = audioop.lin2ulaw(pcm_8k, _SW)
                    payload = base64.b64encode(mulaw).decode("ascii")

                    # Send to Twilio
                    try:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": state["stream_sid"],
                                    "media": {"payload": payload},
                                }
                            )
                        )
                    except (WebSocketDisconnect, RuntimeError):
                        break

                # Handle interruptions
                elif response.metadata and response.metadata.get("type") == "interrupted":
                    if state["stream_sid"]:
                        try:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "event": "clear",
                                        "streamSid": state["stream_sid"],
                                    }
                                )
                            )
                        except (WebSocketDisconnect, RuntimeError):
                            pass
                    # Reset resampler state
                    state["ratecv_out_state"] = None

                # Accumulate transcript
                elif response.metadata and response.metadata.get("type") == "transcript":
                    role = response.metadata.get("role", "assistant")
                    if response.content:
                        if transcript and role == transcript[-1]["role"]:
                            transcript[-1]["text"] += response.content
                        else:
                            transcript.append({"role": role, "text": response.content})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent→twilio error")

    try:
        # Run Twilio input handler
        await _twilio_to_agent()
    finally:
        # Cleanup
        if agent_output_task and not agent_output_task.done():
            agent_output_task.cancel()
            try:
                await agent_output_task
            except asyncio.CancelledError:
                pass

        logger.info("Twilio stream session ended (streamSid=%s)", state["stream_sid"])

        # Resolve A2A future if VoiceAgent._stream_phone_call is waiting for this call to end
        call_sid = state.get("call_sid") or ""
        _fut = _CALL_FUTURES.pop(call_sid, None)
        if _fut and not _fut.done():
            _fut.set_result({"transcript": transcript, "call_sid": call_sid})
            logger.info("Resolved A2A future for call_sid=%s", call_sid)
