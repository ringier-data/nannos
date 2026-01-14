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
Two-stage cleaning process:

Stage 1 - Clean Pydantic Models (before dict conversion):
  - Remove fields with annotation=None
  - Remove fields with default=None
  - Use arbitrary_types_allowed=True to handle complex types like BaseStore
  - This prevents these fields from appearing in the converted dict schema

Stage 2 - Clean Dict Schemas (after conversion):
  - Remove properties with None values that slipped through
  - Remove empty dicts and {"default": None}
  - Remove properties containing any None values
  - This catches anything Stage 1 missed

WHY BOTH STAGES:
----------------
- Stage 1 (Pydantic) is needed because it modifies the tool's args_schema BEFORE
  conversion, ensuring the tool execution matches the schema sent to the model
- Stage 2 (Dict) is needed because convert_to_openai_tool() can still produce
  schemas with None values even after Stage 1 cleaning
- Together they prevent schema mismatches that cause orphaned tool_call_ids

USAGE:
------
For orchestrator middleware (DynamicToolDispatchMiddleware):
    tool = clean_tool_schema(tool)  # Stage 1
    tool_dict = convert_to_openai_tool(tool)
    tool_dict = validate_and_clean_tool_dict(tool_dict)  # Stage 2

For sub-agents (DynamicLocalAgentRunnable):
    tools = clean_tools_for_gemini(tools)  # Both stages

TODO: This should be fixed upstream in langchain-google-genai or langchain-core
"""

import logging
from typing import Any

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, create_model

logger = logging.getLogger(__name__)


def clean_tool_schema(tool: BaseTool) -> BaseTool:
    """Clean tool's args_schema by removing fields with None values (Stage 1).

    Removes fields with None annotations or None default values from the
    Pydantic model BEFORE conversion to dict format. Uses arbitrary_types_allowed
    to handle complex types like BaseStore.

    This is Stage 1 of the cleaning process - modifies the source Pydantic model
    so that both the model schema AND tool execution are consistent.

    Args:
        tool: BaseTool to clean

    Returns:
        Same tool with cleaned args_schema
    """
    if not hasattr(tool, "args_schema") or tool.args_schema is None:
        return tool

    # Check if it's a Pydantic model (has model_fields)
    if not hasattr(tool.args_schema, "model_fields"):
        return tool

    # Get the current schema fields
    schema_fields = tool.args_schema.model_fields
    fields_to_remove = []

    # Remove fields with None annotation OR None default value
    for field_name, field_info in schema_fields.items():
        if field_info.annotation is None or field_info.default is None:
            fields_to_remove.append(field_name)

    # If we have fields to remove, create a new schema without them
    if fields_to_remove:
        remaining_fields = {
            name: (field_info.annotation, field_info)
            for name, field_info in schema_fields.items()
            if name not in fields_to_remove
        }

        # Use arbitrary_types_allowed to handle complex types like BaseStore
        tool.args_schema = create_model(
            f"{tool.name}Args",
            __config__=ConfigDict(arbitrary_types_allowed=True),
            **remaining_fields,
        )

    return tool


def clean_schema_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove invalid property schemas (Stage 2).

    Removes properties with None values, empty dicts, or None in their values
    from dict schemas AFTER conversion. This catches anything Stage 1 missed.

    This is Stage 2 of the cleaning process - cleans the dict schema that will
    be sent to Gemini.

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
    """Validate and clean tool dict schema (Stage 2 helper).

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


def clean_tools_for_gemini(tools: list[Any]) -> list[dict[str, Any]]:
    """Clean tools for Gemini compatibility (both stages).

    This is a convenience function that applies both Stage 1 (Pydantic cleaning)
    and Stage 2 (dict cleaning) to a list of tools.

    Use this in sub-agents or anywhere you need to convert BaseTool instances
    to clean dict format for Gemini.

    Args:
        tools: List of BaseTool instances

    Returns:
        List of cleaned tool dicts in OpenAI format
    """
    cleaned_tools = []

    for tool in tools:
        if isinstance(tool, BaseTool):
            # Stage 1: Clean Pydantic model
            tool = clean_tool_schema(tool)

            # Convert to dict
            tool_dict = convert_to_openai_tool(tool)

            # Stage 2: Clean dict schema
            tool_dict = validate_and_clean_tool_dict(tool_dict)

            cleaned_tools.append(tool_dict)
        else:
            # Already a dict, just validate and clean
            cleaned_tools.append(validate_and_clean_tool_dict(tool))

    return cleaned_tools
