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

# Canonical attribution ContextVars. ringier-a2a-sdk sets these per request.
current_user_sub: contextvars.ContextVar = contextvars.ContextVar("nannos_user_sub", default=None)
current_conversation_id: contextvars.ContextVar = contextvars.ContextVar("nannos_conversation_id", default=None)
current_sub_agent_id: contextvars.ContextVar = contextvars.ContextVar("nannos_sub_agent_id", default=None)
current_scheduled_job_id: contextvars.ContextVar = contextvars.ContextVar("nannos_scheduled_job_id", default=None)
current_sub_agent_config_version_id: contextvars.ContextVar = contextvars.ContextVar(
    "nannos_sub_agent_config_version_id", default=None
)
current_catalog_id: contextvars.ContextVar = contextvars.ContextVar("nannos_catalog_id", default=None)

_FIELDS = {
    "user_sub": current_user_sub,
    "conversation_id": current_conversation_id,
    "sub_agent_id": current_sub_agent_id,
    "scheduled_job_id": current_scheduled_job_id,
    "sub_agent_config_version_id": current_sub_agent_config_version_id,
    "catalog_id": current_catalog_id,
}

_HEADER = "x-litellm-spend-logs-metadata"


def current_attribution() -> dict:
    """Snapshot the non-empty attribution fields from the current context."""
    return {name: var.get() for name, var in _FIELDS.items() if var.get() is not None}


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
    attrib = current_attribution()
    for name, value in overrides.items():
        if name in _FIELDS and value is not None:
            attrib[name] = value
    return {_HEADER: json.dumps(attrib)} if attrib else {}


async def _stamp_metadata(request) -> None:
    """httpx request event hook: inject attribution as the spend-logs header."""
    attrib = current_attribution()
    if attrib:
        request.headers[_HEADER] = json.dumps(attrib)


_shared_http_client = None


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
    global _shared_http_client
    if _shared_http_client is None:
        import httpx

        _shared_http_client = httpx.AsyncClient(event_hooks={"request": [_stamp_metadata]}, timeout=600.0)
    return _shared_http_client
