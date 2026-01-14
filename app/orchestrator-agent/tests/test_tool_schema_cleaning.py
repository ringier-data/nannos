"""Tests for tool schema cleaning utilities for Gemini compatibility.

This module tests the workarounds for LangChain/Gemini schema validation issues
where tools with None values are rejected by Gemini's strict validation.
"""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.utils import (
    clean_schema_properties,
    clean_tool_schema,
    clean_tools_for_gemini,
    validate_and_clean_tool_dict,
)


class TestCleanToolSchema:
    """Tests for clean_tool_schema() - Stage 1 Pydantic cleaning."""

    def test_clean_tool_with_none_annotation(self):
        """Test that fields with None annotation are removed."""

        class ArgsSchema(BaseModel):
            valid_field: str = Field(description="A valid field")
            none_field: None = Field(default=None, description="Field with None type")

        tool = StructuredTool(
            name="test_tool",
            description="Test tool",
            func=lambda x: x,
            args_schema=ArgsSchema,
        )

        cleaned_tool = clean_tool_schema(tool)

        # Check that none_field was removed
        assert "valid_field" in cleaned_tool.args_schema.model_fields
        assert "none_field" not in cleaned_tool.args_schema.model_fields

    def test_clean_tool_with_none_default(self):
        """Test that fields with None as default value are removed."""

        class ArgsSchema(BaseModel):
            valid_field: str = Field(description="A valid field")
            default_none: str = Field(default=None, description="Field with None default")

        tool = StructuredTool(
            name="test_tool",
            description="Test tool",
            func=lambda x: x,
            args_schema=ArgsSchema,
        )

        cleaned_tool = clean_tool_schema(tool)

        # Check that default_none was removed
        assert "valid_field" in cleaned_tool.args_schema.model_fields
        assert "default_none" not in cleaned_tool.args_schema.model_fields

    def test_clean_tool_with_no_args_schema(self):
        """Test that tools without args_schema pass through unchanged."""

        # Create a tool without explicit args_schema by using a simple function
        # that will auto-generate one
        class SimpleSchema(BaseModel):
            x: str = Field(description="Input")

        tool = StructuredTool(
            name="test_tool",
            description="Test tool",
            func=lambda x: x,
            args_schema=SimpleSchema,
        )

        # Manually set args_schema to None to test the path
        tool.args_schema = None

        cleaned_tool = clean_tool_schema(tool)

        # Should return the same tool unchanged
        assert cleaned_tool.args_schema is None

    def test_clean_tool_with_valid_fields_only(self):
        """Test that tools with only valid fields pass through unchanged."""

        class ArgsSchema(BaseModel):
            field1: str = Field(description="Field 1")
            field2: int = Field(default=42, description="Field 2")

        tool = StructuredTool(
            name="test_tool",
            description="Test tool",
            func=lambda x, y: x,
            args_schema=ArgsSchema,
        )

        cleaned_tool = clean_tool_schema(tool)

        # All fields should still be present
        assert "field1" in cleaned_tool.args_schema.model_fields
        assert "field2" in cleaned_tool.args_schema.model_fields

    def test_clean_tool_arbitrary_types_allowed(self):
        """Test that cleaned schema has arbitrary_types_allowed=True."""

        # Simulate a schema that would need arbitrary_types_allowed
        # by using a field that should be removed
        class ArgsSchema(BaseModel):
            # Use a problematic field that will be removed
            problematic: None = Field(default=None, description="Problematic field")
            valid_field: str = Field(description="A valid field")

        tool = StructuredTool(
            name="test_tool",
            description="Test tool",
            func=lambda x: x,
            args_schema=ArgsSchema,
        )

        cleaned_tool = clean_tool_schema(tool)

        # Check that arbitrary_types_allowed is set
        assert cleaned_tool.args_schema.model_config.get("arbitrary_types_allowed") is True

        # Problematic field should be removed (has None annotation)
        assert "problematic" not in cleaned_tool.args_schema.model_fields
        assert "valid_field" in cleaned_tool.args_schema.model_fields


class TestCleanSchemaProperties:
    """Tests for clean_schema_properties() - Stage 2 dict cleaning."""

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


