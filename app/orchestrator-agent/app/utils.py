"""Shared utility functions for the orchestrator agent.

This module contains utilities that are used across multiple modules
to avoid circular imports.
"""

# Language code to display name mapping
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sl": "Slovenian",
    "sr": "Serbian",
    "uk": "Ukrainian",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "sw": "Swahili",
}


def get_language_display_name(language_code: str) -> str:
    """Get the display name for a language code.

    Args:
        language_code: ISO 639-1 language code (e.g., 'en', 'de', 'fr')

    Returns:
        Human-readable language name, or the code itself if not found
    """
    return LANGUAGE_NAMES.get(language_code.lower(), language_code)


# ============================================================================
# Tool Schema Cleaning for Gemini Compatibility
# ============================================================================
"""
CONTEXT: LangChain's tool conversion and Gemini's strict schema validation
===========================================================================

This section provides workarounds for a schema validation issue when using
LangChain tools with Google's Gemini models via langchain-google-genai.

THE ISSUE:
----------
Some LangChain tools (particularly those from deepagents like FilesystemMiddleware)
have parameters with:
1. None as the annotation type
2. None as the default value
3. Complex types like BaseStore that Pydantic can't serialize

When these tools are converted to OpenAI function format via convert_to_openai_tool(),
the resulting schema contains properties with None values, which Gemini's strict
validation rejects with:

    pydantic_core.ValidationError: 1 validation error for Schema
    properties.runtime
      Input should be a valid dictionary or object to extract fields from
      [type=model_attributes_type, input_value=None, input_type=NoneType]

OpenAI and Claude models accept these schemas, but Gemini does not.

THE SOLUTION:
-------------
Clean dict schemas at model-binding time (not at tool creation time):
  - Remove properties with None values
  - Remove empty dicts and {"default": None}
  - Remove properties containing any None values
  - Sync required array with cleaned properties

This is handled by middleware (DynamicToolDispatchMiddleware and ToolSchemaCleaningMiddleware)
which intercepts model calls and cleans the dict representations of tools before sending
them to the model, while keeping the original BaseTool instances intact for execution.

WHY AT MODEL-BINDING TIME:
--------------------------
- Cleaning at tool creation breaks tool execution (tools need their full schema)
- Cleaning at model-binding time allows us to send clean schemas to the model
  while keeping the original tools intact for ToolNode execution
- Prevents "tool not found" errors that occur when tool schemas are modified in-place

USAGE:
------
For orchestrator:
    # Handled automatically by DynamicToolDispatchMiddleware
    tool_dict = convert_to_openai_tool(tool)
    tool_dict = validate_and_clean_tool_dict(tool_dict)

For sub-agents:
    # Handled automatically by ToolSchemaCleaningMiddleware
    tools = [tool1, tool2, ...]  # Keep as BaseTool instances

TODO: This should be fixed upstream in langchain-google-genai or langchain-core
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def clean_schema_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove invalid property schemas.

    Removes properties with None values, empty dicts, or None in their values
    from dict schemas. This ensures Gemini compatibility.

    Args:
        properties: Properties dict from JSON Schema

    Returns:
        Cleaned properties dict
    """
    if not isinstance(properties, dict):
        return properties

    cleaned = {}
    for key, value in properties.items():
        # Skip None values entirely
        if value is None:
            logger.debug(f"Removing property '{key}' with None value")
            continue

        # Skip empty dicts
        if isinstance(value, dict) and len(value) == 0:
            logger.debug(f"Removing property '{key}' with empty dict")
            continue

        # Skip dicts with only {"default": None}
        if isinstance(value, dict) and value == {"default": None}:
            logger.debug(f"Removing property '{key}' with default: None")
            continue

        # Skip if the dict contains any top-level None values
        if isinstance(value, dict) and any(v is None for k, v in value.items()):
            logger.debug(f"Removing property '{key}' containing None values: {value}")
            continue

        # Recursively clean nested schemas
        if isinstance(value, dict):
            value_copy = dict(value)
            if "properties" in value_copy:
                value_copy["properties"] = clean_schema_properties(value_copy["properties"])
            if "items" in value_copy and isinstance(value_copy["items"], dict) and "properties" in value_copy["items"]:
                value_copy["items"]["properties"] = clean_schema_properties(value_copy["items"]["properties"])
            cleaned[key] = value_copy
        else:
            cleaned[key] = value

    return cleaned


def validate_and_clean_tool_dict(tool_dict: dict[str, Any]) -> dict[str, Any]:
    """Validate and clean tool dict schema for Gemini compatibility.

    Ensures parameters has valid JSON Schema structure and cleans properties
    with None values.

    Args:
        tool_dict: Tool in OpenAI dict format

    Returns:
        Tool dict with validated and cleaned parameters schema
    """
    # Ensure function key exists
    if "function" not in tool_dict:
        tool_dict = {"function": tool_dict, "type": "function"}

    function_dict = tool_dict["function"]
    parameters = function_dict.get("parameters")

    # Ensure parameters has valid structure
    if parameters is None or not isinstance(parameters, dict):
        function_dict["parameters"] = {"type": "object", "properties": {}}
    elif "properties" not in parameters:
        parameters["properties"] = {}

    # Clean invalid properties and sync required array
    if "properties" in function_dict["parameters"]:
        original_props = function_dict["parameters"]["properties"]
        cleaned_props = clean_schema_properties(original_props)
        function_dict["parameters"]["properties"] = cleaned_props

        # Remove cleaned properties from required array
        if "required" in function_dict["parameters"]:
            function_dict["parameters"]["required"] = [
                r for r in function_dict["parameters"]["required"] if r in cleaned_props
            ]

    return tool_dict
