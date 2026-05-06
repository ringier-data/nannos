"""Test task-scheduler tool whitelist configuration."""

from unittest.mock import Mock

from pydantic import SecretStr

from app.models.config import UserConfig
from app.utils import build_runtime_context


def test_task_scheduler_whitelist_includes_scheduler_and_console_tools():
    """Test that task-scheduler has access to scheduler_* and console_* tools."""
    # Create mock tools
    scheduler_tool = Mock()
    scheduler_tool.name = "scheduler_create_job"

    console_tool = Mock()
    console_tool.name = "console_list_sub_agents"

    github_tool = Mock()
    github_tool.name = "github-pull-request_activePullRequest"

    user_config = UserConfig(
        user_id="user-123",
        user_sub="sub-123",
        name="Test User",
        email="test@example.com",
        access_token=SecretStr("test-token"),
        tools=[scheduler_tool, console_tool, github_tool],
    )

    # Mock the task_scheduler_graph_provider
    def mock_graph_provider():
        return Mock()

    context = build_runtime_context(
        user_config,
        task_scheduler_graph_provider=mock_graph_provider,
    )

    # Verify task-scheduler sub-agent was created
    assert "task-scheduler" in context.subagent_registry

    # Get the task-scheduler subagent (it's a dict with 'runnable' key)
    task_scheduler_subagent = context.subagent_registry["task-scheduler"]

    # Verify task-scheduler has correct whitelist
    # The whitelist should include scheduler_* and console_* tools
    # task_scheduler_subagent["runnable"] is TaskSchedulerRunnable
    task_scheduler_context = task_scheduler_subagent["runnable"]._user_context
    whitelisted_names = task_scheduler_context.whitelisted_tool_names

    assert "scheduler_create_job" in whitelisted_names
    assert "console_list_sub_agents" in whitelisted_names
    # GitHub tool should NOT be in task-scheduler's whitelist
    assert "github-pull-request_activePullRequest" not in whitelisted_names


def test_task_scheduler_whitelist_filters_correctly():
    """Test that task-scheduler whitelist only includes scheduler/console tools."""
    # Create multiple tools with different prefixes
    mock_tools = []
    tool_names = [
        "scheduler_create_job",
        "scheduler_list_jobs",
        "scheduler_validate_watch",
        "console_list_sub_agents",
        "console_create_sub_agent",
        "github-pull-request_issue_fetch",
        "some_other_tool",
    ]

    for name in tool_names:
        tool = Mock()
        tool.name = name
        mock_tools.append(tool)

    user_config = UserConfig(
        user_id="user-123",
        user_sub="sub-123",
        name="Test User",
        email="test@example.com",
        access_token=SecretStr("test-token"),
        tools=mock_tools,
    )

    # Mock the task_scheduler_graph_provider
    def mock_graph_provider():
        return Mock()

    context = build_runtime_context(
        user_config,
        task_scheduler_graph_provider=mock_graph_provider,
    )

    # Get task-scheduler whitelist (subagent is a dict with 'runnable' key)
    task_scheduler_subagent = context.subagent_registry["task-scheduler"]
    task_scheduler_context = task_scheduler_subagent["runnable"]._user_context
    whitelisted_names = task_scheduler_context.whitelisted_tool_names

    # Should include all scheduler_* and console_* tools
    assert "scheduler_create_job" in whitelisted_names
    assert "scheduler_list_jobs" in whitelisted_names
    assert "scheduler_validate_watch" in whitelisted_names
    assert "console_list_sub_agents" in whitelisted_names
    # console_create_sub_agent is intentionally excluded from task-scheduler whitelist
    assert "console_create_sub_agent" not in whitelisted_names

    # Should NOT include other tools
    assert "github-pull-request_issue_fetch" not in whitelisted_names
    assert "some_other_tool" not in whitelisted_names

    # Verify count
    assert len(whitelisted_names) == 4
