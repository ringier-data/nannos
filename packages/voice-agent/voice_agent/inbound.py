"""Inbound call handling for voice-agent.

Flow:
  1.  POST /twilio/incoming   — Twilio webhook when a call arrives.
      a. Validate optional Twilio signature (TWILIO_VALIDATE_SIGNATURE=true).
      b. Resolve From → user via console backend.  Reject unknowns.
      c. List ≤5 activated sub-agents for DTMF menu.
      d. Check for resumable Gemini session per sub-agent (used in step 3).
      e. Store interim state in _INBOUND_PENDING[call_sid].
      f. Return TwiML <Gather> with the agent menu.

  2.  POST /twilio/incoming/menu  — Twilio DTMF callback.
      a. Read Digits from _INBOUND_PENDING state.
      b. Resolve selected sub-agent (or resume option).
      c. Optionally: second <Gather> "press 1 to resume previous session".
      d. Persist voice session to backend (tool-less — MCP not available inbound).
      e. Register full config in _INBOUND_PENDING so twilio_stream picks it up.
      f. Return TwiML <Connect><Stream>.

  3.  POST /twilio/incoming/memory  — optional second DTMF round for memory opt-in.

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
    # Pre-fetched resumable sessions keyed by sub_agent id (populated at call start)
    resumable_sessions: dict[int, VoiceSessionInfo] = field(default_factory=dict)
    # True if a resumable session exists for the selected agent
    has_resume_option: bool = False
    # Fields set after menu selection (step 2)
    selected_agent: SubAgentInfo | None = None
    use_session_memory: bool = False
    gemini_session_handle: str | None = None
    voice_session_id: str | None = None  # backend session record ID
    # Resolved config for twilio_stream (same shape as OutboundCallRequest)
    ready: bool = False


_INBOUND_PENDING: dict[str, InboundCallState] = {}


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


def _twiml_stream(stream_url: str) -> Response:
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="{stream_url}"/></Connect></Response>'
    )
    return Response(content=twiml, media_type="text/xml")


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
    the ``?call_sid=…`` carried by the menu/memory actions), honouring PUBLIC_URL
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
        return _twiml_reject("Sorry, your number is not registered in our system.")

    logger.info("Inbound call verified: user=%s (%s)", user.email, user.id)

    # Fetch available sub-agents
    try:
        sub_agents = await list_sub_agents_for_menu(user.id)
    except Exception as exc:
        logger.error("Failed to list sub-agents for user %s: %s", user.id, exc)
        sub_agents = []

    if not sub_agents:
        logger.warning("No activated sub-agents for user %s — rejecting", user.id)
        return _twiml_reject(
            "You don't have any active agents configured. "
            "Please set up at least one agent in the console and try again."
        )

    # Build digit map (1-based)
    digit_map: dict[str, int] = {str(i + 1): i for i in range(len(sub_agents))}

    # Pre-fetch resumable sessions for all agents in parallel so the menu can hint
    # which agents have a previous session and the /menu handler needs no extra call.
    resumable_results = await asyncio.gather(
        *[get_latest_resumable_session(user.id, agent.id) for agent in sub_agents],
        return_exceptions=True,
    )
    resumable_sessions: dict[int, VoiceSessionInfo] = {
        agent.id: result
        for agent, result in zip(sub_agents, resumable_results)
        if isinstance(result, VoiceSessionInfo) and result.gemini_session_handle
    }

    # Store interim state
    state = InboundCallState(
        call_sid=CallSid,
        user=user,
        from_number=from_number,
        sub_agents=sub_agents,
        digit_map=digit_map,
        resumable_sessions=resumable_sessions,
    )
    _INBOUND_PENDING[CallSid] = state

    # Build menu text — flag agents that have a resumable session
    menu_parts = ["Welcome to Nannos. Please select an agent."]
    for digit, idx in digit_map.items():
        agent = sub_agents[idx]
        suffix = (
            ", previous session available" if agent.id in resumable_sessions else ""
        )
        menu_parts.append(f"Press {digit} for {agent.name}{suffix}.")

    menu_text = " ".join(menu_parts)
    action = _callback_url(request, f"/twilio/incoming/menu?call_sid={CallSid}")
    return _twiml_gather(action, menu_text)


@inbound_router.post("/menu")
async def twilio_incoming_menu(
    request: Request,
    Digits: str = Form(default=""),
    CallSid: str = Form(default=""),
    call_sid: str | None = None,  # also accept via query param
) -> Response:
    """DTMF menu callback — resolve sub-agent and optionally ask about session memory."""
    if (rejection := await _reject_if_invalid_signature(request)) is not None:
        return rejection

    effective_sid = CallSid or call_sid or ""
    state = _INBOUND_PENDING.get(effective_sid)

    if state is None:
        logger.warning("No pending inbound state for CallSid=%s", effective_sid)
        return _twiml_reject("Sorry, your session has expired. Please call back.")

    digit = (Digits or "").strip()

    # ── First selection: which agent? ─────────────────────────────────────────
    if state.selected_agent is None:
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

        # Use the pre-fetched resumable session (checked at call start in twilio_incoming)
        resumable = state.resumable_sessions.get(agent.id)

        if resumable and resumable.gemini_session_handle:
            state.has_resume_option = True
            # Ask about session memory — round 2 of DTMF
            action = _callback_url(
                request, f"/twilio/incoming/memory?call_sid={effective_sid}"
            )
            say = (
                f"You selected {agent.name}. "
                "Press 1 to resume your previous session, "
                "or press 2 to start fresh."
            )
            return _twiml_gather(action, say)

        # No previous session — proceed directly to stream
        return await _finalize_and_stream(request, state, use_memory=False)

    # Shouldn't reach here after selection — but handle gracefully
    return _twiml_reject("Unexpected state. Please call back.")


@inbound_router.post("/memory")
async def twilio_incoming_memory(
    request: Request,
    Digits: str = Form(default=""),
    CallSid: str = Form(default=""),
    call_sid: str | None = None,
) -> Response:
    """Second DTMF round: does the caller want to resume a previous session?"""
    if (rejection := await _reject_if_invalid_signature(request)) is not None:
        return rejection

    effective_sid = CallSid or call_sid or ""
    state = _INBOUND_PENDING.get(effective_sid)

    if state is None or state.selected_agent is None:
        return _twiml_reject("Session expired. Please call back.")

    digit = (Digits or "").strip()
    if digit == "1":
        return await _finalize_and_stream(request, state, use_memory=True)
    elif digit == "2":
        return await _finalize_and_stream(request, state, use_memory=False)
    else:
        action = _callback_url(
            request, f"/twilio/incoming/memory?call_sid={effective_sid}"
        )
        say = "Invalid selection. Press 1 to resume, or 2 to start fresh."
        return _twiml_gather(action, say)


# ── Finalize helper ───────────────────────────────────────────────────────────


async def _finalize_and_stream(
    request: Request,
    state: InboundCallState,
    use_memory: bool,
) -> Response:
    """Resolve final config, persist session, and return <Connect><Stream> TwiML."""
    agent = state.selected_agent
    assert agent is not None

    gemini_handle: str | None = None
    if use_memory:
        # Reuse the session prefetched in twilio_incoming — no extra round-trip.
        resumable = state.resumable_sessions.get(agent.id)
        if resumable:
            gemini_handle = resumable.gemini_session_handle

    state.use_session_memory = use_memory
    state.gemini_session_handle = gemini_handle

    # Create the voice session record
    try:
        session = await create_voice_session(
            user_id=state.user.id,
            phone_number=state.from_number,
            sub_agent_id=agent.id,
            call_sid=state.call_sid,
            use_session_memory=use_memory,
        )
        state.voice_session_id = session.id if session else None
    except Exception as exc:
        logger.warning("Failed to create voice session record: %s", exc)
        state.voice_session_id = None

    # Mark ready — twilio_stream reads this state on the Media Streams "start" event.
    # state is already registered in _INBOUND_PENDING (from twilio_incoming).
    state.ready = True

    logger.info(
        "Inbound call ready: CallSid=%s agent=%s memory=%s handle=%s",
        state.call_sid,
        agent.name,
        use_memory,
        bool(gemini_handle),
    )

    host = request.headers.get("host", "localhost:8002")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    ws_scheme = "wss" if forwarded_proto == "https" else "ws"
    stream_url = f"{ws_scheme}://{host}/twilio/stream"
    return _twiml_stream(stream_url)


# ── Accessor used by twilio_transport ─────────────────────────────────────────


def pop_inbound_state(call_sid: str) -> InboundCallState | None:
    """Remove and return the inbound call state for call_sid, or None."""
    return _INBOUND_PENDING.pop(call_sid, None)


def build_inbound_init_config(state: InboundCallState) -> dict:
    """Build the init_config dict that _start_audio_session expects."""
    agent = state.selected_agent
    assert agent is not None
    return {
        "system_prompt": agent.system_prompt or DEFAULT_SYSTEM_PROMPT,
        "voice_name": agent.voice_name or "Kore",
        "mcp_tools": [],
        "access_token": None,
        "gemini_session_handle": state.gemini_session_handle,
        "voice_session_id": state.voice_session_id,
    }
