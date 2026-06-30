"""Inbound call handling for voice-agent.

Flow:
  1.  POST /twilio/incoming   — Twilio webhook when a call arrives.
      a. Validate optional Twilio signature (TWILIO_VALIDATE_SIGNATURE=true).
      b. Resolve From → user via console backend.  Reject unknowns.
      c. List ≤5 activated sub-agents.
      d. Store interim state in _INBOUND_PENDING[call_sid].
      e. If the user has a resumable session (<1h, any agent), offer to resume it
         FIRST via <Gather> → /twilio/incoming/resume.  Otherwise show the agent
         picker directly via <Gather> → /twilio/incoming/menu.

  2.  POST /twilio/incoming/resume  — Twilio DTMF callback (only when a resumable
      session exists).
      a. Press 1 → resume: reuse the prior session's agent + Gemini handle, skip
         the picker, and stream.
      b. Press 2 → declined: fall back to the agent picker.

  3.  POST /twilio/incoming/menu  — agent picker DTMF callback.
      a. Resolve the selected sub-agent (fresh session — no resume handle).
      b. Persist voice session to backend, register config, stream.

Session memory is always retained: every call's Gemini resumption handle is saved
so a callback within the resume window can continue the conversation.  MCP tools are
enabled when the backend can mint a user-scoped gateway token from the caller's stored
offline token (saved at console login); otherwise the call falls back to tool-less.

The shared _INBOUND_PENDING dict is consumed by twilio_transport.twilio_stream on
the "start" Media Streams event, same as _PENDING_CALLS for outbound.

Twilio signature validation:
  Set TWILIO_VALIDATE_SIGNATURE=true and ensure TWILIO_AUTH_TOKEN is set.
  Disabled by default so ngrok testing works without extra headers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from voice_agent.agent import SYSTEM_PROMPT as DEFAULT_SYSTEM_PROMPT
from voice_agent.console_client import (
    SubAgentInfo,
    UserInfo,
    VoiceSessionInfo,
    create_voice_session,
    get_latest_resumable_session,
    get_user_mcp_token,
    list_sub_agents_for_menu,
    lookup_user_by_phone,
)

logger = logging.getLogger(__name__)

inbound_router = APIRouter(prefix="/twilio/incoming", tags=["twilio-inbound"])
_VALIDATE_SIG = os.getenv("TWILIO_VALIDATE_SIGNATURE", "false").lower() == "true"
_PUBLIC_URL = os.getenv("PUBLIC_URL", os.getenv("VOICE_AGENT_BASE_URL", ""))

# ── Shared inbound state dict ─────────────────────────────────────────────────
# Keyed by call_sid.  Populated by /twilio/incoming and /twilio/incoming/menu.
# Consumed (and removed) by twilio_transport.twilio_stream on the "start" event.


@dataclass
class InboundCallState:
    call_sid: str
    user: UserInfo
    from_number: str
    sub_agents: list[SubAgentInfo]
    # Maps DTMF digit ("1"…"5") → sub_agent index in sub_agents
    digit_map: dict[str, int] = field(default_factory=dict)
    # Most recent resumable session (<1h, any agent), offered before the picker.
    resume_candidate: VoiceSessionInfo | None = None
    # Fields set once an agent is resolved (resume or picker)
    selected_agent: SubAgentInfo | None = None
    gemini_session_handle: str | None = None  # set only when resuming
    voice_session_id: str | None = None  # backend session record ID
    # Pre-built MCP gateway auth headers (user-scoped token minted by the backend).
    mcp_headers: dict[str, str] | None = None
    # Resolved config for twilio_stream (same shape as OutboundCallRequest)
    ready: bool = False
    created_at: float = field(default_factory=time.monotonic)


_INBOUND_PENDING: dict[str, InboundCallState] = {}
_INBOUND_TTL_SECONDS = 300  # 5 min — enough for any menu interaction


def _evict_expired_inbound_state() -> None:
    now = time.monotonic()
    expired = [
        sid
        for sid, s in _INBOUND_PENDING.items()
        if now - s.created_at > _INBOUND_TTL_SECONDS
    ]
    for sid in expired:
        logger.info("Evicting expired inbound state for CallSid=%s", sid)
        _INBOUND_PENDING.pop(sid, None)


# ── TwiML helpers ─────────────────────────────────────────────────────────────


def _twiml_reject(message: str) -> Response:
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{message}</Say><Hangup/></Response>"
    )
    return Response(content=twiml, media_type="text/xml")


def _twiml_gather(action_url: str, say_text: str, num_digits: int = 1) -> Response:
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather numDigits="{num_digits}" action="{action_url}" method="POST">'
        f"<Say>{say_text}</Say>"
        "</Gather>"
        "<Say>We didn't receive your selection. Please call back and try again.</Say>"
        "<Hangup/>"
        "</Response>"
    )
    return Response(content=twiml, media_type="text/xml")


def _twiml_stream(
    stream_url: str,
    say_text: str | None = None,
) -> Response:
    """Build <Connect><Stream> TwiML, optionally preceded by a spoken message
    and/or a media file.

    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    if say_text:
        parts.append(f"<Say>{say_text}</Say>")
    parts.append(f'<Connect><Stream url="{stream_url}"/></Connect>')
    parts.append("</Response>")
    return Response(content="".join(parts), media_type="text/xml")


