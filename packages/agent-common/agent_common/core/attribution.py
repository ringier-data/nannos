"""Request-scoped cost attribution carried to the Model Gateway (ADR-0002).

`create_model` builds gateway clients that are cached and shared across requests,
so attribution can't be baked in at construction. Instead we set these ContextVars
at the request boundary (and in `create_runnable_config`), and a per-request httpx
event hook stamps them onto the `x-litellm-spend-logs-metadata` header on every
outbound call. The proxy's CustomLogger reads them back (validated in the spike,
SPIKE-FINDINGS.md check 4 — correct under 50-way concurrency on a shared client).

These ContextVars live in agent-common (the lowest shared layer) so both the
setters (ringier-a2a-sdk request middleware / create_runnable_config) and the
reader (the http hook here) can reach them without a circular dependency.
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


async def _stamp_metadata(request) -> None:
    """httpx request event hook: inject attribution as the spend-logs header."""
    attrib = current_attribution()
    if attrib:
        request.headers[_HEADER] = json.dumps(attrib)


def build_attribution_http_client():
    """A shared httpx.AsyncClient whose hook stamps per-request attribution.

    Safe to reuse across requests: the hook reads ContextVars at send time, which
    are isolated per asyncio task (spike check 4b). `asyncio.to_thread` copies the
    context so the existing sandbox path stays correct (spike check 4c); only a raw
    `loop.run_in_executor` would drop them.
    """
    import httpx

    return httpx.AsyncClient(event_hooks={"request": [_stamp_metadata]}, timeout=600.0)
