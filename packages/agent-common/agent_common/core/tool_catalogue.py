"""Raw-bytes tool catalogue: cards + lazily decoded schemas, no pydantic on the catalogue path.

Why
---
Materialising a gateway's tool catalogue through the MCP SDK amplifies the wire size
~7x into pydantic objects at the parse site (a 21.9 MB ``tools/list`` became ~663 MB of
live blocks), while the parsed schemas are barely used afterwards: listings and the
toolset selector need name/description/server, ``describe`` needs one schema, model
binding needs a handful, and the MCP server validates arguments on invoke anyway.

This module keeps the catalogue as **bytes**: every tool's input schema is stored as
canonical JSON (``bytes``) next to a lightweight :class:`ToolCard` (name, description,
parameter names, server). A :class:`LazyMcpTool` is a real ``BaseTool`` whose
``args_schema`` is decoded from those bytes on first access — so every existing
consumer (``convert_to_openai_tool``, ``tool.args``, ``describe_tool``, PTC signature
rendering, the interface hash) keeps working unchanged and only pays for the tools it
actually touches.

Ingest is a strategy (see :mod:`catalogue_ingest`): a stateless JSON-RPC ``tools/list``
over plain HTTP, or the same call through the MCP SDK session. Both produce the same
:class:`ServerCatalogue`. A catalogue is a **per-user view** — the gateway answers
``tools/list`` per bearer and a profile may hide tools of a server from one user and not
another — so there is deliberately no process-wide catalogue registry: each user's tools
own their catalogue's bytes for as long as the per-user discovery cache holds them, and
nothing may answer *which tools exist* except a listing made with that user's own token.

Dispatch delegates to ``langchain_mcp_adapters`` on first invocation: the one tool
being called is rebuilt as an ``mcp.types.Tool`` (a single small pydantic object) and
converted with ``convert_mcp_tool_to_langchain_tool`` so interceptors, progress
callbacks and result conversion behave exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import PrivateAttr

from agent_common.core.tool_search import first_line

logger = logging.getLogger(__name__)

CatalogueSource = Literal["stateless", "mcp"]


def canonical_bytes(value: Any) -> bytes:
    """Serialise ``value`` to compact, key-sorted UTF-8 JSON.

    Canonical so identical schemas hash identically regardless of ingest source.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ToolCard:
    """What listings need about a tool — never the schema body."""

    name: str
    description: str
    param_names: tuple[str, ...]
    server_name: str

    @property
    def first_line(self) -> str:
        return first_line(self.description)


@dataclass(slots=True)
class CatalogueTool:
    """One tool: its card plus undecoded schema bytes and the small MCP extras."""

    card: ToolCard
    schema_bytes: bytes
    output_schema_bytes: bytes | None = None
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self.card.name

    def decode_schema(self) -> dict[str, Any]:
        schema = json.loads(self.schema_bytes)
        if not isinstance(schema, dict):
            # An MCP inputSchema must be a JSON-schema object; tolerate garbage with an
            # accept-anything schema rather than failing the whole tool.
            return {"type": "object", "properties": {}}
        return schema


@dataclass(slots=True)
class ServerCatalogue:
    """All tools of one MCP server, as bytes + cards, with a stable interface hash."""

    server_name: str
    tools: dict[str, CatalogueTool]
    interface_hash: str
    source: CatalogueSource
    fetched_at: float = field(default_factory=time.time)

    @property
    def cards(self) -> list[ToolCard]:
        return [t.card for t in self.tools.values()]

    @property
    def byte_size(self) -> int:
        return sum(len(t.schema_bytes) + len(t.output_schema_bytes or b"") for t in self.tools.values())


