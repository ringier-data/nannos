"""Tests for tool schema cleaning utilities for Gemini compatibility.

This module tests the workarounds for LangChain/Gemini schema validation issues
where tools with None values are rejected by Gemini's strict validation.

Progressive Cleanup Strategy Tests:
- MINIMAL: Remove None values only (documented requirement)
- MODERATE: Also remove ALL enums (global state space limit)
- AGGRESSIVE: Also remove format/min/max/array constraints

Note: Tool schema cleaning is now handled by middleware (ToolSchemaCleaningMiddleware)
at model-binding time. These tests cover the low-level cleaning utilities used by
the middleware.
"""

from app.utils import (
    CleanupLevel,
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


class TestCleanupLevels:
    """Tests for progressive cleanup levels (MINIMAL -> MODERATE -> AGGRESSIVE)."""

    def test_minimal_cleanup_removes_none_only(self):
        """MINIMAL should remove None values but preserve enums and constraints."""
        properties = {
            "status": {"type": "string", "enum": ["open", "closed"], "description": "Status"},
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
            },
            "email": {
                "type": "string",
                "format": "email",
            },
            "runtime": None,  # Should be removed
        }

        cleaned = clean_schema_properties(properties, level=CleanupLevel.MINIMAL)

        # None removed
        assert "runtime" not in cleaned

        # Enums preserved
        assert "status" in cleaned
        assert "enum" in cleaned["status"]
        assert cleaned["status"]["enum"] == ["open", "closed"]

        # Min/max preserved
        assert "count" in cleaned
        assert "minimum" in cleaned["count"]
        assert "maximum" in cleaned["count"]

        # Format preserved
        assert "email" in cleaned
        assert "format" in cleaned["email"]

    def test_moderate_cleanup_removes_enums(self):
        """MODERATE should remove None values AND all enum constraints."""
        properties = {
            "status": {"type": "string", "enum": ["open", "closed", "pending"], "description": "Status"},
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE"],
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
            },
            "email": {
                "type": "string",
                "format": "email",
            },
            "runtime": None,
        }

        cleaned = clean_schema_properties(properties, level=CleanupLevel.MODERATE)

        # None removed
        assert "runtime" not in cleaned

        # Enums removed
        assert "status" in cleaned
        assert "enum" not in cleaned["status"]
        assert "method" in cleaned
        assert "enum" not in cleaned["method"]

        # Min/max preserved (not removed in MODERATE)
        assert "count" in cleaned
        assert "minimum" in cleaned["count"]
        assert "maximum" in cleaned["count"]

        # Format preserved (not removed in MODERATE)
        assert "email" in cleaned
        assert "format" in cleaned["email"]

    def test_aggressive_cleanup_removes_everything(self):
        """AGGRESSIVE should remove None, enums, format, and min/max constraints."""
        properties = {
            "status": {
                "type": "string",
                "enum": ["open", "closed"],
                "format": "lowercase",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "exclusiveMinimum": 0,
                "exclusiveMaximum": 101,
            },
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
            },
            "runtime": None,
        }

        cleaned = clean_schema_properties(properties, level=CleanupLevel.AGGRESSIVE)

        # None removed
        assert "runtime" not in cleaned

        # Enums removed
        assert "status" in cleaned
        assert "enum" not in cleaned["status"]

        # Format removed
        assert "format" not in cleaned["status"]

        # Min/max removed
        assert "count" in cleaned
        assert "minimum" not in cleaned["count"]
        assert "maximum" not in cleaned["count"]
        assert "exclusiveMinimum" not in cleaned["count"]
        assert "exclusiveMaximum" not in cleaned["count"]

        # Array constraints removed
        assert "items" in cleaned
        assert "minItems" not in cleaned["items"]
        assert "maxItems" not in cleaned["items"]

    def test_moderate_removes_enum_from_nested_properties(self):
        """MODERATE should recursively remove enums from nested objects."""
        properties = {
            "filter": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "inactive"],
                    },
                    "type": {
                        "type": "string",
                        "enum": ["user", "admin"],
                    },
                },
            },
        }

        cleaned = clean_schema_properties(properties, level=CleanupLevel.MODERATE)

        nested = cleaned["filter"]["properties"]
        assert "status" in nested
        assert "enum" not in nested["status"]
        assert "type" in nested
        assert "enum" not in nested["type"]

    def test_moderate_removes_enum_from_array_items(self):
        """MODERATE should remove enums from array item schemas."""
        properties = {
            "tags": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["bug", "feature", "enhancement"],
                },
            },
        }

        cleaned = clean_schema_properties(properties, level=CleanupLevel.MODERATE)

        assert "tags" in cleaned
        assert "items" in cleaned["tags"]
        assert "enum" not in cleaned["tags"]["items"]

    def test_aggressive_removes_format_from_array_items(self):
        """AGGRESSIVE should remove format from array item schemas."""
        properties = {
            "emails": {
                "type": "array",
                "items": {
                    "type": "string",
                    "format": "email",
                },
            },
        }

        cleaned = clean_schema_properties(properties, level=CleanupLevel.AGGRESSIVE)

        assert "emails" in cleaned
        assert "items" in cleaned["emails"]
        assert "format" not in cleaned["emails"]["items"]

    def test_cleanup_preserves_essential_properties(self):
        """All cleanup levels should preserve essential schema properties."""
        properties = {
            "name": {
                "type": "string",
                "description": "User name",
            },
            "age": {
                "type": "integer",
                "description": "User age",
            },
        }

        for level in [CleanupLevel.MINIMAL, CleanupLevel.MODERATE, CleanupLevel.AGGRESSIVE]:
            cleaned = clean_schema_properties(properties, level=level)

            assert "name" in cleaned
            assert cleaned["name"]["type"] == "string"
            assert cleaned["name"]["description"] == "User name"

            assert "age" in cleaned
            assert cleaned["age"]["type"] == "integer"
            assert cleaned["age"]["description"] == "User age"


