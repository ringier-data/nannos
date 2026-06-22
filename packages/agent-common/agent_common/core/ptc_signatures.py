"""TypeScript-signature rendering for PTC tools, with ``$ref``/``$defs`` resolution.

``langchain_quickjs._prompt`` renders tool signatures but its ``_json_schema_to_ts``
is *shallow*: nested Pydantic models / ``TypedDict``s appear in the JSON Schema as
``$ref`` into ``$defs`` and degrade to ``unknown`` / ``Record<string, unknown>``. That
is why complex tools (e.g. GitHub) are called with the wrong argument shape.

This module reimplements the renderer with ``$ref`` resolution (recursion-guarded) so
nested object args render as real TypeScript types. It is the single renderer used by
both ``tools.describe`` (GP discovery path) and the sub-agent inline rendering path —
so the type-hint fix applies wherever PTC signatures are shown.

The surrounding ``tools`` namespace preamble mirrors the upstream prompt so the model's
usage guidance (Promise.all, pipelining, single-program batching) is preserved.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_quickjs._prompt import to_camel_case

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain_core.tools import BaseTool

# Guard against pathological/recursive ``$ref`` chains. A self-referential model
# (e.g. a tree node) would otherwise expand forever; past this depth we emit the
# referenced type name instead of expanding it further.
_MAX_REF_DEPTH = 6


def _resolve_ref(prop: dict[str, Any], defs: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Resolve a ``{"$ref": "#/$defs/Name"}`` node against ``defs``.

    Returns ``(resolved_schema, ref_name)``. When the ref cannot be resolved the
    original node is returned with ``ref_name=None`` so the caller falls back to
    ``unknown``.
    """
    ref = prop.get("$ref")
    if not isinstance(ref, str):
        return prop, None
    # Refs are of the form "#/$defs/Name" or "#/definitions/Name".
    name = ref.rsplit("/", 1)[-1]
    target = defs.get(name)
    if isinstance(target, dict):
        return target, name
    return prop, None


def json_schema_to_ts(
    prop: dict[str, Any],
    defs: dict[str, Any],
    *,
    _depth: int = 0,
    _seen: frozenset[str] = frozenset(),
) -> str:
    """JSON-Schema → TS type, resolving ``$ref`` against ``defs``.

    Handles ``$ref`` (recursion-guarded), ``enum``, ``anyOf``/``oneOf``, arrays,
    nested objects, and the scalar types. Unknown shapes fall back to ``unknown``.
    """
    if not isinstance(prop, dict):
        return "unknown"

    if "$ref" in prop:
        resolved, name = _resolve_ref(prop, defs)
        if name is None:
            return "unknown"
        if name in _seen or _depth >= _MAX_REF_DEPTH:
            # Cycle or too deep — emit the type name rather than expand further.
            return name
        return json_schema_to_ts(resolved, defs, _depth=_depth + 1, _seen=_seen | {name})

    if "enum" in prop:
        return " | ".join(json.dumps(v) for v in prop["enum"])

    for union_key in ("anyOf", "oneOf"):
        if union_key in prop:
            parts = [
                json_schema_to_ts(part, defs, _depth=_depth, _seen=_seen) for part in prop[union_key]
            ]
            # Dedup while preserving order (e.g. ``string | null``).
            return " | ".join(dict.fromkeys(parts)) or "unknown"

    t = prop.get("type")
    if isinstance(t, list):
        # JSON Schema allows ``type`` to be a list of type names (e.g.
        # ``["string", "null"]`` for nullable fields, common in MCP tool
        # schemas). Render each as a TS union, recursing so that ``array`` /
        # ``object`` members still resolve ``items`` / ``properties``.
        parts = [
            json_schema_to_ts({**prop, "type": member}, defs, _depth=_depth, _seen=_seen)
            for member in t
        ]
        # Dedup while preserving order (e.g. ``string | null``).
        return " | ".join(dict.fromkeys(parts)) or "unknown"
    if t == "string":
        return "string"
    if t in {"integer", "number"}:
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "null":
        return "null"
    if t == "array":
        items = prop.get("items")
        inner = json_schema_to_ts(items, defs, _depth=_depth, _seen=_seen) if isinstance(items, dict) else "unknown"
        return f"{inner}[]"
    if t == "object" or "properties" in prop:
        sub_props = prop.get("properties")
        if isinstance(sub_props, dict) and sub_props:
            required = set(prop.get("required", []))
            fields = [
                f"{k}{'' if k in required else '?'}: {json_schema_to_ts(v, defs, _depth=_depth, _seen=_seen)}"
                for k, v in sub_props.items()
            ]
            return "{ " + "; ".join(fields) + " }"
        return "Record<string, unknown>"
    return "unknown"


