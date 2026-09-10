"""``build_runtime_context``: a sub-agent with ``all_tools`` gets the GP agent's lazy catalog (ADR-0006)."""

from unittest.mock import Mock, patch

from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from langchain_core.tools import Tool
from pydantic import SecretStr

from app.models.config import UserConfig
from app.utils import build_runtime_context


def _tool(name: str) -> Tool:
    return Tool(name=name, description=name, func=lambda x: x)


def _build(config: LocalLangGraphSubAgentConfig, monkeypatch) -> dict:
    """Build the context with one sub-agent and return the kwargs it was created with."""
    monkeypatch.setenv("GP_TOOL_CATALOG", "true")
    user_config = UserConfig(
        user_id="user-123",
        user_sub="sub-123",
        name="Test User",
        email="test@example.com",
        access_token=SecretStr("test-token"),
        model="claude-sonnet-4.5",
        tools=[_tool("list_campaigns"), _tool("get_campaign")],
        local_subagents=[config],
    )
    with (
        patch("agent_common.core.model_factory.create_model", return_value=Mock()),
        patch(
            "agent_common.agents.dynamic_agent.create_dynamic_local_subagent"
        ) as factory,
    ):
        factory.return_value = {
            "name": config.name,
            "description": config.description,
            "runnable": Mock(),
        }
        build_runtime_context(user_config, agent_settings=Mock(), checkpointer=Mock())
    assert factory.call_count == 1
    return factory.call_args.kwargs


def test_all_tools_sub_agent_gets_the_whole_registry_as_a_catalog(monkeypatch):
    kwargs = _build(
        LocalLangGraphSubAgentConfig(
            name="alloy-ai-assistant",
            description="Embedded agent",
            system_prompt="Domain guidance.",
            all_tools=True,
        ),
        monkeypatch,
    )
    assert kwargs["inject_all_tools"] is None  # never the bind-all path
    assert {"list_campaigns", "get_campaign"} <= set(kwargs["tool_catalog"])


def test_plain_sub_agent_without_tools_gets_no_catalog(monkeypatch):
    kwargs = _build(
        LocalLangGraphSubAgentConfig(
            name="plain-agent",
            description="Plain agent",
            system_prompt="Prompt.",
        ),
        monkeypatch,
    )
    assert kwargs["tool_catalog"] is None
    assert kwargs["inject_all_tools"] is None
