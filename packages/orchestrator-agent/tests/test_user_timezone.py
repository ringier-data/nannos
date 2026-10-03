"""The user's saved timezone reaches UserConfig (nannos#343)."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.core.executor import OrchestratorDeepAgentExecutor
from app.core.registry import RegistryService, User, UserSettings


def _settings(timezone: str) -> UserSettings:
    return UserSettings(
        user_id="user-123",
        language="en",
        timezone=timezone,
        mcp_tools=[],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_to_user_carries_timezone():
    user = RegistryService()._to_user("sub-123", [], _settings("America/New_York"))
    assert user.timezone == "America/New_York"


async def _build(user: User):
    executor = OrchestratorDeepAgentExecutor()
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
@patch.dict("os.environ", {"DEFAULT_TIMEZONE": "Europe/Zurich"})
async def test_user_config_uses_saved_timezone():
    user_config = await _build(User(id="user-123", sub="sub-123", timezone="America/New_York"))
    assert user_config.timezone == "America/New_York"


@pytest.mark.asyncio
@patch.dict("os.environ", {"DEFAULT_TIMEZONE": "Europe/Zurich"})
async def test_user_config_falls_back_to_deployment_default():
    user_config = await _build(User(id="user-123", sub="sub-123"))
    assert user_config.timezone == "Europe/Zurich"
