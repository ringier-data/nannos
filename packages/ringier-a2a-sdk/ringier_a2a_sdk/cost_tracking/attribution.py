"""Request-scoped cost attribution carried to the Model Gateway.

`create_model` builds gateway clients that are cached and shared across requests,
so attribution can't be baked in at construction. Instead we set these ContextVars
at the request boundary (and in `create_runnable_config`), and a per-request httpx
event hook stamps them onto the `x-litellm-spend-logs-metadata` header on every
outbound call. The proxy's CustomLogger reads them back (verified correct under
50-way concurrency on a shared client).

These ContextVars live in ringier-a2a-sdk — the lowest shared layer (agent-common and
the apps all depend on the SDK, never the reverse). Both the setters (the SDK's request
middleware / create_runnable_config, the orchestrator executor) and the reader (the http
hook here) reach them without any upward dependency on agent-common. This is the single
canonical home; import it directly from `ringier_a2a_sdk.cost_tracking.attribution`.
"""

import contextlib
import contextvars
import json
import logging

import httpx

from ..utils.http_pool import LazyClient

logger = logging.getLogger(__name__)

# Canonical attribution ContextVars. ringier-a2a-sdk sets these per request.
current_user_sub: contextvars.ContextVar = contextvars.ContextVar("nannos_user_sub", default=None)
current_conversation_id: contextvars.ContextVar = contextvars.ContextVar("nannos_conversation_id", default=None)
current_sub_agent_id: contextvars.ContextVar = contextvars.ContextVar("nannos_sub_agent_id", default=None)
current_scheduled_job_id: contextvars.ContextVar = contextvars.ContextVar("nannos_scheduled_job_id", default=None)
current_sub_agent_config_version_id: contextvars.ContextVar = contextvars.ContextVar(
    "nannos_sub_agent_config_version_id", default=None
)
current_catalog_id: contextvars.ContextVar = contextvars.ContextVar("nannos_catalog_id", default=None)
# The installation (tenant) the inbound request came from — e.g. the Slack/GChat botName the
# bot client stamps on its A2A message metadata. Carried as request *context* so console MCP
# tools can scope delivery channels to the calling installation (see context_header below).
current_installation: contextvars.ContextVar = contextvars.ContextVar("nannos_installation", default=None)

_FIELDS = {
    "user_sub": current_user_sub,
    "conversation_id": current_conversation_id,
    "sub_agent_id": current_sub_agent_id,
    "scheduled_job_id": current_scheduled_job_id,
    "sub_agent_config_version_id": current_sub_agent_config_version_id,
    "catalog_id": current_catalog_id,
    "installation": current_installation,
}

_HEADER = "x-litellm-spend-logs-metadata"

# Inter-service request-context header for the orchestrator → console-backend MCP hop. Distinct
# from the gateway billing header (_HEADER): it carries the same attribution fields as *context* a
# console MCP tool can act on (conversation_id for a bug report, or to forward onto a gateway
# sub-call) — without overloading the gateway's spend-logs header for non-gateway data.
NANNOS_CONTEXT_HEADER = "x-nannos-context"


def parse_attribution_tags(tags: list[str] | None) -> dict:
    """Extract attribution fields from LangGraph cost-tracking tags.

    Parses the ``user_sub:``/``conversation:``/``sub_agent:``/
    ``sub_agent_config_version:``/``scheduled_job:`` scheme produced by
    ``create_runnable_config`` and ``LocalA2ARunnable.extend_config_for_subagent``.
    Integer fields that fail to parse are dropped (logged at DEBUG) rather than
    raising. The returned dict is keyed by the attribution field names in
    ``_FIELDS``, so it feeds ``set_attribution`` / ``attribution_scope`` directly.

    Single source of truth for the tag scheme: the gateway attribution middleware
    (agent-common) and the ``CostTrackingCallback`` here both consume it, so the
    prefix set and int-parsing behaviour can never drift between call paths.
    """
    if not tags:
        return {}
    fields: dict = {}
    for tag in tags:
        if tag.startswith("user_sub:"):
            fields["user_sub"] = tag.split(":", 1)[1]
        elif tag.startswith("conversation:"):
            fields["conversation_id"] = tag.split(":", 1)[1]
        elif tag.startswith("sub_agent_config_version:"):
            try:
                fields["sub_agent_config_version_id"] = int(tag.split(":", 1)[1])
            except ValueError:
                logger.debug("Could not parse sub_agent_config_version id from tag %r", tag)
        elif tag.startswith("sub_agent:"):
            try:
                fields["sub_agent_id"] = int(tag.split(":", 1)[1])
            except ValueError:
                logger.debug("Could not parse sub_agent id from tag %r", tag)
        elif tag.startswith("scheduled_job:"):
            try:
                fields["scheduled_job_id"] = int(tag.split(":", 1)[1])
            except ValueError:
                logger.debug("Could not parse scheduled_job id from tag %r", tag)
    return fields