def _normalize_e164(number: str) -> str:
    """Strip spaces/dashes, ensure leading +."""
    cleaned = re.sub(r"[\s\-().]", "", number)
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


# ── Twilio signature validation (optional) ────────────────────────────────────


def _validate_twilio_signature(request: Request, url: str, form_data: dict) -> bool:
    """Validate X-Twilio-Signature using twilio.request_validator."""
    if not _VALIDATE_SIG:
        return True
    try:
        from twilio.request_validator import RequestValidator  # noqa: PLC0415

        auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        validator = RequestValidator(auth_token)
        signature = request.headers.get("X-Twilio-Signature", "")
        result = validator.validate(url, form_data, signature)
        return result
    except Exception as exc:
        logger.warning("Signature validation error: %s", exc)
        return False


def _callback_url(request: Request, path: str) -> str:
    """Build an absolute callback URL, honouring x-forwarded-proto."""
    public = _PUBLIC_URL.rstrip("/")
    if public:
        return f"{public}{path}"
    # Derive from headers set by ngrok / load balancer
    host = request.headers.get("host", "localhost:8002")
    proto = request.headers.get("x-forwarded-proto", "https")
    return f"{proto}://{host}{path}"


async def _reject_if_invalid_signature(request: Request) -> Response | None:
    """Validate X-Twilio-Signature for an inbound callback.

    Reconstructs the exact public URL Twilio signed (path + query string, e.g.
    the ``?call_sid=…`` carried by the resume/menu actions), honouring PUBLIC_URL
    and proxy headers. Returns a reject Response on failure, or None when valid
    (or when validation is disabled).
    """
    form = dict(await request.form())
    path = request.url.path
    if request.url.query:
        path += f"?{request.url.query}"
    url = _callback_url(request, path)
    if not _validate_twilio_signature(request, url, form):
        logger.warning(
            "Invalid Twilio signature on %s from %s", request.url.path, request.client
        )
        return _twiml_reject("This request could not be authenticated.")
    return None


def _agent_by_id(
    state: InboundCallState, sub_agent_id: int | None
) -> SubAgentInfo | None:
    """Return the sub-agent matching sub_agent_id from the call's agent list."""
    return next((a for a in state.sub_agents if a.id == sub_agent_id), None)


def _agent_menu_gather(request: Request, state: InboundCallState) -> Response:
    """Build the agent-picker <Gather> for this call's sub-agents."""
    menu_parts = ["Welcome to Nannos. Please select an agent."]
    for digit, idx in state.digit_map.items():
        menu_parts.append(f"Press {digit} for {state.sub_agents[idx].name}.")
    action = _callback_url(request, f"/twilio/incoming/menu?call_sid={state.call_sid}")
    return _twiml_gather(action, " ".join(menu_parts))


# ── Endpoints ─────────────────────────────────────────────────────────────────