def _safe_json_schema(tool: BaseTool) -> dict[str, Any] | None:
    """Return the tool's argument JSON Schema, or ``None`` if unavailable.

    Handles both shapes a ``BaseTool`` may carry on ``args_schema``:

    * a **Pydantic model** (native tools) — call ``model_json_schema()``;
    * a **raw JSON-schema dict** (MCP tools, via langchain-mcp-adapters) — use it
      directly. The upstream renderer only handled the model case, so MCP tools
      (e.g. GitHub) degraded to ``Record<string, unknown>``.

    ``BaseTool.get_input_schema()`` is intentionally NOT used as a fallback: when
    ``args_schema`` is a dict it returns a generic wrapper schema (an ``anyOf`` of
    string / object / ``ToolCall``), not the tool's real arguments.
    """
    try:
        schema = tool.args_schema
        if schema is None:
            return None
        # MCP-style tools: ``args_schema`` is already the JSON schema.
        if isinstance(schema, dict):
            return schema
        # Native tools: ``args_schema`` is a Pydantic model class/instance.
        model_json_schema = getattr(schema, "model_json_schema", None)
        if callable(model_json_schema):
            return model_json_schema()
    except Exception:  # noqa: BLE001 — prompt rendering is best-effort
        return None
    return None


def render_signature(camel_name: str, schema: dict[str, Any] | None) -> str:
    """Render one ``async function`` signature with ``$ref``-resolved arg types.

    Three shapes:
    - **no schema available** (``None``) → ``input: Record<string, unknown>`` (we
      genuinely don't know the arguments);
    - **schema present, no parameters** (empty/absent ``properties``) → ``()`` — the
      tool takes no arguments (e.g. ``githubGetMe``). The PTC bridge coerces a
      zero-arg call to ``{}``, so ``await tools.foo()`` is valid;
    - **schema with parameters** → a typed object argument.
    """
    return_clause = "Promise<unknown>"
    if schema is None:
        return f"async function {camel_name}(input: Record<string, unknown>): {return_clause}"
    props = schema.get("properties") if isinstance(schema, dict) else None
    no_arg_signature = f"async function {camel_name}(): {return_clause}"
    if not isinstance(props, dict) or not props:
        return no_arg_signature
    defs: dict[str, Any] = {}
    for defs_key in ("$defs", "definitions"):
        if isinstance(schema.get(defs_key), dict):
            defs = {**defs, **schema[defs_key]}
    required = set(schema.get("required", []))
    fields = []
    for key, prop in props.items():
        optional = "" if key in required else "?"
        type_str = json_schema_to_ts(prop, defs)
        desc = prop.get("description") if isinstance(prop, dict) else None
        prefix = f"/**\n   *{desc}\n   */ " if desc else ""
        fields.append(f"  {prefix}{key}{optional}: {type_str};")
    body = "\n".join(fields)
    if not body:
        return no_arg_signature
    return f"async function {camel_name}(input: {{\n{body}\n}}): {return_clause}"


def render_signature_block(tool: BaseTool) -> str:
    """Render ``/** description */\\n<signature>`` for a single tool."""
    camel = to_camel_case(tool.name)
    schema = _safe_json_schema(tool)
    description = (tool.description or "").strip().splitlines()[0] if tool.description else ""
    signature = render_signature(camel, schema)
    return f"/** {description} */\n{signature}"


_NAMESPACE_PREAMBLE = (
    "\n\n"
    "### API Reference — `tools` namespace\n\n"
    "Agent tools are exposed on `globalThis.tools` (also reachable as `tools`). Each "
    "takes a single object argument and returns a Promise that resolves to the tool's "
    "native value (strings, numbers, arrays, objects, `null`) — you do NOT need to "
    "`JSON.parse` results.\n\n"
    "Invocation pattern: `await tools.<name>({ ... })`.\n\n"
    "- Use `await` to get tool results; combine with `Promise.all` for independent "
    "calls so they run concurrently.\n"
    "- If the task needs multiple tool calls, prefer one `{tool_name}` invocation that "
    "performs all of them rather than splitting the work across calls — each round-trip "
    "costs a model turn.\n"
    "- Pipeline dependent calls within a single program: if one tool's result feeds "
    "another, chain them instead of returning the intermediate value to the model.\n"
)


def render_tools_namespace(
    tools: Sequence[BaseTool],
    *,
    tool_name: str = "eval",
    discovery_note: str = "",
) -> str:
    """Render the full ``tools`` namespace prompt block.

    ``tools`` are rendered with full ``$ref``-resolved signatures. ``discovery_note``
    (optional) is appended after the preamble to instruct the model that additional,
    unlisted tools are reachable via ``tools.search`` / ``tools.describe``.
    """
    if not tools and not discovery_note:
        return ""
    preamble = _NAMESPACE_PREAMBLE.replace("{tool_name}", tool_name)
    blocks = [render_signature_block(tool) for tool in tools]
    body = "\n\n".join(blocks)
    parts = [preamble]
    if discovery_note:
        parts.append("\n" + discovery_note + "\n")
    if body:
        parts.append("\n```typescript\n" + body + "\n```")
    return "".join(parts)