def current_attribution() -> dict:
    """Snapshot the non-empty attribution fields from the current context."""
    return {name: var.get() for name, var in _FIELDS.items() if var.get() is not None}


def _merged_attribution(overrides: dict) -> dict:
    """Current attribution ContextVars merged with explicit overrides (overrides win; None and
    unknown keys ignored). Shared by the header builders below."""
    attrib = current_attribution()
    for name, value in overrides.items():
        if name in _FIELDS and value is not None:
            attrib[name] = value
    return attrib


def set_attribution(**fields) -> None:
    """Set attribution ContextVars (ignores unknown keys / None values)."""
    for name, value in fields.items():
        var = _FIELDS.get(name)
        if var is not None and value is not None:
            var.set(value)


@contextlib.contextmanager
def attribution_scope(**fields):
    """Set attribution for the duration of a block, then restore prior values."""
    tokens = []
    try:
        for name, value in fields.items():
            var = _FIELDS.get(name)
            if var is not None and value is not None:
                tokens.append((var, var.set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def attribution_header(**overrides) -> dict[str, str]:
    """Build the `x-litellm-spend-logs-metadata` header for a manually-constructed
    request (one that doesn't go through the hooked client below).

    Merges the current attribution ContextVars with explicit overrides (overrides win;
    None values ignored), so callers stamp the full attribution set — user_sub,
    conversation_id, sub_agent_id, scheduled_job_id, … — not an ad-hoc subset. Returns
    {} when there's nothing to stamp, so callers can `headers.update(attribution_header())`.

    This is the single place the header name and field shape live; the embeddings adapter
    and console-backend's gateway_chat use it instead of hand-rolling the header.
    """
    attrib = _merged_attribution(overrides)
    return {_HEADER: json.dumps(attrib)} if attrib else {}


def context_header(**overrides) -> dict[str, str]:
    """Build the ``x-nannos-context`` header carrying the current attribution as request context
    for a downstream service (the orchestrator → console-backend MCP hop).

    Unlike ``attribution_header`` (which feeds a virtual-key gateway call and so must carry
    ``user_sub``), this hop is independently authenticated by the caller's token — the downstream
    service derives identity from that token, so ``user_sub`` is omitted here as redundant. Only the
    context the downstream can't derive (conversation_id, sub_agent_id, …) is sent. Returns {} when
    there's nothing to stamp; read back with ``parse_context_header``."""
    attrib = _merged_attribution(overrides)
    attrib.pop("user_sub", None)
    return {NANNOS_CONTEXT_HEADER: json.dumps(attrib)} if attrib else {}


def parse_context_header(raw: str | None) -> dict:
    """Inverse of ``context_header``: parse a received ``x-nannos-context`` value into an
    attribution dict, or {} when absent/malformed. Framework-agnostic (takes the raw header
    string) so any web layer can wrap it."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


async def _stamp_metadata(request) -> None:
    """httpx request event hook: inject attribution as the spend-logs header."""
    attrib = current_attribution()
    if attrib:
        request.headers[_HEADER] = json.dumps(attrib)


_shared_http_client = LazyClient(lambda: httpx.AsyncClient(event_hooks={"request": [_stamp_metadata]}, timeout=600.0))


def build_attribution_http_client():
    """A process-wide shared httpx.AsyncClient whose hook stamps per-request attribution.

    Safe to reuse across requests: the hook reads ContextVars at send time, which
    are isolated per asyncio task (spike check 4b). `asyncio.to_thread` copies the
    context so the existing sandbox path stays correct (spike check 4c); only a raw
    `loop.run_in_executor` would drop them.

    Returns a single lazily-created client rather than a new one per call: create_model /
    create_embeddings run on uncached per-request/per-sub-agent paths, so constructing a
    fresh AsyncClient (each owning a connection pool, timeout=600s) every time leaked an
    unclosed client per build. One shared client pools connections to the gateway and lives
    for the process lifetime — nothing closes it, which is correct here (it is the gateway
    client, not a per-request resource). httpx clients are loop-agnostic until first use, so
    a module-level singleton is safe across the app's single event loop.
    """
    return _shared_http_client.get()
