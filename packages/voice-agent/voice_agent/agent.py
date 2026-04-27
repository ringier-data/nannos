"""Gemini Live agent — all session logic, decoupled from transport.

The agent communicates via two asyncio Queues:
  audio_in:  bytes | None — raw 16-bit PCM (16 kHz) chunks; None signals end-of-stream
  event_out: dict         — JSON-serialisable events for the caller

Event types written to event_out:
  audio_chunk       — {"type": "audio_chunk", "audio": "<base64 PCM 24 kHz>"}
  input_transcript  — {"type": "input_transcript", "text": "..."}
  output_transcript — {"type": "output_transcript", "text": "..."}
  interrupted       — {"type": "interrupted"}
  turn_complete     — {"type": "turn_complete"}
  error             — {"type": "error", "message": "..."}

Example usage::

    agent = GeminiLiveAgent()
    audio_in: asyncio.Queue[bytes | None] = asyncio.Queue()
    event_out: asyncio.Queue[dict] = asyncio.Queue()
    await agent.run(audio_in, event_out)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import logging.handlers
import os
import pathlib
import time
from datetime import timedelta

from google import genai
from google.genai import types
from langsmith import traceable
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

# ── Event file logger ─────────────────────────────────────────────────────────

_LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_event_logger = logging.getLogger("voice_agent.events")
_event_logger.setLevel(logging.DEBUG)
_event_logger.propagate = False
_fh = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "events.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=0,
    encoding="utf-8",
)
_fh.setFormatter(logging.Formatter("%(message)s"))
_event_logger.addHandler(_fh)

# ── Gemini client ─────────────────────────────────────────────────────────────


def build_gemini_client() -> genai.Client:
    """Build a Gemini client authenticated via Vertex AI service account.

    Reads GCP_KEY (full service account JSON blob), GCP_PROJECT_ID, and
    GCP_LOCATION from environment — same convention as orchestrator-agent.
    """
    gcp_key = os.getenv("GCP_KEY")
    gcp_project = os.getenv("GCP_PROJECT_ID")
    gcp_location = os.getenv("GCP_LOCATION", "us-central1")

    if not gcp_key:
        raise RuntimeError("GCP_KEY is not set — add the service account JSON blob to .env")
    if not gcp_project:
        raise RuntimeError("GCP_PROJECT_ID is not set — add it to .env")

    try:
        from google.oauth2 import service_account as _sa  # noqa: PLC0415

        credentials = _sa.Credentials.from_service_account_info(
            json.loads(gcp_key),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Failed to parse GCP_KEY as service account JSON: {exc}") from exc

    logger.info(
        "Building Gemini Vertex AI client (project=%s, location=%s)",
        gcp_project,
        gcp_location,
    )
    return genai.Client(
        vertexai=True,
        credentials=credentials,
        project=gcp_project,
        location=gcp_location,
    )


# ── Config ────────────────────────────────────────────────────────────────────

MODEL_ID = os.getenv("GEMINI_MODEL_ID", "gemini-live-2.5-flash-native-audio")
VOICE_NAME = os.getenv("GEMINI_VOICE", "Kore")

SYSTEM_PROMPT = """
 You are a helpful voice assistant for a product named Nannos.
 In your first response, greet him and introduce yourself as a voice assistant for Nannos,
 and provide assistance as needed. Keep your responses concise and natural — as if speaking out loud.
 Do NOT use markdown, bullet points, numbered lists, or special characters.
 Respond in short, clear sentences."""


# ── Tools ─────────────────────────────────────────────────────────────────────


# Maps tool name → callable for receive_loop dispatch.
_TOOL_MAP: dict[str, object] = {}


# ── Live config ───────────────────────────────────────────────────────────────


def build_live_config(
    voice_name: str = VOICE_NAME,
    system_prompt: str | None = None,
    tools: list | None = None,
) -> types.LiveConnectConfig:
    """Build Gemini Live configuration with optional custom system prompt.

    Args:
        voice_name: Voice to use (Puck, Charon, Kore, Fenrir, Aoede, Leda, Orus, Zephyr)
        system_prompt: Custom system prompt. If None, uses default SYSTEM_PROMPT.
        tools: List of tools to make available. If None, uses default [get_current_time].
    """
    prompt = system_prompt if system_prompt is not None else SYSTEM_PROMPT
    tool_list = tools

    return types.LiveConnectConfig(
        response_modalities=["audio"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name),
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=prompt)]),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=tool_list,
    )


# ── Agent ─────────────────────────────────────────────────────────────────────


class GeminiLiveAgent:
    """Gemini Live session, decoupled from transport.

    Drives a persistent Gemini Live WebSocket session.  All audio I/O and
    events flow through asyncio Queues so this class has zero dependency on
    FastAPI or WebSockets — it can be wrapped in an A2A runnable or driven
    directly from a test harness.
    """

    def __init__(
        self,
        model_id: str = MODEL_ID,
        voice_name: str = VOICE_NAME,
        system_prompt: str | None = None,
        tool_map: dict[str, object] | None = None,
        mcp_gateway_url: str | None = None,
        mcp_headers: dict[str, str] | None = None,
        mcp_tool_filter: list[str] | None = None,
    ) -> None:
        self.model_id = model_id
        self.voice_name = voice_name
        self.system_prompt = system_prompt
        self.tool_map = tool_map if tool_map is not None else _TOOL_MAP.copy()
        self.mcp_gateway_url = mcp_gateway_url
        self.mcp_headers = mcp_headers
        self.mcp_tool_filter = mcp_tool_filter
        self._config = build_live_config(voice_name, system_prompt)
        # Resolved when the MCP connection status is known:
        #   True  = connected successfully
        #   False = connection/auth failed, running without tools
        # None when no MCP gateway is configured.
        self.mcp_status: asyncio.Future[bool] | None = None

    async def _init_mcp_tools(
        self, mcp_session: ClientSession, event_out: asyncio.Queue[dict]
    ) -> list[types.FunctionDeclaration]:
        """Discover tools from an already-initialised MCP session.

        Mutates ``self.tool_map`` with async executors for each discovered tool
        and returns the list of ``FunctionDeclaration``s for building the
        Gemini Live connect config.
        """
        tools_result = await mcp_session.list_tools()
        declarations: list[types.FunctionDeclaration] = []

        # Flag to ensure we only emit one mcp_auth_failed event (and thus
        # one SMS) per session, regardless of how many tools need credentials.
        # Wrapped in a list so closures can mutate it.
        _auth_notified = [False]

        for tool in tools_result.tools:
            # If a tool filter was specified, skip tools not in the list.
            if self.mcp_tool_filter and tool.name not in self.mcp_tool_filter:
                logger.debug("MCP tool %r skipped (not in filter %s)", tool.name, self.mcp_tool_filter)
                continue
            raw_schema = dict(tool.inputSchema or {})
            logger.debug("MCP tool schema for %r: %s", tool.name, raw_schema)
            declarations.append(
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description or "",
                    parameters_json_schema=raw_schema or None,
                )
            )

            # Capture tool.name in default arg to avoid late-binding closure issues.
            async def _exec(args: dict, *, _name: str = tool.name) -> str:
                result = await mcp_session.call_tool(_name, arguments=args)
                if result.isError:
                    # Check whether the gateway returned a need-credentials error.
                    # Gatana MCP embeds JSON: {"errorCode":"need-credentials","authorizeUrl":"..."}
                    error_texts = [c.text for c in result.content if hasattr(c, "text")]
                    for text in error_texts:
                        try:
                            data = json.loads(text)
                            if data.get("errorCode") == "need-credentials":
                                authorize_url = data.get("authorizeUrl", "")
                                logger.warning(
                                    "Tool %r requires secondary authorization (url=%s)",
                                    _name,
                                    authorize_url,
                                )
                                if not _auth_notified[0]:
                                    _auth_notified[0] = True
                                    await event_out.put(
                                        {
                                            "type": "mcp_auth_failed",
                                            "message": data.get("message", "Tool requires authorization"),
                                            "authorize_url": authorize_url,
                                        }
                                    )
                                    return (
                                        "TOOL_AUTH_REQUIRED. A link was sent to the user's phone. "
                                        "Briefly tell them to check their SMS and try again later. "
                                        "Do not elaborate or repeat."
                                    )
                                # This is an attempt to prevent a looping response from the agent
                                return "TOOL_UNAVAILABLE. Do not retry or mention this tool again."
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    return f"Tool error: {result.content}"
                texts = [c.text for c in result.content if hasattr(c, "text")]
                return "\n".join(texts) if texts else str(result.content)

            self.tool_map[tool.name] = traceable(name=tool.name, run_type="tool")(_exec)
            logger.debug("MCP tool registered: %s", tool.name)

        return declarations

    async def run(
        self,
        audio_in: asyncio.Queue[bytes | str | None],
        event_out: asyncio.Queue[dict],
    ) -> None:
        """Run the agent until audio_in receives None (end-of-stream sentinel).

        Args:
            audio_in:  Queue of items to send to Gemini:
                       - ``bytes``  — raw 16-bit PCM (16 kHz) audio chunk
                       - ``str``    — text to inject as a user turn (bypasses VAD;
                                     useful for testing and orchestrator integration)
                       - ``None``   — end-of-stream sentinel
            event_out: Queue of JSON-serialisable event dicts to forward to the
                       caller (audio_chunk, turn_complete, interrupted, …).
        """
        client = build_gemini_client()

        if self.mcp_gateway_url:
            self.mcp_status = asyncio.get_running_loop().create_future()
            mcp_timeout = int(os.getenv("MCP_TIMEOUT_SECONDS", "60"))
            try:
                async with streamablehttp_client(
                    self.mcp_gateway_url,
                    headers=self.mcp_headers,
                    timeout=timedelta(seconds=mcp_timeout),
                ) as (read, write, _):
                    async with ClientSession(read, write) as mcp_session:
                        await mcp_session.initialize()
                        declarations = await self._init_mcp_tools(mcp_session, event_out)
                        gemini_tools = [types.Tool(function_declarations=declarations)] if declarations else None
                        config = build_live_config(self.voice_name, self.system_prompt, tools=gemini_tools)
                        logger.info(
                            "MCP gateway connected (url=%s), %d tools registered",
                            self.mcp_gateway_url,
                            len(declarations),
                        )
                        if not self.mcp_status.done():
                            self.mcp_status.set_result(True)
                        async with client.aio.live.connect(model=self.model_id, config=config) as session:
                            await self._run_session(session, audio_in, event_out, self.tool_map)
                return
            except Exception as exc:
                logger.warning(
                    "MCP gateway connection failed (url=%s): %s — falling back to session without tools",
                    self.mcp_gateway_url,
                    exc,
                )
                if not self.mcp_status.done():
                    self.mcp_status.set_result(False)
                await event_out.put({"type": "mcp_auth_failed", "message": str(exc)})

        async with client.aio.live.connect(model=self.model_id, config=self._config) as session:
            await self._run_session(session, audio_in, event_out, self.tool_map)

    async def _run_session(
        self,
        session: object,
        audio_in: asyncio.Queue[bytes | str | None],
        event_out: asyncio.Queue[dict],
        dispatch_map: dict[str, object],
    ) -> None:
        """Drive a single Gemini Live session to completion."""
        logger.info(
            "Gemini Live session opened (model=%s, voice=%s)",
            self.model_id,
            self.voice_name,
        )

        t_first_audio_sent: float | None = None
        first_response_logged = False
        chunks_sent = 0

        async def _send_loop() -> None:
            nonlocal t_first_audio_sent, first_response_logged, chunks_sent
            try:
                while True:
                    chunk = await audio_in.get()
                    if chunk is None:
                        break  # end-of-stream sentinel
                    if isinstance(chunk, str):
                        # Text injection — sent as a complete user turn, bypasses VAD.
                        logger.info("Text injection: %r", chunk[:120])
                        await session.send_client_content(
                            turns=[
                                types.Content(
                                    role="user",
                                    parts=[types.Part(text=chunk)],
                                )
                            ],
                            turn_complete=True,
                        )
                        continue
                    chunks_sent += 1
                    if chunks_sent % 50 == 0:
                        logger.debug("send_loop: forwarded %d chunks", chunks_sent)
                    if t_first_audio_sent is None:
                        t_first_audio_sent = time.perf_counter()
                    await session.send_realtime_input(media=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("send_loop error")
            finally:
                logger.info("send_loop: exiting (total chunks: %d)", chunks_sent)
                try:
                    await session.send_realtime_input(audio_stream_end=True)
                except Exception:
                    pass

        async def _receive_loop() -> None:
            nonlocal t_first_audio_sent, first_response_logged
            turn = 0
            try:
                # session.receive() exhausts after each turn_complete in this SDK
                # version — outer while restarts it. for...else detects genuine
                # session closure (generator exhausts without turn_complete).
                while True:
                    async for response in session.receive():
                        # ── Tool calls ───────────────────────────────────
                        # Must respond or Gemini waits indefinitely.
                        if response.tool_call:
                            fn_responses = []
                            for fc in response.tool_call.function_calls:
                                logger.info(
                                    "Tool call: %s(%s) (turn=%d)",
                                    fc.name,
                                    dict(fc.args or {}),
                                    turn,
                                )
                                fn = dispatch_map.get(fc.name)
                                if fn is not None:
                                    kwargs = dict(fc.args) if fc.args else {}
                                    if asyncio.iscoroutinefunction(fn):
                                        result = await fn(kwargs)
                                    else:
                                        result = fn(**kwargs)
                                else:
                                    result = f"Unknown function: {fc.name}"
                                    logger.warning("Unknown tool called: %s", fc.name)
                                fn_responses.append(
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response={"result": result},
                                    )
                                )
                            await session.send_tool_response(function_responses=fn_responses)
                            continue

                        sc = response.server_content
                        if sc is None:
                            continue

                        if sc.interrupted:
                            logger.info("Gemini: barge-in (turn=%d)", turn)
                            t_first_audio_sent = None
                            first_response_logged = False
                            await event_out.put({"type": "interrupted"})
                            continue

                        if sc.model_turn:
                            for part in sc.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    if not first_response_logged and t_first_audio_sent is not None:
                                        lat_ms = (time.perf_counter() - t_first_audio_sent) * 1000
                                        logger.info("[LATENCY] Gemini first audio: %.0f ms", lat_ms)
                                        first_response_logged = True
                                    audio_b64 = base64.b64encode(part.inline_data.data).decode("ascii")
                                    _event_logger.debug(json.dumps({"type": "audio_chunk"}))
                                    await event_out.put({"type": "audio_chunk", "audio": audio_b64})

                        if sc.input_transcription and sc.input_transcription.text:
                            ev = {"type": "input_transcript", "text": sc.input_transcription.text}
                            _event_logger.debug(json.dumps(ev))
                            await event_out.put(ev)

                        if sc.output_transcription and sc.output_transcription.text:
                            ev = {"type": "output_transcript", "text": sc.output_transcription.text}
                            _event_logger.debug(json.dumps(ev))
                            await event_out.put(ev)

                        if sc.turn_complete:
                            logger.info("Gemini: turn %d complete", turn)
                            t_first_audio_sent = None
                            first_response_logged = False
                            _event_logger.debug(json.dumps({"type": "turn_complete"}))
                            await event_out.put({"type": "turn_complete"})
                            turn += 1
                            break  # restart session.receive() for next turn
                    else:
                        # for completed without break → session genuinely closed.
                        logger.warning("receive_loop: session closed after %d turns", turn)
                        break

            except asyncio.CancelledError:
                logger.debug("receive_loop: cancelled")
                raise
            except Exception:
                logger.exception("receive_loop error")
            finally:
                logger.info("receive_loop: exiting (completed %d turns)", turn)

        receive_task = asyncio.create_task(_receive_loop())
        try:
            await _send_loop()
        finally:
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass
