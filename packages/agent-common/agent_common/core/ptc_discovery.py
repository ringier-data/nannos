"""Runtime tool-discovery tools for the PTC ``eval`` namespace.

When the GP agent carries a large MCP catalog, listing every tool's signature in the
system prompt is what broke prompt caching (the rendered block varied per turn). The
redesign keeps every tool *callable* (its ``globalThis.tools`` bridge is installed) but
renders only a stable core into the prompt. The volatile catalog is found at runtime via
two read-only helpers pinned into the namespace:

* ``tools.search({query})`` — keyword/token ranking over tool name + description,
  returning ``{name, description}`` for the best matches (``name`` is the camelCase
  identifier the model calls as ``tools.<name>``);
* ``tools.describe({name})`` — the full ``$ref``-resolved TypeScript signature for one
  tool.

Both are plain ``StructuredTool``s (NOT risk-guarded via ``wrap_tool_for_ptc``): they
only read metadata and never execute a side-effecting tool, so they must never trip the
HITL approval flow. They close over the per-turn exposed catalog.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_quickjs._prompt import is_valid_js_identifier, to_camel_case
from pydantic import BaseModel, Field

from agent_common.core.ptc_signatures import render_signature_block

if TYPE_CHECKING:
    from collections.abc import Sequence

# Names of the discovery tools as exposed in the ``tools`` namespace.
PTC_SEARCH_TOOL_NAME = "search"
PTC_DESCRIBE_TOOL_NAME = "describe"

_DISCOVERY_TOOL_NAMES = frozenset({PTC_SEARCH_TOOL_NAME, PTC_DESCRIBE_TOOL_NAME})

# How many matches ``search`` returns by default.
_DEFAULT_TOP_K = 10

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class _SearchArgs(BaseModel):
    query: Annotated[str, Field(description="Natural-language intent, e.g. 'list commits in a repo'.")]


class _DescribeArgs(BaseModel):
    name: Annotated[str, Field(description="The tool name as called in `tools.<name>` (camelCase).")]


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _first_line(description: str | None) -> str:
    if not description:
        return ""
    stripped = description.strip()
    return stripped.splitlines()[0] if stripped else ""


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
        desc = _first_line(tool.description)
        entries.append(
            {
                "camel": camel,
                "name": tool.name,
                "desc": desc,
                "name_haystack": f"{camel} {tool.name}".lower(),
                "haystack": f"{camel} {tool.name} {desc}".lower(),
                "tool": tool,
            }
        )
    return entries


def _score(entry: dict[str, Any], tokens: list[str]) -> int:
    """Token-overlap score: name/identifier hits weigh more than description hits."""
    score = 0
    for tok in tokens:
        if tok in entry["name_haystack"]:
            score += 2
        elif tok in entry["haystack"]:
            score += 1
    return score


def build_discovery_tools(
    catalog: Sequence[BaseTool],
    *,
    top_k: int = _DEFAULT_TOP_K,
) -> list[BaseTool]:
    """Build the ``search`` and ``describe`` tools bound to ``catalog``.

    Returned tools are plain (unguarded) ``StructuredTool``s suitable for the PTC
    exposed set. They are read-only and introspect ``catalog`` only.
    """
    entries = _build_entries(catalog)
    by_camel = {e["camel"]: e for e in entries}
    by_name = {e["name"]: e for e in entries}

    async def _search(query: str) -> list[dict[str, str]]:
        tokens = _tokenize(query)
        if not tokens:
            return []
        scored = [(e, _score(e, tokens)) for e in entries]
        hits = sorted(
            (pair for pair in scored if pair[1] > 0),
            key=lambda pair: pair[1],
            reverse=True,
        )[:top_k]
        return [{"name": e["camel"], "description": e["desc"]} for e, _ in hits]

    async def _describe(name: str) -> str:
        entry = by_camel.get(name) or by_name.get(name) or by_camel.get(to_camel_case(name))
        if entry is None:
            return (
                f"No tool named '{name}'. Use tools.search({{ query: '...' }}) "
                "to find the right tool name first."
            )
        return render_signature_block(entry["tool"])

    search_tool = StructuredTool.from_function(
        coroutine=_search,
        name=PTC_SEARCH_TOOL_NAME,
        description=(
            "Find agent tools by intent. Returns up to "
            f"{top_k} matches as {{ name, description }} ranked by relevance; `name` is "
            "the identifier to call as `tools.<name>(...)`. Use this to discover tools "
            "that are not listed in this prompt, then `describe` the one you want."
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