class TestValidateAndCleanToolDictWithLevels:
    """Tests for validate_and_clean_tool_dict with cleanup levels."""

    def test_minimal_level_preserves_enums_in_tool(self):
        """MINIMAL level should preserve enum constraints in tool schema."""
        tool_dict = {
            "function": {
                "name": "search_issues",
                "description": "Search issues",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["open", "closed"],
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["created", "updated", "comments"],
                        },
                    },
                },
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict, level=CleanupLevel.MINIMAL)

        props = result["function"]["parameters"]["properties"]
        assert "enum" in props["state"]
        assert "enum" in props["sort"]

    def test_moderate_level_removes_enums_from_tool(self):
        """MODERATE level should remove all enum constraints from tool schema."""
        tool_dict = {
            "function": {
                "name": "search_issues",
                "description": "Search issues",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["open", "closed"],
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["created", "updated", "comments"],
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                },
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict, level=CleanupLevel.MODERATE)

        props = result["function"]["parameters"]["properties"]
        # Enums removed
        assert "enum" not in props["state"]
        assert "enum" not in props["sort"]
        # Min/max preserved in MODERATE
        assert "minimum" in props["count"]
        assert "maximum" in props["count"]

    def test_aggressive_level_removes_all_constraints(self):
        """AGGRESSIVE level should remove enums, format, and min/max constraints."""
        tool_dict = {
            "function": {
                "name": "create_user",
                "description": "Create a user",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "role": {
                            "type": "string",
                            "enum": ["user", "admin"],
                        },
                        "email": {
                            "type": "string",
                            "format": "email",
                        },
                        "age": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 150,
                        },
                    },
                },
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict, level=CleanupLevel.AGGRESSIVE)

        props = result["function"]["parameters"]["properties"]
        # All constraints removed
        assert "enum" not in props["role"]
        assert "format" not in props["email"]
        assert "minimum" not in props["age"]
        assert "maximum" not in props["age"]

    def test_tool_name_extracted_for_logging(self):
        """Test that tool name is extracted and passed through (for logging)."""
        tool_dict = {
            "function": {
                "name": "test_tool",
                "description": "Test",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["active"],
                        },
                    },
                },
            },
            "type": "function",
        }

        # Should not raise any errors, tool_name is extracted internally
        result = validate_and_clean_tool_dict(tool_dict, level=CleanupLevel.MODERATE)
        assert result["function"]["name"] == "test_tool"


class TestRealWorldScenarios:
    """Tests with realistic tool schemas from GitHub API and other sources."""

    def test_github_issue_write_tool_moderate(self):
        """Test MODERATE cleanup on complex GitHub tool with multiple enums."""
        tool_dict = {
            "function": {
                "name": "github_issue_write",
                "description": "Create or update issue",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["create", "update"],
                        },
                        "state": {
                            "type": "string",
                            "enum": ["open", "closed"],
                        },
                        "state_reason": {
                            "type": "string",
                            "enum": ["completed", "not_planned", "duplicate"],
                        },
                        "repo": {
                            "type": "string",
                            "description": "Repository name",
                        },
                    },
                    "required": ["method", "repo"],
                },
            },
            "type": "function",
        }

        result = validate_and_clean_tool_dict(tool_dict, level=CleanupLevel.MODERATE)

        props = result["function"]["parameters"]["properties"]
        # All enums removed
        assert "enum" not in props["method"]
        assert "enum" not in props["state"]
        assert "enum" not in props["state_reason"]
        # Other properties preserved
        assert props["repo"]["type"] == "string"
        assert props["repo"]["description"] == "Repository name"

    def test_80_plus_tools_scenario(self):
        """Simulate the 80+ GitHub tools scenario where MODERATE solves the issue."""
        # Simulate multiple tools with enums (cumulative state space)
        tools = []
        for i in range(85):
            tool = {
                "function": {
                    "name": f"github_tool_{i}",
                    "description": f"Tool {i}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "method": {
                                "type": "string",
                                "enum": ["get", "post", "put", "delete"],
                            },
                            "format": {
                                "type": "string",
                                "enum": ["json", "xml"],
                            },
                        },
                    },
                },
                "type": "function",
            }
            tools.append(tool)

        # Clean all tools with MODERATE (removes enums)
        cleaned_tools = [validate_and_clean_tool_dict(tool, level=CleanupLevel.MODERATE) for tool in tools]

        # Verify all enums removed
        for cleaned in cleaned_tools:
            props = cleaned["function"]["parameters"]["properties"]
            assert "enum" not in props["method"]
            assert "enum" not in props["format"]

        # Verify we still have 85 valid tools
        assert len(cleaned_tools) == 85