def _param_names(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    return tuple(str(k) for k in props) if isinstance(props, Mapping) else ()


def make_catalogue_tool(
    *,
    server_name: str,
    name: str,
    description: str | None,
    input_schema: Mapping[str, Any] | None,
    output_schema: Mapping[str, Any] | None = None,
    annotations: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> CatalogueTool:
    """Build a :class:`CatalogueTool` from already-decoded (transient) values.

    Callers decode one tool at a time and drop the dicts right after; only the bytes
    and the card are retained.
    """
    schema: Mapping[str, Any] = (
        input_schema if isinstance(input_schema, Mapping) else {"type": "object", "properties": {}}
    )
    card = ToolCard(
        name=name,
        description=description or "",
        param_names=_param_names(schema),
        server_name=server_name,
    )
    return CatalogueTool(
        card=card,
        schema_bytes=canonical_bytes(schema),
        output_schema_bytes=canonical_bytes(output_schema) if isinstance(output_schema, Mapping) else None,
        annotations=dict(annotations) if isinstance(annotations, Mapping) and annotations else None,
        meta=dict(meta) if isinstance(meta, Mapping) and meta else None,
    )


def compute_interface_hash(tools: Iterable[CatalogueTool]) -> str:
    """SHA-256 over the sorted (name, description, schema bytes) — the tool *interface*.

    Same intent as ``LangGraphAgent._compute_interface_hash`` in the SDK (change when
    tools are added/removed or their schemas change), computed on bytes so it never
    requires decoding a schema.
    """
    h = hashlib.sha256()
    for tool in sorted(tools, key=lambda t: t.name):
        h.update(tool.card.name.encode("utf-8"))
        h.update(b"\x00")
        h.update(tool.card.description.encode("utf-8"))
        h.update(b"\x00")
        h.update(tool.schema_bytes)
        h.update(b"\x01")
    return h.hexdigest()


def build_server_catalogue(
    server_name: str, tools: Iterable[CatalogueTool], source: CatalogueSource
) -> ServerCatalogue:
    by_name: dict[str, CatalogueTool] = {}
    for tool in tools:
        if tool.name in by_name:
            logger.warning(
                "Duplicate tool name '%s' on server '%s' — keeping the first",
                tool.name,
                server_name,
            )
            continue
        by_name[tool.name] = tool
    return ServerCatalogue(
        server_name=server_name,
        tools=by_name,
        interface_hash=compute_interface_hash(by_name.values()),
        source=source,
    )


class LazyMcpTool(BaseTool):
    """A ``BaseTool`` over a :class:`CatalogueTool` whose schema stays as bytes until needed.

    * ``name`` / ``description`` / ``metadata`` come from the card (cheap, always set).
    * ``args_schema`` is a normal pydantic field, populated from ``schema_bytes`` the
      first time it is read (``__getattribute__`` hook), so ``convert_to_openai_tool``,
      ``tool.args``, ``tool_call_schema`` and direct ``tool.args_schema`` reads all see a
      plain JSON-schema dict exactly like a ``langchain_mcp_adapters`` tool — but only the
      tools that are bound, described or invoked ever decode.
    * Invocation builds the adapter's ``StructuredTool`` for this one tool on first call
      and delegates to its coroutine, so interceptors / progress callbacks / result
      conversion are byte-for-byte the upstream behaviour.

    The MCP connection and the per-call interceptors (which mint the user's bearer for
    each call — see ``agent_common.core.token_provider``) are private attributes on the
    tool, never on the shared catalogue: catalogue bytes are shared across users,
    connections and credentials are not. A connection may still carry a static bearer
    when no token provider is configured (standalone execution).
    """

    server_name: str
    response_format: Literal["content", "content_and_artifact"] = "content_and_artifact"

    _entry: CatalogueTool = PrivateAttr()
    _connection: Any = PrivateAttr(default=None)
    _callbacks: Any = PrivateAttr(default=None)
    _interceptors: list[Any] | None = PrivateAttr(default=None)
    _delegate: StructuredTool | None = PrivateAttr(default=None)

    def __getattribute__(self, item: str) -> Any:
        if item == "args_schema":
            values = object.__getattribute__(self, "__dict__")
            if values.get("args_schema") is None:
                private = object.__getattribute__(self, "__pydantic_private__") or {}
                entry = private.get("_entry")
                if entry is None:
                    raise RuntimeError(
                        f"LazyMcpTool '{values.get('name')}' is not bound to a catalogue entry; "
                        "construct it via make_lazy_tool()/build_lazy_tools()"
                    )
                values["args_schema"] = entry.decode_schema()
            return values["args_schema"]
        return object.__getattribute__(self, item)

    @property
    def coroutine(self) -> Callable[..., Awaitable[Any]]:
        """The async implementation, exposed like ``StructuredTool.coroutine``.

        Wrappers that re-package MCP tools (``_wrap_tool_with_agent_name`` in the
        orchestrator, ``DynamicLocalAgentRunnable._wrap_with_agent_name``) read
        ``tool.coroutine`` and would silently skip a tool without one.
        """

        async def _call(**kwargs: Any) -> Any:
            return await self._arun(**kwargs)

        return _call

    @property
    def catalogue_entry(self) -> CatalogueTool:
        return self._entry

    @property
    def schema_decoded(self) -> bool:
        """True once ``args_schema`` has been materialised (test/diagnostic hook)."""
        return object.__getattribute__(self, "__dict__").get("args_schema") is not None

    def _get_delegate(self) -> StructuredTool:
        if self._delegate is None:
            from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
            from mcp.types import Tool as MCPTool

            entry = self._entry
            input_schema = self.args_schema
            if not isinstance(input_schema, dict):  # pragma: no cover — always a dict for catalogue tools
                input_schema = entry.decode_schema()
            mcp_tool = MCPTool(
                name=entry.name,
                description=entry.card.description or None,
                inputSchema=input_schema,
                annotations=entry.annotations,  # type: ignore[arg-type]
                _meta=entry.meta,
            )
            delegate = convert_mcp_tool_to_langchain_tool(
                None,
                mcp_tool,
                connection=self._connection,
                callbacks=self._callbacks,
                tool_interceptors=self._interceptors,
                server_name=self.server_name,
            )
            self._delegate = cast(StructuredTool, delegate)
        return self._delegate

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("MCP tools are async-only; use ainvoke()")

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        delegate = self._get_delegate()
        # The adapter coroutine takes ``runtime`` as an injected arg; the ToolNode only
        # injects it when it can see it on ``tool.func``/``tool.coroutine``, which this
        # class does not expose. Nothing on our call path reads it (the console
        # attribution interceptor uses ContextVars; the progress callback only logs).
        if delegate.coroutine is None:  # pragma: no cover — the adapter always sets a coroutine
            raise RuntimeError(f"MCP tool '{self.name}' has no async implementation")
        return await delegate.coroutine(runtime=None, **kwargs)


def build_lazy_tools(
    catalogue: ServerCatalogue,
    *,
    connection: Any,
    callbacks: Any = None,
    tool_interceptors: list[Any] | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> list[LazyMcpTool]:
    """Wrap every catalogue entry as a :class:`LazyMcpTool` bound to ``connection``.

    Metadata mirrors ``langchain_mcp_adapters`` (``annotations`` flattened + ``_meta``)
    and always carries ``server_name`` (discovery previously stamped it after the fact).
    """
    return [
        make_lazy_tool(
            entry,
            server_name=catalogue.server_name,
            connection=connection,
            callbacks=callbacks,
            tool_interceptors=tool_interceptors,
            extra_metadata=extra_metadata,
        )
        for entry in catalogue.tools.values()
    ]


def make_lazy_tool(
    entry: CatalogueTool,
    *,
    server_name: str,
    connection: Any,
    callbacks: Any = None,
    tool_interceptors: list[Any] | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> LazyMcpTool:
    """Wrap one catalogue entry as a :class:`LazyMcpTool` bound to ``connection``."""
    metadata: dict[str, Any] = {**(entry.annotations or {})}
    if entry.meta is not None:
        metadata["_meta"] = entry.meta
    metadata["server_name"] = server_name
    if extra_metadata:
        metadata.update(extra_metadata)
    tool = LazyMcpTool(
        name=entry.name,
        description=entry.card.description,
        metadata=metadata,
        server_name=server_name,
    )
    tool._entry = entry
    tool._connection = connection
    tool._callbacks = callbacks
    tool._interceptors = tool_interceptors
    return tool
