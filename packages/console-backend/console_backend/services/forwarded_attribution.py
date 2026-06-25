"""Caller request-context forwarded on console MCP requests.

The orchestrator stamps the caller's attribution (user_sub, conversation_id, sub_agent_id, …) on
every console MCP request as the dedicated ``x-nannos-context`` header — read per request from its
attribution ContextVars (see the orchestrator's ``discovery._attribution_http_client_factory``).
Console-backend MCP tools read it here rather than taking conversation_id as a model-visible tool
param, so the value is system-injected, deterministic, and out of the tool schema. Two uses:
  - forward it onto a gateway sub-call the tool makes (``console_web_search``) so spend bills to the
    right conversation instead of "Direct API Calls";
  - use a field as request context (``console_create_bug_report``'s conversation_id).

The header name + (de)serialization live in ringier_a2a_sdk (the shared layer both ends depend on),
so the orchestrator and console can't drift. ``user_sub`` from the header is NOT authoritative —
endpoints that bill must override it with the authenticated identity; conversation_id / sub_agent_id
are grouping context, safe to trust.
"""

from fastapi import Request
from ringier_a2a_sdk.cost_tracking.attribution import NANNOS_CONTEXT_HEADER, parse_context_header


def forwarded_attribution(request: Request) -> dict:
    """Caller attribution forwarded on the MCP request, or ``{}`` when absent/malformed."""
    return parse_context_header(request.headers.get(NANNOS_CONTEXT_HEADER))


def forwarded_conversation_id(request: Request) -> str | None:
    """The conversation_id the orchestrator stamped on the MCP request, if any."""
    cid = forwarded_attribution(request).get("conversation_id")
    return cid if isinstance(cid, str) and cid else None
