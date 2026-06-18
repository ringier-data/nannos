"""Production-faithful attribution transport for Check 4. Throwaway.

Mirrors the real constraint: the model is passed UN-BOUND into create_agent
(graph_utils.py:1604), so per-invoke extra_body is not viable. Attribution must
ride on the HTTP request itself. We set request-scoped ContextVars (mirroring
current_sub_agent_id / _current_user_sub / _current_conversation_id) and a custom
httpx.AsyncClient event hook stamps them onto `x-litellm-spend-logs-metadata` for
every outbound call — independent of the call site, safe to share one client.
"""

import contextlib
import contextvars
import json

import httpx
from langchain_openai import ChatOpenAI

# ContextVars mirroring the production attribution sources.
current_user_sub = contextvars.ContextVar("current_user_sub", default=None)
current_conversation_id = contextvars.ContextVar("current_conversation_id", default=None)
current_sub_agent_id = contextvars.ContextVar("current_sub_agent_id", default=None)
current_scheduled_job_id = contextvars.ContextVar("current_scheduled_job_id", default=None)
current_sub_agent_config_version_id = contextvars.ContextVar("current_sub_agent_config_version_id", default=None)

_FIELDS = {
    "user_sub": current_user_sub,
    "conversation_id": current_conversation_id,
    "sub_agent_id": current_sub_agent_id,
    "scheduled_job_id": current_scheduled_job_id,
    "sub_agent_config_version_id": current_sub_agent_config_version_id,
}


def current_attribution() -> dict:
    return {name: var.get() for name, var in _FIELDS.items() if var.get() is not None}


@contextlib.contextmanager
def attribution(**fields):
    """Set attribution ContextVars for the duration of the block, then reset."""
    tokens = []
    try:
        for name, value in fields.items():
            var = _FIELDS[name]
            tokens.append((var, var.set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _stamp_metadata(request: httpx.Request) -> None:
    """httpx request event hook: inject attribution from ContextVars."""
    attrib = current_attribution()
    if attrib:
        request.headers["x-litellm-spend-logs-metadata"] = json.dumps(attrib)


def make_chat_client(base_url: str, api_key: str, model: str = "mock-fast", **kwargs) -> ChatOpenAI:
    """A ChatOpenAI wired to the proxy with the attribution hook. Reusable/cacheable."""
    http_client = httpx.AsyncClient(event_hooks={"request": [_stamp_metadata]}, timeout=60.0)
    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        http_async_client=http_client,
        stream_usage=True,  # mirrors the real local-provider branch
        **kwargs,
    )