@inbound_router.post("")
async def twilio_incoming(
    request: Request,
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
) -> Response:
    """Twilio webhook called when an inbound call arrives.

    Configure your Twilio number: Voice webhook → POST {PUBLIC_URL}/twilio/incoming
    """
    if (rejection := await _reject_if_invalid_signature(request)) is not None:
        return rejection

    _evict_expired_inbound_state()
    from_number = _normalize_e164(From)
    logger.info("Inbound call: CallSid=%s From=%s To=%s", CallSid, from_number, To)

    # Verify caller is a registered user
    try:
        user = await lookup_user_by_phone(from_number)
    except Exception as exc:
        logger.error("Phone lookup failed: %s", exc)
        return _twiml_reject(
            "Sorry, we could not verify your number. Please try again later or contact administrator."
        )

    if user is None:
        logger.info("Inbound call from unregistered number: %s", from_number)
        return _twiml_reject(
            "Sorry, your number is not registered in our system. Please log-in initially via the browser."
        )

    logger.info("Inbound call verified: user=%s (%s)", user.email, user.id)

    # Fetch sub-agents and latest resumable session in parallel — both depend only
    # on user.id and are independent backend round-trips.
    sub_agents_result, resumable = await asyncio.gather(
        list_sub_agents_for_menu(user.id),
        get_latest_resumable_session(user.id),
        return_exceptions=True,
    )

    if isinstance(sub_agents_result, BaseException):
        logger.error("Failed to list sub-agents for user %s: %s", user.id, sub_agents_result)
        sub_agents_result = []
    if isinstance(resumable, BaseException):
        logger.warning("Failed to fetch resumable session: %s", resumable)
        resumable = None

    sub_agents: list = sub_agents_result

    if not sub_agents:
        logger.warning("No activated sub-agents for user %s — rejecting", user.id)
        return _twiml_reject(
            "You don't have any active agents configured. "
            "Please set up at least one agent in the console and try again."
        )

    # Build digit map (1-based)
    digit_map: dict[str, int] = {str(i + 1): i for i in range(len(sub_agents))}

    # Store interim state
    state = InboundCallState(
        call_sid=CallSid,
        user=user,
        from_number=from_number,
        sub_agents=sub_agents,
        digit_map=digit_map,
    )
    _INBOUND_PENDING[CallSid] = state

    # Offer to resume the most recent session (<1h, any agent) BEFORE the picker.
    if resumable and resumable.gemini_session_handle:
        agent = _agent_by_id(state, resumable.sub_agent_id)
        if agent is not None:
            state.resume_candidate = resumable
            action = _callback_url(
                request, f"/twilio/incoming/resume?call_sid={CallSid}"
            )
            say = (
                f"Welcome back to Nannos. Press 1 to resume your previous "
                f"conversation with {agent.name}, or press 2 to choose an agent."
            )
            return _twiml_gather(action, say)

    return _agent_menu_gather(request, state)


@inbound_router.post("/resume")
async def twilio_incoming_resume(
    request: Request,
    Digits: str = Form(default=""),
    CallSid: str = Form(default=""),
    call_sid: str | None = None,
) -> Response:
    """Resume opt-in (shown before the picker when a resumable session exists)."""
    if (rejection := await _reject_if_invalid_signature(request)) is not None:
        return rejection

    effective_sid = CallSid or call_sid or ""
    state = _INBOUND_PENDING.get(effective_sid)

    if state is None or state.resume_candidate is None:
        return _twiml_reject("Sorry, your session has expired. Please call back.")

    digit = (Digits or "").strip()

    if digit == "1":
        # Resume: reuse the prior session's agent + Gemini handle, skip the picker.
        agent = _agent_by_id(state, state.resume_candidate.sub_agent_id)
        if agent is None:
            # Agent is no longer available — fall back to the picker.
            return _agent_menu_gather(request, state)
        state.selected_agent = agent
        state.gemini_session_handle = state.resume_candidate.gemini_session_handle
        logger.info(
            "CallSid=%s resuming session with agent %s (id=%s)",
            effective_sid,
            agent.name,
            agent.id,
        )
        return await _finalize_and_stream(request, state)

    if digit == "2":
        # Declined — fall back to the sub-agent picker.
        return _agent_menu_gather(request, state)

    action = _callback_url(request, f"/twilio/incoming/resume?call_sid={effective_sid}")
    say = "Invalid selection. Press 1 to resume, or 2 to choose an agent."
    return _twiml_gather(action, say)


@inbound_router.post("/menu")
async def twilio_incoming_menu(
    request: Request,
    Digits: str = Form(default=""),
    CallSid: str = Form(default=""),
    call_sid: str | None = None,  # also accept via query param
) -> Response:
    """Agent-picker DTMF callback — resolve the selected sub-agent and stream."""
    if (rejection := await _reject_if_invalid_signature(request)) is not None:
        return rejection

    effective_sid = CallSid or call_sid or ""
    state = _INBOUND_PENDING.get(effective_sid)

    if state is None:
        logger.warning("No pending inbound state for CallSid=%s", effective_sid)
        return _twiml_reject("Sorry, your session has expired. Please call back.")

    digit = (Digits or "").strip()
    idx = state.digit_map.get(digit)
    if idx is None:
        logger.info("Invalid digit %r for CallSid=%s", digit, effective_sid)
        action = _callback_url(
            request, f"/twilio/incoming/menu?call_sid={effective_sid}"
        )
        menu_parts = ["Invalid selection. Please try again."]
        for d, i in state.digit_map.items():
            menu_parts.append(f"Press {d} for {state.sub_agents[i].name}.")
        return _twiml_gather(action, " ".join(menu_parts))

    agent = state.sub_agents[idx]
    state.selected_agent = agent
    logger.info(
        "CallSid=%s selected agent: %s (id=%s)", effective_sid, agent.name, agent.id
    )
    # Fresh session — no resume handle. The handle is still saved during the call.
    return await _finalize_and_stream(request, state)


