"""Ingest strategies that turn a server's tool list into a :class:`ServerCatalogue`.

Two paths, one representation (see :mod:`tool_catalogue`):

* :func:`fetch_catalogue_stateless` — **fast path, feature-detected, never assumed.** A
  single stateless JSON-RPC ``tools/list`` POST over plain ``httpx`` to the server's MCP
  URL — the standard MCP method and result shape, but without the SDK: no
  ``initialize``, no session id, no protocol negotiation, no resumability to own. The
  reply (JSON, or a single SSE frame) is scanned one tool at a time with a bounded
  ``raw_decode`` walk, so at no point is the catalogue materialised as Python objects and
  pydantic is never involved. Because it is the MCP listing itself, made with the
  caller's own token, the gateway applies overrides, disabled flags and per-user
  entitlements exactly as it does for the SDK path. A server that refuses a stateless
  request (a JSON-RPC error or 4xx on the probe) marks the path unsupported for that URL
  and everything falls back; any other failure falls back for that server only.
  This is the same pattern console-backend uses in production for its own tool
  listing and tool calls (``console_backend.services.mcp_tool_client``).
* :func:`fetch_catalogue_mcp` — **universal fallback**: the MCP ``tools/list`` through
  the SDK session exactly as before. It still pays the SDK's parse on receive, but
  each ``mcp.types.Tool`` is immediately flattened to bytes + card and the pydantic
  objects dropped, recovering the steady-state and cross-user sharing wins; only the
  transient parse peak remains (bounded by the inbound size guard once #153 lands).

Which path each server took is reported to the caller so discovery can log it —
a silent fall-back to the slow path must be visible, not mysterious.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx

from agent_common.core.tool_catalogue import (
    CatalogueTool,
    ServerCatalogue,
    build_server_catalogue,
    make_catalogue_tool,
)

logger = logging.getLogger(__name__)

# Default per-request timeout for a stateless catalogue fetch. Catalogues can be tens of MB.
STATELESS_TIMEOUT_S = 30.0

# One JSON-RPC id per request so a notification or ping streamed ahead of the reply on an
# SSE response can never be mistaken for it.
_LIST_ID = "catalogue-tools-list"

# Never follow more than this many `nextCursor` pages for one server.
_MAX_LIST_PAGES = 1000

# JSON-RPC error codes that mean "this server will not serve a stateless request", as
# opposed to a transient failure. -32600 invalid request (e.g. "session required"),
# -32601 method not found, -32602 invalid params.
_UNSUPPORTED_RPC_CODES = {-32600, -32601, -32602}


# Whether an MCP endpoint serves a stateless ``tools/list``, probed once per URL for the
# process lifetime. A property of the endpoint, not of any user — the only thing about a
# catalogue fetch that is legitimately process-wide.
_stateless_supported: dict[str, bool] = {}


def stateless_supported(url: str) -> bool | None:
    """``True``/``False`` once ``url`` has been probed, ``None`` before."""
    return _stateless_supported.get(url)


def set_stateless_supported(url: str, supported: bool) -> None:
    _stateless_supported[url] = supported


def reset_stateless_memo() -> None:
    """Test hook."""
    _stateless_supported.clear()


class StatelessListUnsupported(Exception):
    """The MCP endpoint does not serve ``tools/list`` without a session — fall back for good."""


class StatelessListError(Exception):
    """The endpoint serves stateless requests but this fetch failed — fall back for this server only."""


# --------------------------------------------------------------------------------------
# Bounded scanner: walk a JSON object by key path and yield one array element at a time
# --------------------------------------------------------------------------------------

_WS = re.compile(r"\s*")


def _skip_ws(text: str, pos: int) -> int:
    return _WS.match(text, pos).end()  # type: ignore[union-attr]


def iter_array_at(
    text: str,
    path: tuple[str, ...],
    siblings: dict[str, Any] | None = None,
    *,
    keep: frozenset[str] = frozenset(),
) -> Iterator[Any]:
    """Yield the elements of the array found at ``path`` in a JSON object, one at a time.

    Walks objects key by key with ``JSONDecoder.raw_decode`` so only one element of the
    target array is alive at once; every other member along the way is decoded and
    dropped, except keys named in ``keep`` (small scalars such as ``nextCursor`` or
    ``error``) which are stored in ``siblings`` keyed by ``"<depth>.<key>"`` → value.
    Locating the array by *parsing* (rather than searching for ``"tools": [``) means a
    tool whose schema contains a ``tools`` property cannot derail the scan.
    """
    dec = json.JSONDecoder()
    pos = _skip_ws(text, 0)
    if pos >= len(text) or text[pos] != "{":
        raise ValueError("payload is not a JSON object")
    yield from _walk_object(text, pos, path, 0, dec, siblings if siblings is not None else {}, keep)


def _walk_object(
    text: str,
    pos: int,
    path: tuple[str, ...],
    depth: int,
    dec: json.JSONDecoder,
    siblings: dict[str, Any],
    keep: frozenset[str],
) -> Iterator[Any]:
    pos = _skip_ws(text, pos + 1)  # past '{'
    if text[pos] == "}":
        siblings["__end__"] = pos + 1  # an empty object still has to report where it ends
        return
    while True:
        key, pos = dec.raw_decode(text, pos)
        pos = _skip_ws(text, pos)
        if text[pos] != ":":
            raise ValueError("malformed payload (expected ':')")
        pos = _skip_ws(text, pos + 1)
        if key == path[depth]:
            if depth == len(path) - 1:
                if text[pos] != "[":
                    raise ValueError(f"'{key}' is not an array")
                pos = _skip_ws(text, pos + 1)
                if text[pos] != "]":
                    while True:
                        item, pos = dec.raw_decode(text, pos)
                        yield item
                        pos = _skip_ws(text, pos)
                        if text[pos] == ",":
                            pos = _skip_ws(text, pos + 1)
                            continue
                        if text[pos] == "]":
                            break
                        raise ValueError(f"malformed '{key}' array")
                pos += 1
            else:
                if text[pos] != "{":
                    raise ValueError(f"'{key}' is not an object")
                # Recurse; the nested walker leaves `pos` just past its closing brace.
                sub = _walk_object(text, pos, path, depth + 1, dec, siblings, keep)
                yield from sub
                pos = siblings.pop("__end__")
        else:
            value, pos = dec.raw_decode(text, pos)
            if key in keep:
                siblings[f"{depth}.{key}"] = value
        pos = _skip_ws(text, pos)
        if text[pos] == ",":
            pos = _skip_ws(text, pos + 1)
            continue
        if text[pos] == "}":
            siblings["__end__"] = pos + 1
            return
        raise ValueError("malformed payload")


# --------------------------------------------------------------------------------------
# Stateless JSON-RPC tools/list
# --------------------------------------------------------------------------------------


def rpc_body_from_response(response: httpx.Response) -> str:
    """Return the JSON-RPC reply text, whether the server answered JSON or a single SSE frame.

    An SSE reply may carry notifications or pings ahead of the response: take the first
    ``data:`` line that is a response (has ``result`` or ``error``) with our request id.
    Kept textual so the caller can scan it without decoding the whole catalogue.
    """
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return response.text
    for line in response.text.split("\n"):
        if not line.startswith("data:"):
            continue
        data = line[5:].removeprefix(" ").rstrip("\r")
        # Cheap pre-check before scanning: must look like our response envelope.
        if f'"id": "{_LIST_ID}"' in data or f'"id":"{_LIST_ID}"' in data:
            if '"result"' in data or '"error"' in data:
                return data
    raise ValueError("no JSON-RPC response frame in SSE reply")


def mcp_tool_dict_to_catalogue_tool(server_name: str, item: Mapping[str, Any]) -> CatalogueTool | None:
    """Flatten one ``tools/list`` result element (a plain dict) into bytes + card."""
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None
    description = item.get("description")
    return make_catalogue_tool(
        server_name=server_name,
        name=name,
        description=description if isinstance(description, str) else None,
        input_schema=item.get("inputSchema") if isinstance(item.get("inputSchema"), Mapping) else None,
        output_schema=item.get("outputSchema") if isinstance(item.get("outputSchema"), Mapping) else None,
        annotations=item.get("annotations") if isinstance(item.get("annotations"), Mapping) else None,
        meta=item.get("_meta") if isinstance(item.get("_meta"), Mapping) else None,
    )


def parse_tools_list_reply(server_name: str, body: str) -> tuple[list[CatalogueTool], str | None]:
    """Scan one ``tools/list`` JSON-RPC reply into catalogue tools + the next cursor.

    Raises :class:`StatelessListUnsupported` for a JSON-RPC error whose code says the
    server will not serve this request, :class:`StatelessListError` for other RPC errors,
    and ``ValueError`` for a malformed body.
    """
    siblings: dict[str, Any] = {}
    tools: list[CatalogueTool] = []
    for item in iter_array_at(body, ("result", "tools"), siblings, keep=frozenset({"error", "nextCursor", "id"})):
        if isinstance(item, Mapping):
            tool = mcp_tool_dict_to_catalogue_tool(server_name, item)
            if tool is not None:
                tools.append(tool)
    error = siblings.get("0.error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or "unknown error"
        if code in _UNSUPPORTED_RPC_CODES or "session" in str(message).lower():
            raise StatelessListUnsupported(f"JSON-RPC {code}: {message}")
        raise StatelessListError(f"JSON-RPC {code}: {message}")
    cursor = siblings.get("1.nextCursor")
    return tools, cursor if isinstance(cursor, str) and cursor else None


async def fetch_catalogue_stateless(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: Mapping[str, str] | None,
    server_slug: str,
) -> ServerCatalogue:
    """``tools/list`` (all pages) as stateless JSON-RPC POSTs, scanned into a catalogue.

    ``url``/``headers`` are the server's MCP connection (the same URL and bearer token
    the SDK path would use, e.g. ``…/mcp?includeOnlyServerSlugs=<slug>``).
    """
    request_headers = {
        **(headers or {}),
        "Content-Type": "application/json",
        # Gateways may answer either; accept both so a streaming-only server still works.
        "Accept": "application/json, text/event-stream",
    }
    tools: list[CatalogueTool] = []
    cursor: str | None = None
    for _ in range(_MAX_LIST_PAGES):
        params: dict[str, Any] = {"cursor": cursor} if cursor else {}
        try:
            response = await client.post(
                url,
                headers=request_headers,
                json={"jsonrpc": "2.0", "id": _LIST_ID, "method": "tools/list", "params": params},
            )
        except httpx.HTTPError as e:
            raise StatelessListError(f"{url}: {e}") from e
        if 300 <= response.status_code < 400:
            # Redirect not followed (client built without follow_redirects): retrying via the
            # SDK would follow it, so treat as a per-server failure, not a refusal.
            raise StatelessListError(
                f"{url} -> HTTP {response.status_code} redirect to {response.headers.get('location')!r} not followed"
            )
        if response.status_code in (400, 404, 405, 406):
            # The endpoint rejects the request shape itself (no session, wrong method,
            # unacceptable media) — a property of the endpoint, not of this fetch.
            raise StatelessListUnsupported(f"{url} -> HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code >= 400:
            raise StatelessListError(f"{url} -> HTTP {response.status_code}: {response.text[:300]}")
        try:
            body = rpc_body_from_response(response)
            page, cursor = parse_tools_list_reply(server_slug, body)
        except (ValueError, LookupError) as e:
            # LookupError: the bounded scanner ran off the end of a truncated body (IndexError)
            # or met a shape it does not handle (KeyError) — never let a parser bug take the
            # server down when the SDK path can still list it.
            raise StatelessListError(f"{url}: unparseable tools/list reply: {e}") from e
        tools.extend(page)
        if not cursor:
            break
    else:
        raise StatelessListError(f"tools/list on '{server_slug}' exceeded {_MAX_LIST_PAGES} pages")
    return build_server_catalogue(server_slug, tools, source="stateless")


# --------------------------------------------------------------------------------------
# SDK-session fallback
# --------------------------------------------------------------------------------------


def mcp_tool_to_catalogue_tool(server_name: str, tool: Any) -> CatalogueTool:
    """Flatten an ``mcp.types.Tool`` into bytes + card (the pydantic object is dropped by the caller)."""
    annotations = tool.annotations.model_dump(exclude_none=True) if getattr(tool, "annotations", None) else None
    return make_catalogue_tool(
        server_name=server_name,
        name=tool.name,
        description=tool.description,
        input_schema=tool.inputSchema,
        output_schema=getattr(tool, "outputSchema", None),
        annotations=annotations,
        meta=getattr(tool, "meta", None),
    )


async def fetch_catalogue_mcp(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    server_slug: str,
) -> ServerCatalogue:
    """``tools/list`` (all pages) through an MCP SDK session, flattened page by page.

    ``session_factory`` is typically ``lambda: client.session(server_slug)`` on a
    ``MultiServerMCPClient`` — it owns transport, callbacks and ``initialize()``.
    """
    tools: list[CatalogueTool] = []
    async with session_factory() as session:
        cursor: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            page = await session.list_tools(cursor=cursor)
            for mcp_tool in page.tools or []:
                tools.append(mcp_tool_to_catalogue_tool(server_slug, mcp_tool))
            cursor = page.nextCursor
            if not cursor:
                break
        else:
            raise RuntimeError(f"tools/list on '{server_slug}' exceeded {_MAX_LIST_PAGES} pages")
    return build_server_catalogue(server_slug, tools, source="mcp")


# --------------------------------------------------------------------------------------
# Strategy: stateless first (feature-detected per URL), SDK session as fallback
# --------------------------------------------------------------------------------------


async def fetch_catalogue(
    *,
    server_slug: str,
    url: str,
    headers: Mapping[str, str] | None,
    http_client: httpx.AsyncClient | None,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    stateless: bool = True,
) -> ServerCatalogue:
    """One server's catalogue: stateless ``tools/list`` POST first, SDK session as fallback.

    Both paths hit the same MCP ``url`` with the same ``headers`` (the caller's bearer), so
    the result is the same per-user listing; the stateless path just skips the SDK
    handshake and the pydantic parse. Whether an endpoint serves a stateless request is
    probed once per URL (see :func:`stateless_supported`): a refusal marks it unsupported
    for the process lifetime, any other failure falls back for this fetch only, and every
    fallback is logged. ``stateless=False`` (or no ``http_client``) forces the SDK path.
    The returned catalogue's ``source`` says which path was taken.
    """
    if http_client is not None and stateless and stateless_supported(url) is not False:
        try:
            catalogue = await fetch_catalogue_stateless(http_client, url=url, headers=headers, server_slug=server_slug)
        except StatelessListUnsupported as e:
            set_stateless_supported(url, False)
            logger.warning("MCP endpoint refuses stateless tools/list — using SDK session for '%s' (%s)", server_slug, e)
        except StatelessListError as e:
            logger.warning("Stateless tools/list failed for '%s', falling back to SDK session: %s", server_slug, e)
        else:
            set_stateless_supported(url, True)
            return catalogue
    return await fetch_catalogue_mcp(session_factory, server_slug=server_slug)
