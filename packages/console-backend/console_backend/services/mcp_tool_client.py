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


class GatewayError(RuntimeError):
    """The gateway could not be reached, or refused the call."""


class ToolNotFound(GatewayError):
    """The gateway does not expose a tool by that name."""


@dataclass
class ToolCallResult:
    """Outcome of one tool call."""

    #: The response folded into a dict — the shape a JSONPath is evaluated against.
    result: dict[str, Any]
    elapsed_ms: int
    #: The tool ran but reported a failure of its own (MCP `isError`).
    is_error: bool = False


def parse_envelope(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON-RPC reply, which arrives as JSON or as a single SSE event."""
    content_type = response.headers.get("content-type", "")

    if "text/event-stream" not in content_type:
        return response.json()  # type: ignore[no-any-return]

    for line in response.text.strip().split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            return json.loads(line[6:])  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue

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
        GatewayError: the server was unreachable, timed out, or errored.
    """
    url = _console_mcp_url() if is_console_tool(tool_name) else config.mcp_gateway.url
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments or {}},
                },
            )
            response.raise_for_status()
            data = parse_envelope(response)
    except httpx.TimeoutException as exc:
        raise GatewayError(f"'{tool_name}' did not respond within {timeout:.0f}s") from exc
    except httpx.HTTPStatusError as exc:
        raise GatewayError(
            f"{url} returned HTTP {exc.response.status_code} for '{tool_name}'"
        ) from exc
    except httpx.RequestError as exc:
        raise GatewayError(f"Cannot reach {url}: {type(exc).__name__}") from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if "error" in data:
        message = (data.get("error") or {}).get("message") or "unknown gateway error"
        # An unknown tool is worth distinguishing: it means the job references something
        # the gateway no longer exposes, which is a configuration problem, not an outage.
        if "not found" in message.lower() or "unknown tool" in message.lower():
            raise ToolNotFound(f"'{tool_name}' is not available: {message}")
        raise GatewayError(f"'{tool_name}' failed: {message}")

    rpc_result = data.get("result") or {}
    return ToolCallResult(
        result=extract_result(rpc_result),
        elapsed_ms=elapsed_ms,
        is_error=bool(rpc_result.get("isError")),
    )
