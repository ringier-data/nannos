"""The orchestrator's ``tools.*`` block always says how much of the user's catalog it is NOT.

The console's MCP toggles narrow only the orchestrator's own whitelist; the general-purpose
sub-agent always gets the unfiltered registry. Under the render threshold the namespace is
rendered inline as a closed list, and with nothing saying otherwise the model read it as
"all there is" (2026-09-24: GitHub tools toggled off → the orchestrator searched its memory
for the GitHub username and asked the user, instead of delegating). The gap note must be
present in BOTH render modes, carry the numbers, and disappear only when nothing is hidden
(catalog-mode sub-agents) or when there is no registry-backed namespace at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.tools import StructuredTool

import agent_common.core.graph_utils as gu

DISCOVERY_MARKER = "Many more tools are available but NOT listed here"
GAP_MARKER = "is a slice of this user's MCP tool catalog"


@pytest.fixture(autouse=True)
def _ptc_enabled(monkeypatch):
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
    # Lower the threshold so a handful of enabled MCP tools flips the render mode.
    monkeypatch.setattr(gu, "PTC_INLINE_RENDER_THRESHOLD", 3)


def _tool(name: str, server: str | None = "github") -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(
        coroutine=_fn,
        name=name,
        description=f"{name} description",
        metadata={"server_name": server} if server else None,
    )


def _mw(*, expose_context_registry: bool = True):
    """The orchestrator's shape: no request-tools harvest, registry routed from the context."""
    return gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[_tool("get_current_time", server=None)],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=False,
        expose_context_registry=expose_context_registry,
        backend_supports_execution=False,
    )


def _registry(mcp_count: int) -> dict[str, StructuredTool]:
    registry = {f"github_tool_{i}": _tool(f"github_tool_{i}") for i in range(mcp_count)}
    # A base (non-MCP) registry tool: auto-whitelisted in production, never part of
    # the console-governed catalog, so it must count on neither side.
    registry["docstore_search"] = _tool("docstore_search", server=None)
    return registry


def _req(registry: dict | None, whitelist: set[str] | None):
    if registry is None:
        return SimpleNamespace(tools=[], state={})
    context = SimpleNamespace(tool_registry=registry, whitelisted_tool_names=whitelist)
    return SimpleNamespace(tools=[], state={}, runtime=SimpleNamespace(context=context))


def _prompt(mw, req) -> str:
    prompt, _hidden = mw._ptc_prompt_and_hidden(req)
    return prompt


def test_gap_note_present_when_the_whitelist_renders_inline():
    """Under the threshold: closed inline list + the gap line (the incident's shape)."""
    registry = _registry(10)
    prompt = _prompt(_mw(), _req(registry, {"github_tool_0", "github_tool_1"}))

    assert "async function githubTool_0" in prompt, "enabled tools are rendered inline"
    assert DISCOVERY_MARKER not in prompt, "no core-only discovery under the threshold"
    assert GAP_MARKER in prompt
    assert "8 of its 10 tools are NOT in it" in prompt
    assert prompt.rstrip().endswith("delegate it, never report it missing."), "the line closes the block"


def test_gap_note_present_in_core_only_mode_too():
    """Over the threshold both lines coexist: discovery (own unrendered tools) + gap (the rest)."""
    registry = _registry(10)
    whitelist = {f"github_tool_{i}" for i in range(5)}  # 5 > threshold(3) → core-only
    prompt = _prompt(_mw(), _req(registry, whitelist))

    assert DISCOVERY_MARKER in prompt
    assert "5 of its 10 tools are NOT in it" in prompt


def test_gap_note_counts_only_mcp_tools():
    """Base registry tools (no server_name) are outside the console catalog: not counted."""
    registry = _registry(4)
    prompt = _prompt(_mw(), _req(registry, {"github_tool_0"}))  # docstore_search not whitelisted

    assert "3 of its 4 tools are NOT in it" in prompt


def test_no_gap_note_when_the_whitelist_covers_the_catalog():
    """Catalog-mode sub-agents (general-purpose, all_tools) set whitelist = catalog: nothing to say."""
    registry = _registry(5)
    prompt = _prompt(_mw(), _req(registry, set(registry)))

    assert GAP_MARKER not in prompt


def test_no_gap_note_without_a_registry_backed_namespace():
    """A plain sub-agent (no context registry) and a non-registry middleware both stay silent."""
    assert GAP_MARKER not in _prompt(_mw(), _req(None, None))

    registry = _registry(5)
    non_registry_mw = _mw(expose_context_registry=False)
    assert GAP_MARKER not in _prompt(non_registry_mw, _req(registry, {"github_tool_0"}))


def test_gap_note_stays_out_of_the_rendered_body_cache():
    """Two users with the same enabled set but different catalogs share the body, not the note."""
    mw = _mw()
    whitelist = {"github_tool_0", "github_tool_1"}
    first = _prompt(mw, _req(_registry(10), whitelist))
    second = _prompt(mw, _req(_registry(20), whitelist))

    assert "8 of its 10 tools" in first
    assert "18 of its 20 tools" in second
    assert first.split(GAP_MARKER)[0] == second.split(GAP_MARKER)[0], "same rendered body"
