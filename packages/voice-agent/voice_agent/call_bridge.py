"""Shared call-initiation and Future bridge for voice-agent.

This module exists solely to break the circular import between a2a_agent.py
(which needs _CALL_FUTURES and make_outbound_call) and twilio_transport.py
(which imports VoiceAgent from a2a_agent.py).

Contents:
  _CALL_FUTURES        — dict[call_sid, asyncio.Future] resolved when a Twilio
                         Media Stream session ends.  Populated by VoiceAgent
                         before awaiting, resolved by twilio_stream's finally block.
  make_outbound_call() — thin wrapper around the Twilio REST API.
"""

from __future__ import annotations

import asyncio
import logging
import os

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class OutboundCallRequest(BaseModel):
    """Config for an outbound Twilio call, including agent personality.

    Stored in ``_PENDING_CALLS[call_sid]`` after the call is initiated so the
    Twilio Media Stream WebSocket handler can pick it up when the call connects.

    All personality fields (system_prompt, voice_name, mcp_tools) are resolved
    from the sub-agent config by ``VoiceAgent._fetch_sub_agent_config()``.
    """

    to: str  # E.164 phone number, e.g. "+41791234567"
    system_prompt: str | None = None  # From sub-agent config_version.system_prompt
    voice_name: str | None = None  # Gemini voice (Kore, Puck, Aoede, …)
    # MCP tool names from sub-agent config — wired into GeminiLiveAgent (next step)
    mcp_tools: list[str] = []
    # Human messages to inject into the Gemini Live session after the call connects.
    # Sent as complete user turns via session.send_client_content() before audio streaming.
    context_messages: list[str] = []


# Global registry: call_sid → OutboundCallRequest.
# Populated by VoiceAgent._stream_phone_call() after make_outbound_call() returns,
# consumed (and removed) by the Twilio Media Stream WebSocket on the "start" event.
_PENDING_CALLS: dict[str, OutboundCallRequest] = {}

# Maps call_sid → asyncio.Future that resolves when the Twilio stream ends.
# Set by VoiceAgent._stream_phone_call() before awaiting, resolved by
# twilio_stream's finally block in twilio_transport.py.
_CALL_FUTURES: dict[str, asyncio.Future] = {}


def make_outbound_call(to_number: str, public_url: str) -> str:
    """Use the Twilio Programmable Voice REST API to dial ``to_number``.

    When the callee answers, Twilio fetches TwiML from POST /twilio/voice on
    this server, which responds with a <Connect><Stream> instruction that
    bridges the call to GeminiLiveAgent.

    Required env vars:
        TWILIO_ACCOUNT_SID  — Twilio account SID (ACxxxxxxxx…)
        TWILIO_AUTH_TOKEN   — Twilio auth token
        TWILIO_PHONE_NUMBER — Twilio "from" number in E.164 format

    Optional env vars:
        TWILIO_REGION       — e.g. ``'ie1'`` for Ireland
        TWILIO_EDGE         — e.g. ``'dublin'`` for Ireland
    """
    from twilio.rest import Client  # noqa: PLC0415 — lazy import

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    api_secret = os.environ["TWILIO_API_SECRET"]
    api_key = os.environ["TWILIO_API_KEY"]
    from_number = os.environ["TWILIO_PHONE_NUMBER"]
    twiml_url = f"{public_url.rstrip('/')}/twilio/voice"

    # Region/edge are optional — only pass when explicitly configured
    region = os.getenv("TWILIO_REGION") or None
    edge = os.getenv("TWILIO_EDGE") or None

    kwargs: dict = {}
    if region:
        kwargs["region"] = region
    if edge:
        kwargs["edge"] = edge

    client = Client(api_key, api_secret, account_sid, **kwargs)
    call = client.calls.create(to=to_number, from_=from_number, url=twiml_url)
    logger.info("Outbound call initiated to=%s call_sid=%s", to_number, call.sid)
    return call.sid
