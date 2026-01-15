"""Tests for tool schema cleaning utilities for Gemini compatibility.

This module tests the workarounds for LangChain/Gemini schema validation issues
where tools with None values are rejected by Gemini's strict validation.

Note: Tool schema cleaning is now handled by middleware (ToolSchemaCleaningMiddleware)
at model-binding time. These tests cover the low-level cleaning utilities used by
the middleware.
"""

from app.utils import (
    clean_schema_properties,
    validate_and_clean_tool_dict,
)


class TestCleanSchemaProperties:
    """Tests for clean_schema_properties() - dict cleaning."""

    def test_remove_none_values(self):
        """Test that properties with None values are removed."""
        properties = {
            "valid_prop": {"type": "string", "description": "Valid"},
            "none_prop": None,
            "another_valid": {"type": "integer"},
        }

        cleaned = clean_schema_properties(properties)

        assert "valid_prop" in cleaned
        assert "none_prop" not in cleaned
        assert "another_valid" in cleaned

    def test_remove_empty_dicts(self):
        """Test that empty dict properties are removed."""
        properties = {
            "valid_prop": {"type": "string"},
            "empty_prop": {},
            "another_valid": {"type": "integer"},
        }

        cleaned = clean_schema_properties(properties)

        assert "valid_prop" in cleaned
        assert "empty_prop" not in cleaned
        assert "another_valid" in cleaned

    def test_remove_default_none(self):
        """Test that properties with only {"default": None} are removed."""
        properties = {
            "valid_prop": {"type": "string", "default": "value"},
            "none_default": {"default": None},
            "another_valid": {"type": "integer"},
        }

        cleaned = clean_schema_properties(properties)

        assert "valid_prop" in cleaned
        assert "none_default" not in cleaned
        assert "another_valid" in cleaned

    def test_remove_props_containing_none(self):
        """Test that properties containing None values are removed."""
        properties = {
            "valid_prop": {"type": "string", "description": "Valid"},
            "prop_with_none": {"type": "string", "runtime": None},
            "another_valid": {"type": "integer"},
        }

        cleaned = clean_schema_properties(properties)

        assert "valid_prop" in cleaned
        assert "prop_with_none" not in cleaned
        assert "another_valid" in cleaned

    def test_recursive_cleaning_nested_properties(self):
        """Test that nested properties are cleaned recursively."""
        properties = {
            "object_prop": {
                "type": "object",
                "properties": {
                    "nested_valid": {"type": "string"},
                    "nested_none": None,
                    "deeply_nested": {
                        "type": "object",
                        "properties": {
                            "deep_valid": {"type": "integer"},
                            "deep_none": {"default": None},
                        },
                    },
                },
            },
        }

        cleaned = clean_schema_properties(properties)

        # Check nested cleaning
        nested_props = cleaned["object_prop"]["properties"]
        assert "nested_valid" in nested_props
        assert "nested_none" not in nested_props

        # Check deeply nested cleaning
        deep_props = nested_props["deeply_nested"]["properties"]
        assert "deep_valid" in deep_props
        assert "deep_none" not in deep_props

    def test_recursive_cleaning_array_items(self):
        """Test that array item properties are cleaned recursively."""
        properties = {
            "array_prop": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_valid": {"type": "string"},
                        "item_none": None,
                    },
                },
            },
        }

        cleaned = clean_schema_properties(properties)

        # Check array items cleaning
        item_props = cleaned["array_prop"]["items"]["properties"]
        assert "item_valid" in item_props
        assert "item_none" not in item_props

    def test_non_dict_input_passes_through(self):
        """Test that non-dict inputs are returned unchanged."""
        assert clean_schema_properties("string") == "string"
        assert clean_schema_properties(123) == 123
        assert clean_schema_properties(None) is None


class TestValidateAndCleanToolDict:
    """Tests for validate_and_clean_tool_dict() - Stage 2 validation helper."""

    def test_ensure_function_key(self):
        """Test that function key is added if missing."""
        tool_dict = {
            "name": "test_tool",
            "description": "Test",
            "parameters": {"type": "object", "properties": {}},
        }

        result = validate_and_clean_tool_dict(tool_dict)

        assert "function" in result
        assert "type" in result
        assert result["type"] == "function"
        assert result["function"]["name"] == "test_tool"

    def test_ensure_parameters_structure(self):
        """Test that parameters structure is created if missing."""
        tool_dict = {
            "function": {
                "name": "test_tool",
                "description": "Test",
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict)

        params = result["function"]["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert params["properties"] == {}

    def test_ensure_properties_in_parameters(self):
        """Test that properties key is added if missing."""
        tool_dict = {
            "function": {
                "name": "test_tool",
                "description": "Test",
                "parameters": {"type": "object"},
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict)

        assert "properties" in result["function"]["parameters"]
        assert result["function"]["parameters"]["properties"] == {}

    def test_clean_invalid_properties(self):
        """Test that invalid properties are cleaned."""
        tool_dict = {
            "function": {
                "name": "test_tool",
                "description": "Test",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "valid_prop": {"type": "string"},
                        "none_prop": None,
                        "empty_prop": {},
                    },
                },
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict)

        props = result["function"]["parameters"]["properties"]
        assert "valid_prop" in props
        assert "none_prop" not in props
        assert "empty_prop" not in props

    def test_sync_required_array(self):
        """Test that required array is synced with cleaned properties."""
        tool_dict = {
            "function": {
                "name": "test_tool",
                "description": "Test",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "valid_prop": {"type": "string"},
                        "none_prop": None,
                    },
                    "required": ["valid_prop", "none_prop"],
                },
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict)

        # none_prop should be removed from required since it was cleaned
        required = result["function"]["parameters"]["required"]
        assert "valid_prop" in required
        assert "none_prop" not in required
        assert len(required) == 1
