"""The PTC signature renderer must resolve ``$ref``/``$defs`` (the type-hint fix).

The upstream ``langchain_quickjs`` renderer degrades nested Pydantic models /
``TypedDict``s to ``unknown`` / ``Record<string, unknown>`` because they appear in the
JSON Schema as ``$ref`` into ``$defs``. Our renderer must expand them so complex tools
(e.g. GitHub) get a correct argument shape.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent_common.core.ptc_signatures import json_schema_to_ts, render_signature_block


def _fn(**kwargs):  # noqa: ANN003, ANN202
    return None


def _tool(name: str, args_schema, description: str = "desc"):
    return StructuredTool.from_function(func=_fn, name=name, description=description, args_schema=args_schema)


def test_nested_model_ref_is_resolved_not_unknown():
    class Author(BaseModel):
        name: str
        date: str

    class Commit(BaseModel):
        message: str
        author: Author

    class Args(BaseModel):
        commits: List[Commit]

    sig = render_signature_block(_tool("x", Args))
    assert "unknown" not in sig.split("Promise<", 1)[0], sig
    assert "message: string" in sig
    assert "author: { name: string; date: string }" in sig
    assert "commits: { message: string; author: { name: string; date: string } }[]" in sig


def test_enum_and_optional_and_literal():
    class Args(BaseModel):
        owner: str = Field(description="Repo owner")
        state: Literal["open", "closed", "all"] = "open"
        sha: Optional[str] = None

    sig = render_signature_block(_tool("github_list_commits", Args))
    assert "githubListCommits" in sig  # camelCased
    assert 'state?: "open" | "closed" | "all"' in sig
    assert "sha?: string | null" in sig
    assert "owner: string" in sig  # required, no '?'


def test_no_param_tool_renders_no_args():
    """A tool with no parameters renders ``()``, not a misleading Record<unknown>.

    Regression: empty/absent ``properties`` fell through to the unknown-schema
    default (``input: Record<string, unknown>``), implying ``githubGetMe`` accepts
    arbitrary args. It takes none.
    """

    class Empty(BaseModel):
        pass

    sig = render_signature_block(_tool("github_get_me", Empty))
    assert "Record<string, unknown>" not in sig
    assert "async function githubGetMe(): Promise<unknown>" in sig


def test_no_param_dict_schema_renders_no_args():
    """Same for MCP tools whose dict schema has empty/absent properties."""
    for schema in ({"type": "object", "properties": {}}, {"type": "object"}):
        sig = render_signature_block(_tool("github_get_me", schema))
        assert "async function githubGetMe(): Promise<unknown>" in sig, schema
        assert "Record<string, unknown>" not in sig


def test_recursive_ref_is_guarded():
    """A self-referential ``$ref`` must terminate, emitting the type name on cycle."""
    defs = {
        "Node": {
            "type": "object",
            "properties": {
                "value": {"type": "integer"},
                "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
            },
        }
    }
    ts = json_schema_to_ts({"$ref": "#/$defs/Node"}, defs)
    # Renders without hanging; the inner self-reference collapses to the type name.
    assert "value?: number" in ts
    assert "children?: Node[]" in ts


def test_dict_args_schema_is_rendered():
    """MCP tools carry a raw JSON-schema dict (not a Pydantic model) on args_schema.

    Regression: these degraded to ``Record<string, unknown>`` because the renderer
    only handled Pydantic models. The dict must be used directly.
    """
    dict_schema = {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repo owner"},
            "repo": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
        },
        "required": ["owner", "repo"],
    }
    sig = render_signature_block(_tool("github_list_issues", dict_schema))
    assert "Record<string, unknown>" not in sig
    assert "owner: string" in sig
    assert "repo: string" in sig
    assert 'state?: "open" | "closed" | "all"' in sig


def test_dict_args_schema_resolves_nested_ref():
    """A dict schema with ``$defs``/``$ref`` (as MCP servers emit) must expand."""
    dict_schema = {
        "type": "object",
        "$defs": {"Label": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "properties": {
            "repo": {"type": "string"},
            "labels": {"type": "array", "items": {"$ref": "#/$defs/Label"}},
        },
        "required": ["repo"],
    }
    sig = render_signature_block(_tool("github_create_issue", dict_schema))
    assert "labels?: { name?: string }[]" in sig


def test_json_schema_to_ts_scalars_and_arrays():
    assert json_schema_to_ts({"type": "string"}, {}) == "string"
    assert json_schema_to_ts({"type": "integer"}, {}) == "number"
    assert json_schema_to_ts({"type": "boolean"}, {}) == "boolean"
    assert json_schema_to_ts({"type": "array", "items": {"type": "string"}}, {}) == "string[]"
    assert json_schema_to_ts({"enum": ["a", "b"]}, {}) == '"a" | "b"'
    # Unresolvable ref → unknown
    assert json_schema_to_ts({"$ref": "#/$defs/Missing"}, {}) == "unknown"


def test_json_schema_to_ts_list_type_renders_union():
    # JSON Schema allows ``type`` to be a list (nullable fields in MCP tool
    # schemas, e.g. GitHub). This must not raise ``unhashable type: 'list'``.
    assert json_schema_to_ts({"type": ["string", "null"]}, {}) == "string | null"
    assert json_schema_to_ts({"type": ["integer", "null"]}, {}) == "number | null"
    # ``array`` member still resolves ``items``.
    assert (
        json_schema_to_ts({"type": ["array", "null"], "items": {"type": "string"}}, {})
        == "string[] | null"
    )
    assert json_schema_to_ts({"type": []}, {}) == "unknown"
