"""Calling a single MCP tool.

Two callers need this, and they must agree: the scheduler evaluates a watch job's check
tool here, and the console UI previews the same call so a condition can be written
against a real response. If the two unwrapped a tool's output differently, the preview
would show a payload the job never sees.

Tools come from two places. Most are behind the Gatana gateway. The rest are this
backend's own routes, exposed on its own /mcp mount and named `console_*` — reaching
those means calling ourselves over loopback with a token minted for our own audience,
which is exactly what agent-runner has always done to reach them from outside.

Both speak JSON-RPC over HTTP and answer in either JSON or SSE depending on the request,
so both shapes are handled for every method.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from ringier_a2a_sdk.utils.http_pool import LazyClient

from ..config import config
from ..utils.gatana_auth import exchange_for_audience

logger = logging.getLogger(__name__)

#: How long a single tool call may take. The scheduler evaluates jobs concurrently, so a
#: slow tool delays only its own job, but an unbounded wait would hold a run open.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: Prefix of the tools this backend serves itself.
CONSOLE_TOOL_PREFIX = "console_"


def is_console_tool(tool_name: str) -> bool:
    """Whether a tool is served by this backend rather than the gateway."""
    return tool_name.startswith(CONSOLE_TOOL_PREFIX)


def _console_mcp_url() -> str:
    """This backend's own MCP mount, over loopback.

    Deliberately loopback rather than a configured public URL: the call must reach *this*
    instance, and it has no business leaving the pod to do it.
    """
    return f"http://127.0.0.1:{os.getenv('API_PORT', '5001')}/mcp"


async def token_for(tool_name: str, user_access_token: str) -> str:
    """Mint a token the server hosting `tool_name` will accept.

    Each audience validates its own: the gateway rejects a token minted for this backend
    and vice versa, so which server a tool lives on decides which exchange to perform.
    """
    audience = config.oidc.client_id if is_console_tool(tool_name) else config.mcp_gateway.client_id
    return await exchange_for_audience(user_access_token, audience)


#: The JSON-RPC id this client sends, so a streamed reply can be told from a
#: notification that happens to arrive first.
_CALL_ID = 1

# One process-wide pooled client, as llm_gateway does, instead of a fresh DNS lookup, TCP
# connect and TLS handshake to the same host on every call. This is the scheduler's
# per-poll hot path, and every "Run check now" as well. Per-call timeouts still vary, so
# each request passes its own `timeout=`.
_client = LazyClient(lambda: httpx.AsyncClient())


class GatewayError(RuntimeError):
    """The gateway could not be reached, or refused the call."""


class ToolNotFound(GatewayError):
    """The gateway does not expose a tool by that name."""


class GatewayTimeout(GatewayError):
    """The tool did not answer in time.

    A distinct type because callers map it differently — an HTTP 504 rather than a 502,
    a run failure rather than a configuration problem. It used to be recovered by
    matching the English of the message this module composes, so rewording that message
    silently turned every check-tool timeout into a 502 with no test failing.
    """


#: JSON-RPC codes that mean "no such method/tool" rather than "the server is unwell".
#: Preferred over reading the message, which is prose written by whichever server
#: answered: a tool legitimately reporting "resource not found" was classified as a
#: missing tool and surfaced as a 404.
_NOT_FOUND_CODES = frozenset({-32601, -32602})


@dataclass
class ToolCallResult:
    """Outcome of one tool call."""

    #: The response folded into a dict — the shape a JSONPath is evaluated against.
    result: dict[str, Any]
    elapsed_ms: int
    #: The tool ran but reported a failure of its own (MCP `isError`).
    is_error: bool = False


def parse_envelope(response: httpx.Response, expect_id: Any = None) -> dict[str, Any]:
    """Decode a JSON-RPC reply, which arrives as JSON or as a single SSE event.

    The reply is not necessarily the *first* event: a server may stream progress
    notifications or pings ahead of it. So take the first event that is a response —
    it carries `result` or `error`, and its `id` matches the request when one is given.
    Taking the first `data:` line whatever it is would hand a notification back as the
    tool's answer, and a condition would then be evaluated against `{}`.
    """
    content_type = response.headers.get("content-type", "")

    if "text/event-stream" not in content_type:
        return response.json()  # type: ignore[no-any-return]

    for line in response.text.strip().split("\n"):
        if not line.startswith("data:"):
            continue
        # A single optional space after the colon is SSE's own framing, not data.
        try:
            envelope = json.loads(line[5:].removeprefix(" "))
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        if "result" not in envelope and "error" not in envelope:
            continue
        if expect_id is not None and envelope.get("id") != expect_id:
            continue
        return envelope

    logger.error("Unparsable SSE reply from MCP gateway: %s", response.text[:500])
    raise GatewayError("Invalid SSE response from MCP gateway")


def content_to_result(blocks: Any) -> dict[str, Any]:
    """Fold MCP content blocks into the dict a condition is evaluated against.

    A tool that returns a JSON object gives that object; one that returns text or a bare
    array is wrapped under "output", because a JSONPath needs a mapping at the root.
    """
    if isinstance(blocks, dict):
        return blocks
    if isinstance(blocks, str):
        return _from_text(blocks)
    if isinstance(blocks, list):
        texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and "text" in b]
        combined = "\n".join(texts)
        return _from_text(combined) if combined else {}
    return {"output": str(blocks)}


def _from_text(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"output": text}
    return parsed if isinstance(parsed, dict) else {"output": parsed}


def extract_result(rpc_result: dict[str, Any]) -> dict[str, Any]:
    """Pull the payload out of a tools/call reply.

    Key presence, not truthiness: a tool that legitimately returns no content blocks
    must fold to {} rather than falling through to the raw JSON-RPC result.
    """
    if "structuredContent" in rpc_result:
        return content_to_result(rpc_result["structuredContent"])
    if "content" in rpc_result:
        return content_to_result(rpc_result["content"])
    return content_to_result(rpc_result)


async def call_tool(
    token: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ToolCallResult:
    """Call one MCP tool and return its response.

    The server is chosen from the tool's name — `console_*` tools are served here, the
    rest by the gateway — and `token` must be minted for that server's audience; see
    `token_for`.

    No server filter is applied to gateway calls: tools/call resolves by name, and
    narrowing to a slug the caller guessed wrong would hide the tool and fail the call
    outright.

    Raises:
        ToolNotFound: the server rejected the tool name.
        GatewayTimeout: the tool did not answer within `timeout`.
        GatewayError: the server was unreachable or errored.
    """
    url = _console_mcp_url() if is_console_tool(tool_name) else config.mcp_gateway.url
    started = time.perf_counter()
    try:
        response = await _client.get().post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": _CALL_ID,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments or {}},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = parse_envelope(response, expect_id=_CALL_ID)
    except httpx.TimeoutException as exc:
        raise GatewayTimeout(f"'{tool_name}' did not respond within {timeout:.0f}s") from exc
    except httpx.HTTPStatusError as exc:
        raise GatewayError(
            f"{url} returned HTTP {exc.response.status_code} for '{tool_name}'"
        ) from exc
    except httpx.RequestError as exc:
        raise GatewayError(f"Cannot reach {url}: {type(exc).__name__}") from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if data.get("error") is not None:
        error = data["error"] if isinstance(data["error"], dict) else {}
        message = error.get("message") or "unknown gateway error"
        # An unknown tool is worth distinguishing: it means the job references something
        # the gateway no longer exposes, which is a configuration problem, not an outage.
        # Decided by code, with the message only as a fallback for servers that answer
        # a missing tool with a generic code.
        if error.get("code") in _NOT_FOUND_CODES or "unknown tool" in message.lower():
            raise ToolNotFound(f"'{tool_name}' is not available: {message}")
        raise GatewayError(f"'{tool_name}' failed: {message}")

    rpc_result = data.get("result") or {}
    return ToolCallResult(
        result=extract_result(rpc_result),
        elapsed_ms=elapsed_ms,
        is_error=bool(rpc_result.get("isError")),
    )
