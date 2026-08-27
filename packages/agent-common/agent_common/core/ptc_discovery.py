"""Runtime tool-discovery tools for the PTC ``eval`` namespace.

When the GP agent carries a large MCP catalog, listing every tool's signature in the
system prompt is what broke prompt caching (the rendered block varied per turn). The
redesign keeps every tool *callable* (its ``globalThis.tools`` bridge is installed) but
renders only a stable core into the prompt. The volatile catalog is found at runtime via
two read-only helpers pinned into the namespace:

* ``tools.search({query, limit?, offset?})`` — keyword/token ranking over tool name +
  description (shared with ``tool_search``), returning a page of ``{name, description}``
  matches plus ``total_matches``/``truncated``/``next_offset`` so the model can tell a
  capped page from an exhaustive one and page on (``name`` is the camelCase identifier
  the model calls as ``tools.<name>``);
* ``tools.describe({name})`` — the full ``$ref``-resolved TypeScript signature for one
  tool.

Both are plain ``StructuredTool``s (NOT risk-guarded via ``wrap_tool_for_ptc``): they
only read metadata and never execute a side-effecting tool, so they must never trip the
HITL approval flow. They close over the per-turn exposed catalog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_quickjs._prompt import is_valid_js_identifier, to_camel_case
from pydantic import BaseModel, Field

from agent_common.core.ptc_signatures import render_signature_block
from agent_common.core.tool_search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SearchResult,
    build_page,
    make_entry,
    rank,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Names of the discovery tools as exposed in the ``tools`` namespace.
PTC_SEARCH_TOOL_NAME = "search"
PTC_DESCRIBE_TOOL_NAME = "describe"

_DISCOVERY_TOOL_NAMES = frozenset({PTC_SEARCH_TOOL_NAME, PTC_DESCRIBE_TOOL_NAME})


class _SearchArgs(BaseModel):
    query: Annotated[
        str,
        Field(description="Natural-language intent, e.g. 'list commits in a repo'."),
    ]
    limit: Annotated[
        int | None,
        Field(description=f"Page size (default {DEFAULT_LIMIT}, max {MAX_LIMIT})."),
    ] = None
    offset: Annotated[
        int | None,
        Field(description="Skip this many ranked matches; pass the previous result's `next_offset` to page."),
    ] = None


class _DescribeArgs(BaseModel):
    name: Annotated[str, Field(description="The tool name as called in `tools.<name>` (camelCase).")]


def _build_entries(catalog: Sequence[BaseTool]) -> list[dict[str, Any]]:
    """Precompute searchable entries from the exposed catalog.

    Skips the discovery tools themselves and any tool whose camelCase name is not a
    valid JS identifier (it could not be called as ``tools.<name>`` anyway).
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in catalog:
        if tool.name in _DISCOVERY_TOOL_NAMES or tool.name in seen:
            continue
        camel = to_camel_case(tool.name)
        if not is_valid_js_identifier(camel):
            continue
        seen.add(tool.name)
        entry = make_entry(name=tool.name, callable_name=camel, description=tool.description)
        entry["tool"] = tool
        entries.append(entry)
    return entries


def build_discovery_tools(catalog: Sequence[BaseTool]) -> list[BaseTool]:
    """Build the ``search`` and ``describe`` tools bound to ``catalog``.

    Returned tools are plain (unguarded) ``StructuredTool``s suitable for the PTC
    exposed set. They are read-only and introspect ``catalog`` only.
    """
    entries = _build_entries(catalog)
    by_camel = {e["callable"]: e for e in entries}
    by_name = {e["name"]: e for e in entries}

    async def _search(query: str, limit: int | None = None, offset: int | None = None) -> SearchResult:
        return build_page(
            rank(entries, query),
            query=query,
            offset=offset,
            limit=limit,
            search_tool_name=f"tools.{PTC_SEARCH_TOOL_NAME}",
        )

    async def _describe(name: str) -> str:
        entry = by_camel.get(name) or by_name.get(name) or by_camel.get(to_camel_case(name))
        if entry is None:
            return f"No tool named '{name}'. Use tools.search({{ query: '...' }}) to find the right tool name first."
        return render_signature_block(entry["tool"])

    search_tool = StructuredTool.from_function(
        coroutine=_search,
        name=PTC_SEARCH_TOOL_NAME,
        description=(
            "Find agent tools by intent. Returns { matches: [{ name, description }], "
            "total_matches, shown, truncated, next_offset, hint } — `matches` is ONE PAGE "
            f"(default {DEFAULT_LIMIT}, max {MAX_LIMIT}) of all tools that matched, ranked by relevance; "
            "if `truncated` is true more matches exist: call again with `offset: next_offset` "
            "or narrow the query. `name` is the identifier to call as `tools.<name>(...)`. "
            "Use this to discover tools that are not listed in this prompt, then `describe` "
            "the one you want."
        ),
        args_schema=_SearchArgs,
    )
    describe_tool = StructuredTool.from_function(
        coroutine=_describe,
        name=PTC_DESCRIBE_TOOL_NAME,
        description=(
            "Return the full TypeScript signature (argument shape, with nested object "
            "types resolved) for one tool by name. Call this before invoking any tool "
            "that is not already listed in this prompt, so you pass the correct arguments."
        ),
        args_schema=_DescribeArgs,
    )
    return [search_tool, describe_tool]
