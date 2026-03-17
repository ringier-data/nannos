"""Shared utility functions for agent infrastructure."""

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CleanupLevel(Enum):
    """Progressive schema cleanup levels for Gemini compatibility.

    MINIMAL: Only remove None values (documented Gemini requirement)
    MODERATE: Also remove ALL enum constraints (global state space limit)
    AGGRESSIVE: Also remove format, min/max bounds, and array constraints

    Testing revealed Gemini has a GLOBAL state space limit across all tools:
    - Individual complex tools work fine with enums
    - 80+ tools combined hit the limit (cumulative enum state)
    - Removing ALL enums from all tools solves the issue
    """

    MINIMAL = "minimal"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


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


def clean_schema_properties(
    properties: dict[str, Any],
    level: CleanupLevel = CleanupLevel.MINIMAL,
    tool_name: str | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    """Recursively remove invalid property schemas with progressive cleanup levels.

    MINIMAL: Only removes None values, empty dicts (documented Gemini requirement)
    MODERATE: Also removes ALL enum constraints (global state space limit)
    AGGRESSIVE: Also removes format, min/max bounds, array length constraints

    Args:
        properties: Properties dict from JSON Schema
        level: Cleanup level to apply
        tool_name: Name of the tool (for logging only, not used for targeting)

    Returns:
        Cleaned properties dict
    """
    if not isinstance(properties, dict):
        return properties

    cleaned = {}
    for key, value in properties.items():
        # MINIMAL: Remove None values (documented Gemini requirement)
        if value is None:
            logger.debug(f"Removing property '{key}' with None value")
            continue

        if isinstance(value, dict) and len(value) == 0:
            logger.debug(f"Removing property '{key}' with empty dict")
            continue

        if isinstance(value, dict) and value == {"default": None}:
            logger.debug(f"Removing property '{key}' with default: None")
            continue

        if isinstance(value, dict) and any(v is None for k, v in value.items()):
            logger.debug(f"Removing property '{key}' containing None values: {value}")
            continue

        # Recursively clean nested schemas
        if isinstance(value, dict):
            value_copy = dict(value)

            # MODERATE & AGGRESSIVE: Remove ALL enums (global state space limit)
            if level in (CleanupLevel.MODERATE, CleanupLevel.AGGRESSIVE) and "enum" in value_copy:
                logger.debug(
                    f"[{level.value}] Removing 'enum' constraint from property '{key}' ({len(value_copy['enum'])} values)"
                )
                del value_copy["enum"]

            # AGGRESSIVE: Also remove format and min/max constraints
            if level == CleanupLevel.AGGRESSIVE:
                if "format" in value_copy:
                    logger.debug(f"[{level.value}] Removing 'format' constraint from property '{key}'")
                    del value_copy["format"]
                for constraint in [
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "minItems",
                    "maxItems",
                ]:
                    if constraint in value_copy:
                        logger.debug(f"[{level.value}] Removing '{constraint}' constraint from property '{key}'")
                        del value_copy[constraint]

            # Recursively clean nested properties
            if "properties" in value_copy:
                value_copy["properties"] = clean_schema_properties(
                    value_copy["properties"], level, tool_name, depth + 1
                )
            if "items" in value_copy and isinstance(value_copy["items"], dict):
                if "properties" in value_copy["items"]:
                    value_copy["items"]["properties"] = clean_schema_properties(
                        value_copy["items"]["properties"], level, tool_name, depth + 1
                    )

                # Remove enum from array items (MODERATE & AGGRESSIVE)
                if level in (CleanupLevel.MODERATE, CleanupLevel.AGGRESSIVE) and "enum" in value_copy["items"]:
                    logger.debug(f"[{level.value}] Removing 'enum' from array items in property '{key}'")
                    del value_copy["items"]["enum"]

                # Remove format from array items (AGGRESSIVE only)
                if level == CleanupLevel.AGGRESSIVE and "format" in value_copy["items"]:
                    del value_copy["items"]["format"]

            cleaned[key] = value_copy
        else:
            cleaned[key] = value

    return cleaned


def validate_and_clean_tool_dict(
    tool_dict: dict[str, Any], level: CleanupLevel = CleanupLevel.MINIMAL
) -> dict[str, Any]:
    """Validate and clean tool dict schema for Gemini compatibility.

    Ensures parameters has valid JSON Schema structure and cleans properties
    with None values.

    Args:
        tool_dict: Tool in OpenAI dict format
        level: Cleanup level to apply

    Returns:
        Tool dict with validated and cleaned parameters schema
    """
    # Ensure function key exists
    if "function" not in tool_dict:
        tool_dict = {"function": tool_dict, "type": "function"}

    function_dict = tool_dict["function"]
    parameters = function_dict.get("parameters")

    # Extract tool name for TARGETED enum removal
    tool_name = function_dict.get("name")

    # Ensure parameters has valid structure
    if parameters is None or not isinstance(parameters, dict):
        function_dict["parameters"] = {"type": "object", "properties": {}}
    elif "properties" not in parameters:
        parameters["properties"] = {}

    # Clean invalid properties and sync required array
    if "properties" in function_dict["parameters"]:
        original_props = function_dict["parameters"]["properties"]
        cleaned_props = clean_schema_properties(original_props, level, tool_name)
        function_dict["parameters"]["properties"] = cleaned_props

        # Remove cleaned properties from required array
        if "required" in function_dict["parameters"]:
            function_dict["parameters"]["required"] = [
                r for r in function_dict["parameters"]["required"] if r in cleaned_props
            ]

    return tool_dict