# ── Finalize helper ───────────────────────────────────────────────────────────


async def _finalize_and_stream(
    request: Request,
    state: InboundCallState,
) -> Response:
    """Persist the session and return <Connect><Stream> TwiML.

    Session memory is always retained: the call's Gemini resumption handle is saved
    so a callback within the resume window can continue. ``state.gemini_session_handle``
    is set only when the caller chose to resume a previous session.
    """
    agent = state.selected_agent
    assert agent is not None

    # Mint a user-scoped MCP gateway token from the caller's stored offline token.
    # None when the user never granted consent (or on error) — call stays tool-less.
    if agent.mcp_tools:
        token = await get_user_mcp_token(state.user.id)
        if token:
            state.mcp_headers = {"Authorization": f"Bearer {token}"}
            logger.info("MCP token obtained for inbound call (user=%s)", state.user.id)
        else:
            logger.info(
                "No MCP token for user %s — inbound call will be tool-less",
                state.user.id,
            )

    # Create the voice session record (memory always on).
    logger.info(
        "Creating voice session: user=%s agent=%s call_sid=%s",
        state.user.id,
        agent.id,
        state.call_sid,
    )
    try:
        session = await create_voice_session(
            user_id=state.user.id,
            phone_number=state.from_number,
            sub_agent_id=agent.id,
            call_sid=state.call_sid,
            use_session_memory=True,
        )
        if session:
            logger.info("Voice session created: id=%s", session.id)
        else:
            logger.error(
                "Voice session creation returned None: user=%s agent=%s call_sid=%s",
                state.user.id,
                agent.id,
                state.call_sid,
            )
        state.voice_session_id = session.id if session else None
    except Exception as exc:
        logger.error("Failed to create voice session record: %s", exc)
        state.voice_session_id = None

    # Mark ready — twilio_stream reads this state on the Media Streams "start" event.
    # state is already registered in _INBOUND_PENDING (from twilio_incoming).
    state.ready = True

    logger.info(
        "Inbound call ready: CallSid=%s agent=%s resuming=%s",
        state.call_sid,
        agent.name,
        bool(state.gemini_session_handle),
    )

    host = request.headers.get("host", "localhost:8002")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    ws_scheme = "wss" if forwarded_proto == "https" else "ws"
    stream_url = f"{ws_scheme}://{host}/twilio/stream"

    # Spoken hand-off so the caller isn't met with silence while the Gemini
    # session spins up.
    say_text = (
        "Thank you. Connecting you to your agent now. "
        "This may take up to a minute, please hold."
    )
    return _twiml_stream(
        stream_url,
        say_text=say_text,
    )


# ── Accessor used by twilio_transport ─────────────────────────────────────────


def pop_inbound_state(call_sid: str) -> InboundCallState | None:
    """Remove and return the inbound call state for call_sid, or None if missing/expired."""
    state = _INBOUND_PENDING.pop(call_sid, None)
    if state is not None and time.monotonic() - state.created_at > _INBOUND_TTL_SECONDS:
        logger.warning(
            "Inbound state for CallSid=%s expired before stream start, discarding",
            call_sid,
        )
        return None
    return state


def build_inbound_init_config(state: InboundCallState) -> dict:
    """Build the init_config dict that _start_audio_session expects."""
    agent = state.selected_agent
    assert agent is not None
    # Tools are enabled only when an MCP token was minted (state.mcp_headers set).
    mcp_tools = agent.mcp_tools if state.mcp_headers else []
    return {
        "system_prompt": agent.system_prompt or DEFAULT_SYSTEM_PROMPT,
        "voice_name": agent.voice_name or "Kore",
        "mcp_tools": mcp_tools,
        "access_token": None,
        "mcp_headers": state.mcp_headers,
        "gemini_session_handle": state.gemini_session_handle,
        "voice_session_id": state.voice_session_id,
    }
