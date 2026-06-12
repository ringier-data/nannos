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
        raise RuntimeError(
            "GCP_KEY is not set — add the service account JSON blob to .env"
        )
    if not gcp_project:
        raise RuntimeError("GCP_PROJECT_ID is not set — add it to .env")

    try:
        from google.oauth2 import service_account as _sa  # noqa: PLC0415

        credentials = _sa.Credentials.from_service_account_info(
            json.loads(gcp_key),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Failed to parse GCP_KEY as service account JSON: {exc}"
        ) from exc

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
 """


# ── Tools ─────────────────────────────────────────────────────────────────────


# Maps tool name → callable for receive_loop dispatch.
_TOOL_MAP: dict[str, object] = {}


# ── Live config ───────────────────────────────────────────────────────────────


_NO_PROACTIVE_TOOLS_INSTRUCTION = (
    "\n\nCRITICAL: Your FIRST action must always be to speak a greeting to the user. "
    "Never call any tools before your first spoken response. "
    "After the greeting you may use tools freely."
)

_WRITE_TOOL_INSTRUCTION = (
    "\n\nFor any tool that creates, modifies, deletes, or sends data — such as creating "
    "issues, posting messages, committing code, or sending emails — you MUST first ask "
    "the user for explicit verbal confirmation. Say what you are about to do and ask "
    "'Shall I proceed?' Wait for a clear yes before calling the tool."
)

# Keywords that indicate a tool has write/mutate side-effects (fallback only).
_WRITE_KEYWORDS = frozenset(
    {
        "create",
        "update",
        "delete",
        "remove",
        "post",
        "write",
        "send",
        "push",
        "modify",
        "edit",
        "close",
        "merge",
        "publish",
        "commit",
        "open",
        "add",
        "insert",
        "patch",
        "put",
        "submit",
        "deploy",
        "release",
    }
)


def _is_write_tool(name: str, description: str) -> bool:
    """Return True if the tool name or description suggests a write/mutate operation."""
    text = (name + " " + description).lower().replace("_", " ").replace("-", " ")
    return any(kw in text.split() for kw in _WRITE_KEYWORDS)


# Results larger than this are stored in memory and replaced with a stub so they
# don't inflate the model's context window.
_LARGE_RESULT_THRESHOLD: int = 15 * 1024  # 15 KB

# ── Tool risk scoring ─────────────────────────────────────────────────────────

# Risk score threshold: >= this value → treat tool as write/mutate (require confirmation).
_WRITE_RISK_THRESHOLD: float = 0.4

# Module-level cache: tool_name → risk score. Persists across sessions within the process.
_TOOL_RISK_CACHE: dict[str, float] = {}

# Lightweight flash model used only for tool classification (separate from the Live session model).
_RISK_SCORER_MODEL: str = os.getenv("GEMINI_RISK_SCORER_MODEL", "gemini-2.5-flash")

_RISK_SCORING_SYSTEM_PROMPT = """\
You are a security analyst evaluating the risk level of AI agent tool calls.
Given a tool's name, description, and JSON schema, return a JSON object with exactly two keys:
  "score": number 0.0-1.0 — inherent risk when called with typical arguments.
    0.0-0.2: Safe    — read-only queries, status checks, information retrieval
    0.2-0.4: Low     — filtered reads, access to user's own data
    0.4-0.6: Moderate — reversible writes to user's own resources
    0.6-0.8: Elevated — writes to shared resources, external API calls, credential access
    0.8-1.0: Critical — destructive, irreversible, or security-sensitive operations
  "reasoning": string — one sentence explanation.
Return only the JSON object, no extra text.\
"""


async def _llm_score_tool_risk(
    client: genai.Client,
    name: str,
    description: str,
    input_schema: dict,
) -> float:
    """Use Gemini Flash to score a tool's write risk. Returns 0.0–1.0."""
    schema_str = (
        json.dumps(input_schema, indent=2) if input_schema else "No schema available"
    )
    user_prompt = (
        f"Tool name: {name}\n"
        f"Description: {description or 'No description available'}\n"
        f"Input schema:\n{schema_str}"
    )
    response = await client.aio.models.generate_content(
        model=_RISK_SCORER_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=_RISK_SCORING_SYSTEM_PROMPT,
            response_mime_type="application/json",
        ),
    )
    data = json.loads(response.text)
    return float(data["score"])


