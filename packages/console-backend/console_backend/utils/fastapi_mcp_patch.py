"""
Patch for fastapi_mcp to fix schema reference resolution.

This patch fixes an issue with resolving schema references in OpenAPI schemas.
See: https://github.com/tadata-org/fastapi_mcp/pull/156
"""

from typing import Any, Dict, Optional, Set

import fastapi_mcp.openapi.utils


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
        if ref_path.startswith("#/components/schemas/"):
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


def apply_patch():
    """Apply the patch to fastapi_mcp."""
    fastapi_mcp.openapi.utils.resolve_schema_references = resolve_schema_references
