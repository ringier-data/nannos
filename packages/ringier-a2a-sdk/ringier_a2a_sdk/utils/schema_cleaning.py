"""Tool schema cleaning utilities for Gemini compatibility.

Progressive schema cleanup levels for handling Gemini's strict tool validation.
"""

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CleanupLevel(Enum):
    """Progressive schema cleanup levels for Gemini compatibility.

    MINIMAL: Remove None values + unwrap anyOf nullable types (Pydantic Optional)
    MODERATE: Also remove ALL enum constraints (global state space limit)
    AGGRESSIVE: Also remove format, min/max bounds, and array constraints

    Testing revealed Gemini has a GLOBAL state space limit across all tools:
    - Individual complex tools work fine with enums
    - 80+ tools combined hit the limit (cumulative enum state)
    - Removing ALL enums from all tools solves the issue

    anyOf nullable unwrapping is applied at ALL levels because Gemini's gRPC
    type enum does not include a NULL value, making Optional fields unparseable.
    """

    MINIMAL = "minimal"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


def _is_null_schema(schema: Any) -> bool:
    """Return True if this schema node represents the JSON Schema null type."""
    return isinstance(schema, dict) and schema.get("type") == "null"


# Guards runaway inlining of self-referential schemas (e.g. a tree node whose def
# refers back to itself). Past this depth along a single $ref chain we stop expanding
# and emit a permissive object instead — Gemini has no recursion support anyway.
_MAX_REF_DEPTH = 8


def _collect_defs(schema: Any) -> dict[str, Any]:
    """Gather the ``$defs`` / ``definitions`` table from a root schema node.

    Pydantic v2 emits ``$defs`` (2020-12); older generators emit ``definitions``
    (draft-7). Both are merged so ``$ref`` resolution works regardless of dialect.
    """
    if not isinstance(schema, dict):
        return {}
    defs: dict[str, Any] = {}
    for key in ("$defs", "definitions"):
        table = schema.get(key)
        if isinstance(table, dict):
            defs.update(table)
    return defs


