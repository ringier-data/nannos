"""Native (non-PTC) lazy-catalog surface for agents carrying a large tool registry.

The GP agent carries the orchestrator's full per-user MCP registry — hundreds of
tools once a whole API is exposed as an MCP server (e.g. the cockpit
``alloy-riad-stg`` server, ~700 tools). Materializing all of them into
``create_agent`` converts every tool's pydantic model to a JSON schema at graph
build time, which OOM-killed the orchestrator, and would put the whole catalog's
schemas into every model request.

This module bounds the native surface to three meta-tools over an arbitrarily
large catalog, mirroring the PTC ``eval`` discovery surface
(``tools.search``/``tools.describe`` in ``ptc_discovery``) so the agent works the
same way whether PTC is on or off:

* ``search_tools({query})`` — ranked ``{name, description}`` matches;
* ``describe_tool({name})`` — one tool's full parameters schema;
* ``call_tool({name, args})`` — invoke one catalog tool by name.

``call_tool`` never executes the inner tool itself: ``ToolCatalogMiddleware``
rewrites the ``ToolCallRequest`` to the real catalog tool (the same
``request.override(tool=...)`` pattern ``DynamicToolDispatchMiddleware`` uses for
registry dispatch), so the inner middleware chain — conditional-HITL risk guard,
retry, filesystem eviction — fires on the *real* tool name and args exactly as if
the tool were natively bound. ``search_tools``/``describe_tool`` are read-only
metadata helpers and are deliberately unguarded (same policy as the PTC
discovery tools).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Annotated, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from langchain.agents.middleware.types import ToolCallRequest
    from langchain.tools.tool_node import ToolCallWithContext

logger = logging.getLogger(__name__)

# Names of the native meta-tools bound to the model.
CATALOG_SEARCH_TOOL_NAME = "search_tools"
CATALOG_DESCRIBE_TOOL_NAME = "describe_tool"
CATALOG_CALL_TOOL_NAME = "call_tool"

_META_TOOL_NAMES = frozenset({CATALOG_SEARCH_TOOL_NAME, CATALOG_DESCRIBE_TOOL_NAME, CATALOG_CALL_TOOL_NAME})

# How many matches ``search_tools`` returns (and how many suggestions an unknown
# ``call_tool`` name gets).
_DEFAULT_TOP_K = 10

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class _SearchToolsArgs(BaseModel):
    query: Annotated[str, Field(description="Natural-language intent, e.g. 'list campaigns for an advertiser'.")]


class _DescribeToolArgs(BaseModel):
    name: Annotated[str, Field(description="Exact tool name as returned by search_tools.")]


class _CallToolArgs(BaseModel):
    name: Annotated[str, Field(description="Exact tool name as returned by search_tools.")]
    args: Annotated[
        dict[str, Any],
        Field(
            default_factory=dict,
            description="Arguments object for the tool, matching the parameters schema from describe_tool.",
        ),
    ]


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _first_line(description: str | None) -> str:
    if not description:
        return ""
    stripped = description.strip()
    return stripped.splitlines()[0] if stripped else ""


def _build_entries(catalog: Mapping[str, BaseTool]) -> list[dict[str, Any]]:
    """Precompute searchable entries (metadata only — no schema conversion)."""
    entries: list[dict[str, Any]] = []
    for name, tool in catalog.items():
        if name in _META_TOOL_NAMES or not isinstance(tool, BaseTool):
            continue
        desc = _first_line(tool.description)
        entries.append(
            {
                "name": name,
                "desc": desc,
                "name_haystack": name.lower(),
                "haystack": f"{name} {desc}".lower(),
            }
        )
    return entries


def _rank(entries: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
    """Token-overlap ranking: name hits weigh more than description hits.

    Same heuristic as ``ptc_discovery._score`` so PTC-on and PTC-off discovery
    return comparable results for the same catalog and query.
    """
    tokens = _tokenize(query)
    if not tokens:
        return []
    scored = []
    for entry in entries:
        score = 0
        for tok in tokens:
            if tok in entry["name_haystack"]:
                score += 2
            elif tok in entry["haystack"]:
                score += 1
        if score > 0:
            scored.append((entry, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [entry for entry, _ in scored[:top_k]]


def _describe_tool_schema(tool: BaseTool) -> str:
    """Render one tool's calling contract: description + JSON parameters schema.

    Converted per ``describe_tool`` call (one tool at a time) — never for the
    whole catalog; bulk conversion is exactly the cost this module avoids.
    """
    try:
        function = convert_to_openai_tool(tool)["function"]
        parameters = json.dumps(function.get("parameters", {}), indent=2, default=str)
    except Exception:
        logger.exception("describe_tool: schema conversion failed for '%s'", tool.name)
        parameters = "(schema unavailable — pass arguments per the description above)"
    description = (tool.description or "").strip()
    return f"Tool: {tool.name}\nDescription: {description}\nParameters (JSON Schema):\n{parameters}"


class ToolCatalogMiddleware(AgentMiddleware):
    """Bound meta-tool surface + ``call_tool`` dispatch over a large tool catalog.

    Contributes ``search_tools``/``describe_tool``/``call_tool`` to the graph via
    the middleware ``tools`` hook, and rewrites ``call_tool`` requests to the real
    catalog tool so inner middlewares (HITL, retry, eviction) see the real call.

    The catalog mapping is held by reference and read lazily — constructing this
    middleware does no per-tool schema work beyond collecting name + first
    description line for search ranking.
    """

    def __init__(self, catalog: Mapping[str, BaseTool], *, top_k: int = _DEFAULT_TOP_K) -> None:
        super().__init__()
        self._catalog = catalog
        self._entries = _build_entries(catalog)
        self._top_k = top_k
        self.tools = self._build_meta_tools()

    def _build_meta_tools(self) -> list[BaseTool]:
        top_k = self._top_k
        entries = self._entries
        catalog = self._catalog

        async def _search(query: str) -> list[dict[str, str]]:
            hits = _rank(entries, query, top_k)
            if not hits:
                return []
            return [{"name": e["name"], "description": e["desc"]} for e in hits]

        async def _describe(name: str) -> str:
            tool = catalog.get(name)
            if not isinstance(tool, BaseTool):
                return self._unknown_tool_message(name)
            return _describe_tool_schema(tool)

        async def _call_stub(name: str, args: dict[str, Any] | None = None) -> str:
            # ToolCatalogMiddleware.awrap_tool_call intercepts call_tool before the
            # ToolNode, so this body only runs if the middleware is missing from the
            # stack — fail loudly rather than pretend the tool ran.
            raise RuntimeError(
                "call_tool executed without ToolCatalogMiddleware in the middleware stack; "
                "catalog dispatch is not wired for this graph"
            )

        search_tool = StructuredTool.from_function(
            coroutine=_search,
            name=CATALOG_SEARCH_TOOL_NAME,
            description=(
                "Find available tools by intent. Returns up to "
                f"{top_k} matches as {{name, description}} ranked by relevance. Use this to discover "
                "tools from the catalog (they are not listed in this prompt), then describe_tool "
                "the one you want before calling it."
            ),
            args_schema=_SearchToolsArgs,
        )
        describe_tool = StructuredTool.from_function(
            coroutine=_describe,
            name=CATALOG_DESCRIBE_TOOL_NAME,
            description=(
                "Return one catalog tool's description and JSON parameters schema by exact name. "
                "Call this before call_tool so you pass the correct arguments."
            ),
            args_schema=_DescribeToolArgs,
        )
        call_tool = StructuredTool.from_function(
            coroutine=_call_stub,
            name=CATALOG_CALL_TOOL_NAME,
            description=(
                "Invoke one catalog tool by exact name with an arguments object matching its "
                "schema from describe_tool. Equivalent to calling the tool directly."
            ),
            args_schema=_CallToolArgs,
        )
        return [search_tool, describe_tool, call_tool]

    def _unknown_tool_message(self, name: str) -> str:
        suggestions = _rank(self._entries, name, 5)
        hint = ""
        if suggestions:
            hint = " Similar tools: " + ", ".join(e["name"] for e in suggestions) + "."
        return (
            f"No tool named '{name}' in the catalog.{hint} "
            f"Use {CATALOG_SEARCH_TOOL_NAME}({{query: ...}}) to find the right tool name."
        )

    def _resolve(self, request: "ToolCallRequest") -> "ToolCallRequest | ToolMessage":
        """Rewrite a ``call_tool`` request to the real catalog tool, or an error ToolMessage."""
        tool_call = request.tool_call
        call_args = tool_call.get("args") or {}
        inner_name = call_args.get("name")
        inner_args = call_args.get("args") or {}
        tool = self._catalog.get(inner_name) if isinstance(inner_name, str) else None
        if not isinstance(tool, BaseTool):
            return ToolMessage(
                content=self._unknown_tool_message(str(inner_name)),
                name=CATALOG_CALL_TOOL_NAME,
                tool_call_id=tool_call.get("id", ""),
                status="error",
            )
        if not isinstance(inner_args, dict):
            return ToolMessage(
                content=(
                    f"call_tool 'args' must be an object matching {inner_name}'s parameters schema "
                    f"(got {type(inner_args).__name__}). Use {CATALOG_DESCRIBE_TOOL_NAME} to see the schema."
                ),
                name=CATALOG_CALL_TOOL_NAME,
                tool_call_id=tool_call.get("id", ""),
                status="error",
            )
        logger.info("[TOOL_CATALOG] call_tool -> '%s'", inner_name)
        # Rewrite name+args so every inner middleware (conditional HITL, retry,
        # eviction) sees the real call; the ToolMessage keeps the original
        # tool_call_id, which is all providers match responses on.
        return request.override(
            tool=tool,
            tool_call={**tool_call, "name": tool.name, "args": inner_args},
        )

    async def awrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Awaitable[ToolMessage | ToolCallWithContext | Any]]",
    ) -> Any:
        if request.tool_call.get("name") != CATALOG_CALL_TOOL_NAME:
            return await handler(request)
        resolved = self._resolve(request)
        if isinstance(resolved, ToolMessage):
            return resolved
        return await handler(resolved)

    def wrap_tool_call(
        self,
        request: "ToolCallRequest",
        handler: "Callable[[ToolCallRequest], Any]",
    ) -> Any:
        if request.tool_call.get("name") != CATALOG_CALL_TOOL_NAME:
            return handler(request)
        resolved = self._resolve(request)
        if isinstance(resolved, ToolMessage):
            return resolved
        return handler(resolved)


# System-prompt addendum for the PTC-off catalog surface. The PTC-on equivalent
# is ``_PTC_DISCOVERY_INSTRUCTION`` rendered by the code-interpreter middleware.
TOOL_CATALOG_PROMPT_ADDENDUM = (
    "\n\n## Tool catalog\n\n"
    "Beyond the tools listed above, you have access to a large catalog of additional tools "
    "that are not listed in this prompt. To use them:\n"
    f"1. `{CATALOG_SEARCH_TOOL_NAME}` — find tools by describing what you need;\n"
    f"2. `{CATALOG_DESCRIBE_TOOL_NAME}` — get the exact parameters schema for one tool;\n"
    f"3. `{CATALOG_CALL_TOOL_NAME}` — invoke it by name with arguments matching that schema.\n"
    "Always search before assuming a capability is unavailable, and always describe a tool "
    "before the first call so the arguments are correct."
)
