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


def clean_schema_node(
    node: Any,
    level: CleanupLevel = CleanupLevel.MINIMAL,
    tool_name: str | None = None,
    _path: str = "",
) -> Any:
    """Recursively clean a single JSON Schema node for Gemini compatibility.

    Handles (at ALL levels):
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

    Returns:
        Cleaned schema node
    """
    if not isinstance(node, dict):
        return node

    node = dict(node)  # shallow copy to avoid mutating originals

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
                inner = clean_schema_node(non_null[0], level, tool_name, f"{_path}.anyOf[0]")
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
                    clean_schema_node(s, level, tool_name, f"{_path}.anyOf[{i}]") for i, s in enumerate(non_null)
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
        node["properties"] = clean_schema_properties(node["properties"], level, tool_name, _path)

    if "items" in node and isinstance(node["items"], dict):
        node["items"] = clean_schema_node(node["items"], level, tool_name, f"{_path}.items")

    for keyword in ("allOf", "oneOf"):
        if keyword in node and isinstance(node[keyword], list):
            node[keyword] = [
                clean_schema_node(s, level, tool_name, f"{_path}.{keyword}[{i}]") for i, s in enumerate(node[keyword])
            ]

    return node


def clean_schema_properties(
    properties: dict[str, Any],
    level: CleanupLevel = CleanupLevel.MINIMAL,
    tool_name: str | None = None,
    _path: str = "",
    # Legacy positional arg — older callers pass depth: int as 4th arg
    depth: int = 0,
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
            result = clean_schema_node(value, level, tool_name, prop_path)
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

    # Clean properties and sync required array
    if "properties" in function_dict["parameters"]:
        original_props = function_dict["parameters"]["properties"]
        cleaned_props = clean_schema_properties(original_props, level, tool_name)
        function_dict["parameters"]["properties"] = cleaned_props

        # Remove from required any properties that were cleaned away
        if "required" in function_dict["parameters"]:
            function_dict["parameters"]["required"] = [
                r for r in function_dict["parameters"]["required"] if r in cleaned_props
            ]

    return tool_dict