async def _score_tool_risk(
    client: genai.Client,
    name: str,
    description: str,
    input_schema: dict,
) -> float:
    """Return write-risk score for an MCP tool (0.0–1.0).

    Resolution order: module-level cache → LLM scoring → keyword fallback.
    """
    cached = _TOOL_RISK_CACHE.get(name)
    if cached is not None:
        return cached

    try:
        score = await _llm_score_tool_risk(client, name, description, input_schema)
        logger.debug("Tool %r LLM risk score: %.2f", name, score)
    except Exception:
        logger.warning(
            "LLM risk scoring failed for %r, using keyword fallback",
            name,
            exc_info=True,
        )
        score = 0.9 if _is_write_tool(name, description) else 0.1

    _TOOL_RISK_CACHE[name] = score
    return score


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
    if tools:
        prompt = prompt + _NO_PROACTIVE_TOOLS_INSTRUCTION + _WRITE_TOOL_INSTRUCTION
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
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=128000,
            sliding_window=types.SlidingWindow(target_tokens=32000),
        ),
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
        self,
        mcp_session: ClientSession,
        event_out: asyncio.Queue[dict],
        client: genai.Client,
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

        # Human-in-the-loop confirmation gate for write tools.
        # Maps tool_name → the exact args dict that was intercepted.
        # Only a second call with identical args is treated as confirmed;
        # a call with different args re-triggers the gate from scratch,
        # preventing a retry with different arguments from slipping through.
        _awaiting_confirmation: dict[str, dict] = {}

        # Per-session store for large tool results.
        # _result_counter wrapped in a list so _exec closures can increment it.
        _stored_results: dict[str, str] = {}
        _result_counter = [0]

        for tool in tools_result.tools:
            # If a tool filter was specified, skip tools not in the list.
            if self.mcp_tool_filter and tool.name not in self.mcp_tool_filter:
                logger.debug(
                    "MCP tool %r skipped (not in filter %s)",
                    tool.name,
                    self.mcp_tool_filter,
                )
                continue
            raw_schema = dict(tool.inputSchema or {})
            logger.debug("MCP tool schema for %r: %s", tool.name, raw_schema)
            risk_score = await _score_tool_risk(
                client, tool.name, tool.description or "", raw_schema
            )
            is_risky = risk_score >= _WRITE_RISK_THRESHOLD
            declarations.append(
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description or "",
                    parameters_json_schema=raw_schema or None,
                )
            )

            # Capture loop variables in default args to avoid late-binding closure issues.
            async def _exec(
                args: dict,
                *,
                _name: str = tool.name,
                _risky: bool = is_risky,
            ) -> str:
                if _risky:
                    confirmation_msg = (
                        f"CONFIRMATION_REQUIRED: Before calling {_name}, tell the user "
                        f"exactly what you are about to do and ask 'Shall I proceed?' "
                        f"Only call {_name} again once they have clearly said yes."
                    )
                    pending = _awaiting_confirmation.get(_name)
                    if pending is None:
                        # First call — gate it.
                        _awaiting_confirmation[_name] = args
                        logger.info(
                            "Risky tool %r intercepted — requesting confirmation", _name
                        )
                        return confirmation_msg
                    if args != pending:
                        # Different args — treat as a new attempt, re-gate.
                        _awaiting_confirmation[_name] = args
                        logger.info(
                            "Risky tool %r re-intercepted with different args", _name
                        )
                        return confirmation_msg
                    # Same args — user confirmed; execute and clear the gate.
                    del _awaiting_confirmation[_name]
                    logger.info("Tool %r executing after confirmation", _name)

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
                                            "message": data.get(
                                                "message", "Tool requires authorization"
                                            ),
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
                full_text = "\n".join(texts) if texts else str(result.content)
                if len(full_text) > _LARGE_RESULT_THRESHOLD:
                    result_id = f"result_{_result_counter[0]}"
                    _result_counter[0] += 1
                    _stored_results[result_id] = full_text
                    size_kb = len(full_text) / 1024
                    line_count = full_text.count("\n") + 1
                    preview = full_text[:200].replace("\n", " ")
                    logger.info(
                        "Large result from %r stored as %s (%.0fKB, %d lines)",
                        _name, result_id, size_kb, line_count,
                    )
                    return (
                        f"Result too large to include directly ({size_kb:.0f}KB, {line_count} lines). "
                        f"Stored as {result_id}. Preview: {preview!r}. "
                        f"Use search_stored_result(result_id='{result_id}', pattern='...') to search, "
                        f"or read_stored_result_range(result_id='{result_id}', start_line=1, end_line=50) to read."
                    )
                return full_text

            self.tool_map[tool.name] = traceable(name=tool.name, run_type="tool")(_exec)
            logger.info(
                "MCP tool registered: %s (write=%s, risk=%.2f)",
                tool.name,
                is_risky,
                risk_score,
            )

        # ── Local result-storage tools ────────────────────────────────────────
        # These are registered as FunctionDeclarations alongside the MCP tools
        # but are handled entirely in-process — no MCP round-trip, no risk scoring.

        async def search_stored_result(args: dict) -> str:
            result_id = args.get("result_id", "")
            pattern = args.get("pattern", "")
            text = _stored_results.get(result_id)
            if text is None:
                return f"No stored result with id '{result_id}'."
            lines = text.splitlines()
            matches = [(i + 1, line) for i, line in enumerate(lines) if pattern.lower() in line.lower()]
            if not matches:
                return f"No lines matching '{pattern}' in {result_id} ({len(lines)} lines total)."
            shown = matches[:50]
            result_lines = [f"L{n}: {line}" for n, line in shown]
            suffix = f"\n… ({len(matches) - 50} more matches not shown)" if len(matches) > 50 else ""
            return "\n".join(result_lines) + suffix

        async def read_stored_result_range(args: dict) -> str:
            result_id = args.get("result_id", "")
            start_line = int(args.get("start_line", 1))
            end_line = int(args.get("end_line", 50))
            text = _stored_results.get(result_id)
            if text is None:
                return f"No stored result with id '{result_id}'."
            lines = text.splitlines()
            total = len(lines)
            start = max(0, start_line - 1)
            end = min(total, end_line)
            chunk = "\n".join(f"L{start + i + 1}: {line}" for i, line in enumerate(lines[start:end]))
            return f"Lines {start_line}–{end} of {total} total:\n{chunk}"

        declarations.append(
            types.FunctionDeclaration(
                name="search_stored_result",
                description=(
                    "Search a large tool result that was too big to return directly. "
                    "Returns up to 50 matching lines with line numbers."
                ),
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "result_id": {"type": "string", "description": "ID returned in the stub, e.g. result_0"},
                        "pattern": {"type": "string", "description": "Case-insensitive text to search for"},
                    },
                    "required": ["result_id", "pattern"],
                },
            )
        )
        declarations.append(
            types.FunctionDeclaration(
                name="read_stored_result_range",
                description=(
                    "Read a line range from a large tool result that was too big to return directly."
                ),
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "result_id": {"type": "string", "description": "ID returned in the stub, e.g. result_0"},
                        "start_line": {"type": "integer", "description": "First line to read (1-indexed)"},
                        "end_line": {"type": "integer", "description": "Last line to read inclusive (1-indexed)"},
                    },
                    "required": ["result_id", "start_line", "end_line"],
                },
            )
        )
        self.tool_map["search_stored_result"] = search_stored_result
        self.tool_map["read_stored_result_range"] = read_stored_result_range
        logger.info("Local result-storage tools registered (search_stored_result, read_stored_result_range)")

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
                        declarations = await self._init_mcp_tools(
                            mcp_session, event_out, client
                        )
                        gemini_tools = (
                            [types.Tool(function_declarations=declarations)]
                            if declarations
                            else None
                        )
                        config = build_live_config(
                            self.voice_name, self.system_prompt, tools=gemini_tools
                        )
                        logger.info(
                            "MCP gateway connected (url=%s), %d tools registered",
                            self.mcp_gateway_url,
                            len(declarations),
                        )
                        if not self.mcp_status.done():
                            self.mcp_status.set_result(True)
                        async with client.aio.live.connect(
                            model=self.model_id, config=config
                        ) as session:
                            await self._run_session(
                                session, audio_in, event_out, self.tool_map
                            )
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

        async with client.aio.live.connect(
            model=self.model_id, config=self._config
        ) as session:
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
                    await session.send_realtime_input(
                        media=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
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

        _running_tools: set[str] = set()

        async def _dispatch_tool_call(call_id: str, name: str, args: dict) -> None:
            """Execute one tool call in the background and send the response.

            Runs as an asyncio.create_task so the receive loop is never blocked
            by slow MCP calls. Scheduling hints tell Gemini when to surface the
            result:
              INTERRUPT — confirmation prompts: speak immediately, interrupting
                          any in-progress audio.
              WHEN_IDLE — normal results: wait for a natural pause so the agent
                          can finish its current sentence first.
            """
            fn = dispatch_map.get(name)
            if fn is not None:
                try:
                    task = asyncio.ensure_future(fn(args))
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(task), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        logger.info(
                            "Tool %r still running after 1s — notifying model", name
                        )
                        await session.send_client_content(
                            turns=[
                                types.Content(
                                    role="user",
                                    parts=[
                                        types.Part(
                                            text=(
                                                f"repeat this in natural way: The tool '{name}' is still executing. "
                                                "Briefly let the user know you're working on it."
                                            )
                                        )
                                    ],
                                )
                            ],
                            turn_complete=True,
                        )
                        result = await task
                except Exception:
                    logger.exception("Tool %r raised an exception", name)
                    result = f"Tool error: {name} raised an exception"
            else:
                result = f"Unknown function: {name}"
                logger.warning("Unknown tool called: %s", name)

            is_confirmation = (
                isinstance(result, str) and "CONFIRMATION_REQUIRED" in result
            )
            scheduling = "INTERRUPT" if is_confirmation else "WHEN_IDLE"
            logger.info("Tool %r response ready (scheduling=%s)", name, scheduling)
            try:
                await session.send_tool_response(
                    function_responses=[
                        types.FunctionResponse(
                            id=call_id,
                            name=name,
                            response={"result": result},
                            scheduling=scheduling,
                        )
                    ]
                )
            finally:
                _running_tools.discard(name)

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
                        # Dispatch each call as a background task so the
                        # receive loop is never blocked by slow MCP calls.
                        if response.tool_call:
                            for fc in response.tool_call.function_calls:
                                if fc.name in _running_tools:
                                    logger.debug(
                                        "Tool %r still running — ignoring duplicate call (id=%s)",
                                        fc.name,
                                        fc.id,
                                    )
                                    continue
                                _running_tools.add(fc.name)
                                logger.info(
                                    "Tool call: %s(%s) (turn=%d)",
                                    fc.name,
                                    dict(fc.args or {}),
                                    turn,
                                )
                                asyncio.create_task(
                                    _dispatch_tool_call(
                                        fc.id,
                                        fc.name,
                                        dict(fc.args or {}),
                                    )
                                )
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
                                    if (
                                        not first_response_logged
                                        and t_first_audio_sent is not None
                                    ):
                                        lat_ms = (
                                            time.perf_counter() - t_first_audio_sent
                                        ) * 1000
                                        logger.info(
                                            "[LATENCY] Gemini first audio: %.0f ms",
                                            lat_ms,
                                        )
                                        first_response_logged = True
                                    audio_b64 = base64.b64encode(
                                        part.inline_data.data
                                    ).decode("ascii")
                                    _event_logger.debug(
                                        json.dumps({"type": "audio_chunk"})
                                    )
                                    await event_out.put(
                                        {"type": "audio_chunk", "audio": audio_b64}
                                    )

                        if sc.input_transcription and sc.input_transcription.text:
                            ev = {
                                "type": "input_transcript",
                                "text": sc.input_transcription.text,
                            }
                            _event_logger.debug(json.dumps(ev))
                            await event_out.put(ev)

                        if sc.output_transcription and sc.output_transcription.text:
                            ev = {
                                "type": "output_transcript",
                                "text": sc.output_transcription.text,
                            }
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
                        logger.warning(
                            "receive_loop: session closed after %d turns", turn
                        )
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
