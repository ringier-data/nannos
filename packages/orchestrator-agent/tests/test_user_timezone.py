"""The user's saved settings reach UserConfig and the time tool (nannos#343)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.core.executor import OrchestratorDeepAgentExecutor
from app.core.registry import RegistryService, User, UserSettings
from app.core.time_tools import create_time_tool

# UserSettings fields that are bookkeeping, or that map onto a differently named User field.
_NOT_COPIED_BY_NAME = {"user_id", "created_at", "updated_at", "mcp_tools"}


def test_to_user_copies_every_setting():
    """A field added to UserSettings and User but not mapped in _to_user is how #343 happened."""
    settings = UserSettings(
        user_id="user-123",
        language="de",
        timezone="America/New_York",
        custom_prompt="be brief",
        mcp_tools=["tool_a"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        preferred_model="model-x",
        enable_thinking=True,
        thinking_level="high",
        tool_bypass_rules={"tool_a": {"mode": "all"}},
    )
    unset = [name for name in UserSettings.model_fields if getattr(settings, name) is None]
    assert not unset, f"give these a non-default value in the test: {unset}"

    user = RegistryService()._to_user("sub-123", [], settings)

    for name in set(UserSettings.model_fields) - _NOT_COPIED_BY_NAME:
        assert getattr(user, name) == getattr(settings, name), name
    assert user.id == settings.user_id
    assert user.tool_names == settings.mcp_tools


async def _build(user: User):
    executor = OrchestratorDeepAgentExecutor()
    # No real discovery (token exchange, MCP gateway) and no shared discovery cache.
    with (
        patch("app.core.executor.get_discovery_cache", return_value=Mock(get=Mock(return_value=None))),
        patch.object(executor.agent.agent_discovery_service, "register_agents", AsyncMock(return_value=[])),
        patch.object(executor.agent.tool_discovery_service, "discover_tools", AsyncMock(return_value=[])),
    ):
        return await executor._build_user_config(
            user=user,
            user_sub="sub-123",
            user_token="test-token",
            user_name="Test User",
            user_email="test@example.com",
            user_groups=[],
            model_choice=None,
            message_formatting="markdown",
            client_user_handle=None,
            sub_agent_config_hash=None,
        )


@pytest.mark.asyncio
async def test_user_config_uses_saved_timezone():
    user_config = await _build(User(id="user-123", sub="sub-123", timezone="America/New_York"))
    assert user_config.timezone == "America/New_York"


@dataclass
class _Context:
    timezone: str


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _run_time_tool(args: dict, timezone: str) -> str:
    graph = StateGraph(_State, context_schema=_Context)
    graph.add_node("tools", ToolNode([create_time_tool()]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    call = AIMessage(content="", tool_calls=[{"id": "c1", "name": "get_current_time", "args": args}])
    result = graph.compile().invoke({"messages": [call]}, context=_Context(timezone=timezone))
    return result["messages"][-1].content


@patch.dict("os.environ", {"DEFAULT_TIMEZONE": "Europe/Zurich"})
def test_time_tool_defaults_to_the_run_timezone():
    assert _run_time_tool({}, "Asia/Tokyo").endswith("+09:00")


@patch.dict("os.environ", {"DEFAULT_TIMEZONE": "Europe/Zurich"})
def test_time_tool_explicit_timezone_wins():
    assert _run_time_tool({"timezone": "UTC"}, "Asia/Tokyo").endswith("+00:00")
