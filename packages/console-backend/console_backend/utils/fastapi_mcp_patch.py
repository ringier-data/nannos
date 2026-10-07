"""
Patches for fastapi_mcp.

- Schema reference resolution (https://github.com/tadata-org/fastapi_mcp/pull/156).
- Optional parameters accept ``null``: fastapi_mcp writes a top-level ``type`` next to an
  ``X | None`` field's ``anyOf: [X, null]``. Both apply, so the MCP server's own input
  validation refused an explicit null that FastAPI would accept (a bug report's
  ``task_id: null`` failed after the user approved the call). The injected ``type`` is
  removed from nullable properties, and a ``None`` query parameter is left out of the
  forwarded request (httpx would send it as an empty string); a body ``null`` stays a
  JSON null, so "clear this field" reaches the route.
"""

from typing import Any, Dict, Optional, Set

import fastapi_mcp.openapi.convert
import fastapi_mcp.openapi.utils
import fastapi_mcp.server


def resolve_schema_references(
    schema_part: Dict[str, Any],
    reference_schema: Dict[str, Any],
    seen: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Resolve schema references in OpenAPI schemas.

    Args:
        schema_part: The part of the schema being processed that may contain references
        reference_schema: The complete schema used to resolve references from
        seen: A set of already seen references to avoid infinite recursion

    Returns:
        The schema with references resolved
    """
    if seen is None:
        seen = set()

    # Make a copy to avoid modifying the input schema
    schema_part = schema_part.copy()

    # Handle $ref directly in the schema
    if "$ref" in schema_part:
        ref_path = schema_part["$ref"]
        # Standard OpenAPI references are in the format "#/components/schemas/ModelName"
        if isinstance(ref_path, str) and ref_path.startswith("#/components/schemas/"):
            if ref_path in seen:
                # Return a simple type to avoid infinite recursion
                return {"type": "object"}
            seen.add(ref_path)
            model_name = ref_path.split("/")[-1]
            if "components" in reference_schema and "schemas" in reference_schema["components"]:
                if model_name in reference_schema["components"]["schemas"]:
                    # Replace with the resolved schema
                    ref_schema = reference_schema["components"]["schemas"][model_name].copy()
                    # Remove the $ref key and merge with the original schema
                    schema_part.pop("$ref")
                    schema_part.update(ref_schema)
                    # Recursively resolve any references in the newly inlined schema
                    schema_part = resolve_schema_references(schema_part, reference_schema, seen.copy())

    # Recursively resolve references in all dictionary values
    for key, value in list(schema_part.items()):
        if isinstance(value, dict):
            schema_part[key] = resolve_schema_references(value, reference_schema, seen.copy())
        elif isinstance(value, list):
            # Only process list items that are dictionaries since only they can contain refs
            schema_part[key] = [
                resolve_schema_references(item, reference_schema, seen.copy()) if isinstance(item, dict) else item
                for item in value
            ]

    return schema_part


def _takes_null(schema: Dict[str, Any]) -> bool:
    return any(
        isinstance(alt, dict) and alt.get("type") == "null" for key in ("anyOf", "oneOf") for alt in schema.get(key) or []
    )


def nullable_properties_accept_null(tools: Any) -> Any:
    """Drop the injected top-level ``type`` from every property whose union includes null."""
    for tool in tools:
        properties = (getattr(tool, "inputSchema", None) or {}).get("properties") or {}
        for prop in properties.values():
            if isinstance(prop, dict) and "type" in prop and _takes_null(prop):
                del prop["type"]
    return tools


_original_convert = fastapi_mcp.openapi.convert.convert_openapi_to_mcp_tools
_original_request = fastapi_mcp.server.FastApiMCP._request


def _convert_openapi_to_mcp_tools(*args: Any, **kwargs: Any) -> Any:
    tools, operation_map = _original_convert(*args, **kwargs)
    return nullable_properties_accept_null(tools), operation_map


async def _request_without_null_query(self: Any, client: Any, method: str, path: str, query: Dict[str, Any], *rest: Any) -> Any:
    return await _original_request(
        self, client, method, path, {k: v for k, v in query.items() if v is not None}, *rest
    )


def apply_patch():
    """Apply the patches to fastapi_mcp."""
    fastapi_mcp.openapi.utils.resolve_schema_references = resolve_schema_references
    # ``server`` imported the converter by name, so it is patched where it is used.
    fastapi_mcp.server.convert_openapi_to_mcp_tools = _convert_openapi_to_mcp_tools
    fastapi_mcp.server.FastApiMCP._request = _request_without_null_query