class TestCleanToolsForGemini:
    """Tests for clean_tools_for_gemini() - Complete cleaning pipeline."""

    def test_clean_list_of_basetools(self):
        """Test cleaning a list of BaseTool instances."""

        class ArgsSchema(BaseModel):
            valid_field: str = Field(description="Valid")
            none_field: None = Field(default=None, description="None field")

        class SimpleSchema(BaseModel):
            field: str = Field(description="Simple field")

        tool1 = StructuredTool(
            name="tool1",
            description="Tool 1",
            func=lambda x: x,
            args_schema=ArgsSchema,
        )

        tool2 = StructuredTool(
            name="tool2",
            description="Tool 2",
            func=lambda x: x,
            args_schema=SimpleSchema,
        )

        tools = [tool1, tool2]
        cleaned = clean_tools_for_gemini(tools)

        # Should return list of dicts
        assert len(cleaned) == 2
        assert all(isinstance(t, dict) for t in cleaned)

        # Check that none_field was removed from tool1
        tool1_props = cleaned[0]["function"]["parameters"]["properties"]
        assert "valid_field" in tool1_props
        assert "none_field" not in tool1_props

    def test_clean_mixed_list(self):
        """Test cleaning a mixed list of BaseTools and dicts."""

        class ArgsSchema(BaseModel):
            field1: str = Field(description="Field 1")
            none_field: None = Field(default=None, description="None field")

        base_tool = StructuredTool(
            name="base_tool",
            description="Base tool",
            func=lambda x: x,
            args_schema=ArgsSchema,
        )

        dict_tool = {
            "function": {
                "name": "dict_tool",
                "description": "Dict tool",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "valid_prop": {"type": "string"},
                        "none_prop": None,
                    },
                },
            },
            "type": "function",
        }

        tools = [base_tool, dict_tool]
        cleaned = clean_tools_for_gemini(tools)

        # Both should be cleaned
        assert len(cleaned) == 2

        # Check BaseTool cleaning
        base_props = cleaned[0]["function"]["parameters"]["properties"]
        assert "field1" in base_props
        assert "none_field" not in base_props

        # Check dict tool cleaning
        dict_props = cleaned[1]["function"]["parameters"]["properties"]
        assert "valid_prop" in dict_props
        assert "none_prop" not in dict_props

    def test_empty_list(self):
        """Test that empty list returns empty list."""
        cleaned = clean_tools_for_gemini([])
        assert cleaned == []

    def test_complete_two_stage_cleaning(self):
        """Test that both Stage 1 (Pydantic) and Stage 2 (dict) cleaning work together."""

        class ArgsSchema(BaseModel):
            valid_field: str = Field(description="Valid field")
            pydantic_none: None = Field(default=None, description="Removed in Stage 1")
            # This simulates a field that might produce None in dict after conversion
            optional_field: str | None = Field(default=None, description="Has None default")

        tool = StructuredTool(
            name="test_tool",
            description="Test tool",
            func=lambda x: x,
            args_schema=ArgsSchema,
        )

        cleaned = clean_tools_for_gemini([tool])

        # Check that cleaning was applied
        props = cleaned[0]["function"]["parameters"]["properties"]
        assert "valid_field" in props

        # pydantic_none should be removed by Stage 1
        assert "pydantic_none" not in props

        # optional_field should be removed by Stage 1 or Stage 2 (has None default)
        assert "optional_field" not in props

    def test_preserves_tool_metadata(self):
        """Test that tool name, description, and type are preserved."""

        class ArgsSchema(BaseModel):
            field1: str = Field(description="Field 1")

        tool = StructuredTool(
            name="my_tool",
            description="My custom tool",
            func=lambda x: x,
            args_schema=ArgsSchema,
        )

        cleaned = clean_tools_for_gemini([tool])

        assert cleaned[0]["function"]["name"] == "my_tool"
        assert cleaned[0]["function"]["description"] == "My custom tool"
        assert cleaned[0]["type"] == "function"


class TestIntegrationScenarios:
    """Integration tests for real-world scenarios."""

    def test_gemini_problematic_tool(self):
        """Test the exact scenario that causes Gemini validation errors."""

        # Simulate a tool like deepagents FilesystemMiddleware
        class ProblematicArgsSchema(BaseModel):
            path: str = Field(description="File path")
            runtime: None = Field(default=None, description="Runtime parameter that causes issues")

        tool = StructuredTool(
            name="filesystem_tool",
            description="Filesystem operation",
            func=lambda path: path,
            args_schema=ProblematicArgsSchema,
        )

        # Clean the tool
        cleaned = clean_tools_for_gemini([tool])

        # Verify the problematic field is removed
        props = cleaned[0]["function"]["parameters"]["properties"]
        assert "path" in props
        assert "runtime" not in props

        # Verify structure is valid for Gemini
        params = cleaned[0]["function"]["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)

        # Ensure no None values in properties
        for prop_name, prop_value in props.items():
            assert prop_value is not None
            if isinstance(prop_value, dict):
                assert None not in prop_value.values()

    def test_tool_with_complex_nested_schema(self):
        """Test cleaning a tool with complex nested schemas."""

        class NestedArgsSchema(BaseModel):
            name: str = Field(description="Name")
            config: dict = Field(
                default={
                    "enabled": True,
                    "runtime": None,  # This would become None in dict
                },
                description="Config object",
            )

        tool = StructuredTool(
            name="complex_tool",
            description="Complex tool",
            func=lambda name, config: name,
            args_schema=NestedArgsSchema,
        )

        cleaned = clean_tools_for_gemini([tool])

        # Should successfully clean without errors
        assert len(cleaned) == 1
        assert "name" in cleaned[0]["function"]["parameters"]["properties"]

    def test_multiple_tools_with_various_issues(self):
        """Test cleaning multiple tools with different schema issues."""

        class Schema1(BaseModel):
            field: str
            none_field: None = None

        class Schema2(BaseModel):
            field: str

        class Schema3(BaseModel):
            field: str
            optional: str | None = None

        tools = [
            StructuredTool(name="tool1", description="Tool 1", func=lambda x: x, args_schema=Schema1),
            StructuredTool(name="tool2", description="Tool 2", func=lambda x: x, args_schema=Schema2),
            StructuredTool(name="tool3", description="Tool 3", func=lambda x: x, args_schema=Schema3),
        ]

        cleaned = clean_tools_for_gemini(tools)

        # All tools should be successfully cleaned
        assert len(cleaned) == 3

        # Verify tool1 had none_field removed
        tool1_props = cleaned[0]["function"]["parameters"]["properties"]
        assert "field" in tool1_props
        assert "none_field" not in tool1_props

        # Verify tool2 unchanged (no issues)
        tool2_props = cleaned[1]["function"]["parameters"]["properties"]
        assert "field" in tool2_props

        # Verify tool3 had optional field removed
        tool3_props = cleaned[2]["function"]["parameters"]["properties"]
        assert "field" in tool3_props
        assert "optional" not in tool3_props