def _resolve_ref(node: dict[str, Any], defs: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Resolve a ``{"$ref": "#/$defs/Name"}`` node against ``defs``.

    Returns ``(resolved_schema, ref_name)``; ``ref_name`` is ``None`` when the ref
    can't be resolved, so the caller can fall back to a permissive node.
    """
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node, None
    # Refs look like "#/$defs/Name" or "#/definitions/Name" — take the final segment.
    name = ref.rsplit("/", 1)[-1]
    target = defs.get(name)
    if isinstance(target, dict):
        return target, name
    return node, None


def clean_schema_node(
    node: Any,
    level: CleanupLevel = CleanupLevel.MINIMAL,
    tool_name: str | None = None,
    _path: str = "",
    *,
    defs: dict[str, Any] | None = None,
    _seen: frozenset[str] = frozenset(),
    _depth: int = 0,
) -> Any:
    """Recursively clean a single JSON Schema node for Gemini compatibility.

    Handles (at ALL levels):
    - $ref: inlines the referenced ``$defs``/``definitions`` entry, dropping the
      ``$ref`` (Vertex's function-calling validator rejects ``$ref``/``$defs`` —
      it tries to resolve the ref name against function display_names and 400s with
      "The referenced name `#/$defs/X` ... does not match to a display_name").
      Self-referential refs collapse to a permissive object past ``_MAX_REF_DEPTH``.
    - anyOf with {type: "null"}: unwraps Pydantic Optional[X] → X
      e.g. anyOf: [{type: string}, {type: null}] → {type: string}
    - None-valued dict fields are stripped
    - default: null is dropped after nullable unwrap

    MODERATE additionally:
    - Removes all enum constraints (global state space limit with 80+ tools)

    AGGRESSIVE additionally:
    - Removes format, minimum/maximum, exclusiveMinimum/Maximum, minItems/maxItems

    Recurses into: properties, items, anyOf, allOf, oneOf

    Args:
        node: A JSON Schema node (dict, list, or scalar — non-dicts pass through)
        level: Cleanup level to apply
        tool_name: Name of the tool (for logging only)
        _path: Current JSON path (for logging)
        defs: The root ``$defs``/``definitions`` table used to resolve ``$ref`` nodes.
            Captured automatically by ``clean_gemini_schema`` / ``validate_and_clean_tool_dict``.
        _seen: ref names already inlined on the current chain (cycle guard)
        _depth: current $ref-expansion depth (cycle guard)

    Returns:
        Cleaned schema node
    """
    if not isinstance(node, dict):
        return node

    node = dict(node)  # shallow copy to avoid mutating originals

    # --- $ref: inline the referenced definition (Gemini can't resolve $ref/$defs) ---
    if "$ref" in node:
        resolved, name = _resolve_ref(node, defs or {})
        if name is None or name in _seen or _depth >= _MAX_REF_DEPTH:
            # Unresolvable, cyclic, or too deep — emit a permissive object so Vertex
            # gets a valid node instead of a dangling $ref. Keep any sibling metadata.
            logger.debug(f"[{level.value}] Dropping unresolved/cyclic $ref at '{_path or 'root'}': {node.get('$ref')}")
            fallback = {k: v for k, v in node.items() if k != "$ref"}
            fallback.setdefault("type", "object")
            return clean_schema_node(fallback, level, tool_name, _path, defs=defs, _seen=_seen, _depth=_depth)
        # Merge sibling metadata (description/title carried alongside $ref) onto the
        # target without letting it clobber the definition's own fields.
        merged = dict(resolved)
        for k, v in node.items():
            if k != "$ref":
                merged.setdefault(k, v)
        return clean_schema_node(
            merged, level, tool_name, _path, defs=defs, _seen=_seen | {name}, _depth=_depth + 1
        )

    # A node may carry its own $defs/definitions (e.g. the root parameters) — drop them
    # after resolution so nothing leaks the table to Vertex.
    for table_key in ("$defs", "definitions"):
        node.pop(table_key, None)

    # --- Handle anyOf: unwrap Pydantic Optional[X] and clean remaining variants ---
    if "anyOf" in node:
        raw_any_of = node.get("anyOf", [])
        if isinstance(raw_any_of, list):
            non_null = [s for s in raw_any_of if not _is_null_schema(s)]
            if len(non_null) == 0:
                # Degenerate: all entries are null-type — drop the anyOf entirely
                del node["anyOf"]
            elif len(non_null) == 1:
                # Standard Pydantic Optional[X]: unwrap to just X.
                # Merge outer metadata (title, description, …) into inner schema.
                inner = clean_schema_node(
                    non_null[0], level, tool_name, f"{_path}.anyOf[0]", defs=defs, _seen=_seen, _depth=_depth
                )
                # Outer fields win for metadata, but we skip anyOf and default: null
                for k, v in node.items():
                    if k == "anyOf":
                        continue
                    if k == "default" and v is None:
                        continue  # drop default: null — null is no longer a valid value
                    inner[k] = v
                return inner
            else:
                # Multiple non-null variants: clean each and keep anyOf (no null entries)
                node["anyOf"] = [
                    clean_schema_node(s, level, tool_name, f"{_path}.anyOf[{i}]", defs=defs, _seen=_seen, _depth=_depth)
                    for i, s in enumerate(non_null)
                ]

    # --- MINIMAL: Strip None-valued fields ---
    node = {k: v for k, v in node.items() if v is not None}

    # Drop bare default: null (may appear without anyOf)
    if "default" in node and node["default"] is None:
        del node["default"]

    # --- MODERATE & AGGRESSIVE: Remove enum constraints ---
    if level in (CleanupLevel.MODERATE, CleanupLevel.AGGRESSIVE) and "enum" in node:
        logger.debug(f"[{level.value}] Removing 'enum' at path '{_path or 'root'}'")
        del node["enum"]

    # --- AGGRESSIVE: Remove format and numeric/array constraints ---
    if level == CleanupLevel.AGGRESSIVE:
        for field in ("format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems"):
            node.pop(field, None)

    # --- Recurse into nested schemas ---
    if "properties" in node and isinstance(node["properties"], dict):
        node["properties"] = clean_schema_properties(
            node["properties"], level, tool_name, _path, defs=defs, _seen=_seen, _depth=_depth
        )

    if "items" in node and isinstance(node["items"], dict):
        node["items"] = clean_schema_node(
            node["items"], level, tool_name, f"{_path}.items", defs=defs, _seen=_seen, _depth=_depth
        )

    for keyword in ("allOf", "oneOf"):
        if keyword in node and isinstance(node[keyword], list):
            node[keyword] = [
                clean_schema_node(s, level, tool_name, f"{_path}.{keyword}[{i}]", defs=defs, _seen=_seen, _depth=_depth)
                for i, s in enumerate(node[keyword])
            ]

    return node


def clean_schema_properties(
    properties: dict[str, Any],
    level: CleanupLevel = CleanupLevel.MINIMAL,
    tool_name: str | None = None,
    _path: str = "",
    *,
    defs: dict[str, Any] | None = None,
    _seen: frozenset[str] = frozenset(),
    _depth: int = 0,
) -> dict[str, Any]:
    """Recursively remove invalid property schemas with progressive cleanup levels.

    MINIMAL: Removes None values, empty dicts, and unwraps anyOf nullable types
    MODERATE: Also removes ALL enum constraints (global state space limit)
    AGGRESSIVE: Also removes format, min/max bounds, array length constraints

    Args:
        properties: Properties dict from JSON Schema
        level: Cleanup level to apply
        tool_name: Name of the tool (for logging only)

    Returns:
        Cleaned properties dict
    """
    if not isinstance(properties, dict):
        return properties

    cleaned = {}
    for key, value in properties.items():
        prop_path = f"{_path}.{key}" if _path else key

        # Remove None-valued and empty properties
        if value is None:
            logger.debug(f"Removing property '{key}' with None value")
            continue
        if isinstance(value, dict) and not value:
            logger.debug(f"Removing property '{key}' with empty dict")
            continue

        # Delegate full recursive cleaning to clean_schema_node
        if isinstance(value, dict):
            result = clean_schema_node(value, level, tool_name, prop_path, defs=defs, _seen=_seen, _depth=_depth)
            if not result:
                # Schema reduced to empty dict (e.g. {"default": None}) — skip it
                logger.debug(f"Removing property '{key}': schema reduced to empty after cleaning")
                continue
            cleaned[key] = result
        else:
            cleaned[key] = value

    return cleaned


def validate_and_clean_tool_dict(
    tool_dict: dict[str, Any], level: CleanupLevel = CleanupLevel.MINIMAL
) -> dict[str, Any] | None:
    """Validate and clean tool dict schema for Gemini compatibility.

    Ensures parameters has valid JSON Schema structure and cleans properties
    with None values, anyOf nullable types, and level-specific constraints.

    Args:
        tool_dict: Tool in OpenAI dict format
        level: Cleanup level to apply

    Returns:
        Tool dict with validated and cleaned parameters schema, or None if invalid
    """
    # Ensure function key exists
    if "function" not in tool_dict:
        tool_dict = {"function": tool_dict, "type": "function"}

    function_dict = tool_dict["function"]

    # CRITICAL: Validate that function dict has a 'name' field
    # This is required by both OpenAI and Bedrock tool formats
    tool_name = function_dict.get("name")
    if not tool_name or not isinstance(tool_name, str):
        logger.error(
            f"Tool validation failed: function dict missing required 'name' field. "
            f"Function keys: {list(function_dict.keys())}"
        )
        return None

    parameters = function_dict.get("parameters")

    # Ensure parameters has valid structure
    if parameters is None or not isinstance(parameters, dict):
        function_dict["parameters"] = {"type": "object", "properties": {}}
    elif "properties" not in parameters:
        parameters["properties"] = {}

    params = function_dict["parameters"]

    # Capture the root $defs/definitions table so nested $ref nodes can be inlined,
    # then drop the table itself — Vertex rejects both $ref and $defs.
    defs = _collect_defs(params)
    for table_key in ("$defs", "definitions"):
        params.pop(table_key, None)

    # Clean properties and sync required array
    if "properties" in params:
        original_props = params["properties"]
        cleaned_props = clean_schema_properties(original_props, level, tool_name, defs=defs)
        params["properties"] = cleaned_props

        # Remove from required any properties that were cleaned away
        if "required" in params:
            params["required"] = [r for r in params["required"] if r in cleaned_props]

    return tool_dict


def clean_gemini_schema(schema: Any, level: CleanupLevel = CleanupLevel.MINIMAL) -> Any:
    """Clean an arbitrary JSON Schema for Gemini/Vertex compatibility.

    Captures the root ``$defs``/``definitions`` table, inlines every ``$ref`` against
    it, drops the table, and applies the same node cleaning as the tool-dict path
    (nullable ``anyOf`` unwrap, None stripping, level-specific constraint removal).

    Use this for schemas that aren't OpenAI tool dicts — e.g. an MCP tool's
    ``inputSchema``/``outputSchema`` echoed back to the agent — where a leftover
    ``$ref`` would otherwise reach Vertex and 400.
    """
    if not isinstance(schema, dict):
        return schema
    defs = _collect_defs(schema)
    cleaned = clean_schema_node(schema, level, defs=defs)
    # clean_schema_node already strips $defs/definitions from the node it returns.
    return cleaned
