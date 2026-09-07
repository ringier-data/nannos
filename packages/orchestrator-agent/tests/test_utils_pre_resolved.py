"""``_pre_resolved_tools_for``: which orchestrator tools a sub-agent gets handed instead of re-discovering."""

from types import SimpleNamespace

from langchain_core.tools import Tool

from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable
from app.utils import _pre_resolved_tools_for


def _tool(name: str) -> Tool:
    return Tool(name=name, description=name, func=lambda x: x)


def test_whitelist_and_self_improvement_tools_present_in_registry_are_handed_over():
    registry = {
        "github_get_me": _tool("github_get_me"),
        "unrelated": _tool("unrelated"),
        "console_create_skill": _tool("console_create_skill"),
        "not_a_tool": {"name": "dict entry"},
    }
    config = SimpleNamespace(mcp_tools=["github_get_me", "missing_from_registry", "not_a_tool"])

    handed = _pre_resolved_tools_for(config, registry)

    assert set(handed) == {"github_get_me", "console_create_skill"}
    assert handed["github_get_me"] is registry["github_get_me"]
    assert "missing_from_registry" not in handed  # left for the sub-agent to discover
    assert "unrelated" not in handed  # only what the sub-agent is entitled to


def test_no_whitelist_still_hands_over_self_improvement_tools():
    registry = {name: _tool(name) for name in DynamicLocalAgentRunnable._CONSOLE_SELF_IMPROVEMENT_TOOLS}
    registry["github_get_me"] = _tool("github_get_me")
    handed = _pre_resolved_tools_for(SimpleNamespace(mcp_tools=None), registry)
    assert set(handed) == set(DynamicLocalAgentRunnable._CONSOLE_SELF_IMPROVEMENT_TOOLS)


def test_build_runtime_context_hands_sub_agents_the_unwrapped_skill_tools():
    """The orchestrator wraps skill tools with agent_name="orchestrator" for itself; sub-agents
    must receive the raw tool so their own agent_name default applies (round-2 review B)."""
    import inspect

    from app import utils

    src = inspect.getsource(utils.build_runtime_context)
    wrap_at = src.index('_wrap_tool_with_agent_name(tool_registry[tool_name], "orchestrator")')
    snapshot_at = src.index("unwrapped_registry = dict(tool_registry)")
    handover_at = src.index("_pre_resolved_tools_for(config, unwrapped_registry)")
    assert snapshot_at < wrap_at < handover_at
