"""PTC core-only rendering: expose the full catalog, render only the stable core.

When the exposed MCP catalog is large (the GP agent), the middleware must:
- keep every tool *exposed* (callable bridge) — expose != render;
- render only base/core tools + the ``search``/``describe`` discovery helpers into the
  prompt, with a discovery instruction, so the prompt is stable across turns;
- leave small, fixed sub-agent toolsets fully rendered inline (no discovery helpers).

Also verifies the enabling change: ``wrap_tool_for_ptc`` preserves ``server_name``
metadata so the core-vs-catalog split works on the *wrapped* instances.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

import agent_common.core.graph_utils as gu
from agent_common.core.ptc_discovery import PTC_DESCRIBE_TOOL_NAME, PTC_SEARCH_TOOL_NAME
from agent_common.core.ptc_signatures import render_tools_namespace
from agent_common.middleware.ptc_guard import wrap_tool_for_ptc


@pytest.fixture(autouse=True)
def _ptc_enabled(monkeypatch):
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
    # Lower the threshold so a handful of MCP tools triggers core-only mode.
    monkeypatch.setattr(gu, "PTC_INLINE_RENDER_THRESHOLD", 3)


def _base_tool(name: str) -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(coroutine=_fn, name=name, description=f"{name} description")


def _mcp_tool(name: str, server: str = "github") -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(
        coroutine=_fn, name=name, description=f"{name} description", metadata={"server_name": server}
    )


def _mw():
    return gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[_base_tool("read_file")],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=True,
        backend_supports_execution=False,
    )


def _req(**kwargs):
    return SimpleNamespace(**kwargs)


def test_wrap_tool_for_ptc_preserves_server_name_metadata():
    wrapped = wrap_tool_for_ptc(_mcp_tool("github_list_commits"), risk_scorer=None)
    assert (wrapped.metadata or {}).get("server_name") == "github"


def test_core_only_exposes_full_catalog_plus_discovery_tools():
    """Over threshold: every MCP tool stays exposed AND search/describe are added."""
    mw = _mw()
    catalog = [_mcp_tool(f"mcp_{i}") for i in range(5)]
    collected = mw._collect_ptc_tools(_req(tools=catalog, state={}))
    names = {t.name for t in collected}
    # Expose != render: all MCP tools remain callable.
    assert {f"mcp_{i}" for i in range(5)} <= names
    assert "read_file" in names
    # Discovery helpers pinned in.
    assert PTC_SEARCH_TOOL_NAME in names
    assert PTC_DESCRIBE_TOOL_NAME in names


def test_core_only_renders_core_only_with_discovery_note():
    mw = _mw()
    catalog = [_mcp_tool(f"mcp_{i}") for i in range(5)]
    collected = mw._collect_ptc_tools(_req(tools=catalog, state={}))
    render_set, note = mw._render_partition(collected)
    render_names = {t.name for t in render_set}
    # The volatile MCP catalog is exposed but NOT rendered.
    assert not any(n.startswith("mcp_") for n in render_names), render_names
    # The stable core IS rendered.
    assert "read_file" in render_names
    assert {PTC_SEARCH_TOOL_NAME, PTC_DESCRIBE_TOOL_NAME} <= render_names
    # Discovery instruction is present.
    assert note
    assert PTC_SEARCH_TOOL_NAME in note and PTC_DESCRIBE_TOOL_NAME in note


def test_small_catalog_renders_all_inline_without_discovery():
    """Under threshold (sub-agent): render everything, no discovery helpers."""
    mw = _mw()
    catalog = [_mcp_tool("mcp_a"), _mcp_tool("mcp_b")]  # 2 <= threshold(3)
    collected = mw._collect_ptc_tools(_req(tools=catalog, state={}))
    names = {t.name for t in collected}
    assert PTC_SEARCH_TOOL_NAME not in names
    assert PTC_DESCRIBE_TOOL_NAME not in names
    render_set, note = mw._render_partition(collected)
    render_names = {t.name for t in render_set}
    # Everything rendered inline, no discovery note.
    assert {"mcp_a", "mcp_b", "read_file"} <= render_names
    assert note == ""


def test_rendered_block_excludes_catalog_signatures():
    """The rendered prompt body must not contain unrendered MCP tool signatures."""
    mw = _mw()
    catalog = [_mcp_tool(f"mcp_tool_{i}") for i in range(5)]
    collected = mw._collect_ptc_tools(_req(tools=catalog, state={}))
    render_set, note = mw._render_partition(collected)
    body = render_tools_namespace(render_set, tool_name="eval", discovery_note=note)
    assert "mcpTool" not in body  # no camelCased MCP signatures leaked
    assert "async function search" in body
    assert "async function describe" in body


def test_render_partition_stable_across_turns_with_different_catalog():
    """Core render set is identical when only the (unrendered) MCP catalog changes."""
    mw = _mw()
    turn1 = mw._collect_ptc_tools(_req(tools=[_mcp_tool(f"a_{i}") for i in range(5)], state={}))
    turn2 = mw._collect_ptc_tools(_req(tools=[_mcp_tool(f"b_{i}") for i in range(6)], state={}))
    set1 = {t.name for t in mw._render_partition(turn1)[0]}
    set2 = {t.name for t in mw._render_partition(turn2)[0]}
    assert set1 == set2, f"core render set drifted across turns: {set1} vs {set2}"
