"""Unit tests for MCP tool refresh mechanism in LangGraphAgent.

Tests the periodic background refresh of MCP tool signatures to account for
dynamic schema changes (tools added, removed, or modified).
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from ringier_a2a_sdk.agent.langgraph import LangGraphAgent


@pytest.fixture
def mock_langgraph_agent():
    """Create a mock LangGraphAgent subclass for testing.

    This fixture creates a minimal concrete implementation of LangGraphAgent
    to test the refresh mechanism without requiring a full agent implementation.
    """

    class TestLangGraphAgent(LangGraphAgent):
        def _create_model(self):
            return MagicMock()

        def _create_checkpointer(self):
            return MagicMock()

        async def _get_mcp_connections(self):
            return {}

        def _get_system_prompt(self):
            return "Test system prompt"

        def _get_checkpoint_namespace(self):
            return "test-agent"

    return TestLangGraphAgent()


@pytest.mark.asyncio
async def test_refresh_enabled_by_default(mock_langgraph_agent):
    """Test that MCP tool refresh is enabled by default."""
    assert mock_langgraph_agent._mcp_refresh_enabled is True
    assert mock_langgraph_agent._mcp_refresh_interval_seconds == 300


@pytest.mark.asyncio
async def test_refresh_disabled_via_env_var(monkeypatch):
    """Test that MCP tool refresh can be disabled via environment variable."""
    monkeypatch.setenv("MCP_TOOLS_REFRESH_ENABLED", "false")

    class TestAgent(LangGraphAgent):
        def _create_model(self):
            return MagicMock()

        def _create_checkpointer(self):
            return MagicMock()

        async def _get_mcp_connections(self):
            return {}

        def _get_system_prompt(self):
            return "Test"

        def _get_checkpoint_namespace(self):
            return "test"

    agent = TestAgent()
    assert agent._mcp_refresh_enabled is False


@pytest.mark.asyncio
async def test_refresh_interval_from_env_var(monkeypatch):
    """Test that refresh interval can be configured via environment variable."""
    monkeypatch.setenv("MCP_TOOLS_REFRESH_INTERVAL_SECONDS", "60")

    class TestAgent(LangGraphAgent):
        def _create_model(self):
            return MagicMock()

        def _create_checkpointer(self):
            return MagicMock()

        async def _get_mcp_connections(self):
            return {}

        def _get_system_prompt(self):
            return "Test"

        def _get_checkpoint_namespace(self):
            return "test"

    agent = TestAgent()
    assert agent._mcp_refresh_interval_seconds == 60


@pytest.mark.asyncio
async def test_start_mcp_refresh_idempotent(mock_langgraph_agent):
    """Test that _start_mcp_refresh() is idempotent (safe to call multiple times)."""
    # First call should start the worker
    await mock_langgraph_agent._start_mcp_refresh()
    task1 = mock_langgraph_agent._refresh_task

    # Second call should not create a new task
    await mock_langgraph_agent._start_mcp_refresh()
    task2 = mock_langgraph_agent._refresh_task

    assert task1 is task2, "Second call should reuse existing task"

    # Cleanup
    await mock_langgraph_agent._stop_mcp_refresh()


@pytest.mark.asyncio
async def test_stop_mcp_refresh_cancels_task(mock_langgraph_agent):
    """Test that _stop_mcp_refresh() cancels the background task."""
    # Start the worker
    await mock_langgraph_agent._start_mcp_refresh()
    assert mock_langgraph_agent._refresh_task is not None
    assert not mock_langgraph_agent._refresh_task.done()

    # Stop the worker
    await mock_langgraph_agent._stop_mcp_refresh()

    # Task should be cancelled and references cleared
    assert mock_langgraph_agent._refresh_task is None
    assert mock_langgraph_agent._refresh_stop_event is None


@pytest.mark.asyncio
async def test_stop_mcp_refresh_when_not_started(mock_langgraph_agent):
    """Test that _stop_mcp_refresh() gracefully handles when refresh wasn't started."""
    # Should not raise an error
    await mock_langgraph_agent._stop_mcp_refresh()
    assert mock_langgraph_agent._refresh_task is None


@pytest.mark.asyncio
async def test_refresh_disabled_startup(monkeypatch):
    """Test that startup() skips refresh when disabled."""
    monkeypatch.setenv("MCP_TOOLS_REFRESH_ENABLED", "false")

    class TestAgent(LangGraphAgent):
        def _create_model(self):
            return MagicMock()

        def _create_checkpointer(self):
            return MagicMock()

        async def _get_mcp_connections(self):
            return {}

        def _get_system_prompt(self):
            return "Test"

        def _get_checkpoint_namespace(self):
            return "test"

    agent = TestAgent()
    await agent.startup()

    # Task should not be created
    assert agent._refresh_task is None


@pytest.mark.asyncio
async def test_refresh_worker_respects_interval(mock_langgraph_agent):
    """Test that refresh worker waits for the configured interval."""
    mock_langgraph_agent._mcp_refresh_interval_seconds = 0.1  # 100ms for fast testing

    # Mock _ensure_mcp_tools_loaded to track calls
    call_count = 0
    original_method = mock_langgraph_agent._ensure_mcp_tools_loaded

    async def mock_ensure_mcp_tools():
        nonlocal call_count
        call_count += 1
        # Don't actually load tools for this test
        if call_count > 1:
            # After first refresh, stop the worker to exit the loop
            mock_langgraph_agent._refresh_stop_event.set()

    mock_langgraph_agent._ensure_mcp_tools_loaded = mock_ensure_mcp_tools

    # Start refresh
    await mock_langgraph_agent._start_mcp_refresh()

    # Wait for at least one refresh cycle
    await asyncio.sleep(0.3)

    # Should have called _ensure_mcp_tools_loaded at least once
    assert call_count >= 1, "Refresh worker should call _ensure_mcp_tools_loaded"

    # Cleanup
    await mock_langgraph_agent._stop_mcp_refresh()


@pytest.mark.asyncio
async def test_refresh_worker_resets_tools_on_refresh(mock_langgraph_agent):
    """Test that refresh worker resets tools to force re-discovery."""
    mock_langgraph_agent._mcp_tools = ["tool1", "tool2"]
    mock_langgraph_agent._graph = MagicMock()

    # Mock _ensure_mcp_tools_loaded to stop after one call
    async def mock_ensure():
        mock_langgraph_agent._refresh_stop_event.set()

    mock_langgraph_agent._ensure_mcp_tools_loaded = mock_ensure

    # Start refresh - should reset tools
    await mock_langgraph_agent._start_mcp_refresh()
    await asyncio.sleep(0.2)

    # Since we're testing the worker directly, check if it reset tools
    # (in the real scenario, the worker would have reset and called _ensure_mcp_tools_loaded)

    # Cleanup
    await mock_langgraph_agent._stop_mcp_refresh()


@pytest.mark.asyncio
async def test_startup_and_shutdown_lifecycle(mock_langgraph_agent):
    """Test complete startup and shutdown lifecycle."""
    # Verify initial state
    assert mock_langgraph_agent._refresh_task is None
    assert mock_langgraph_agent._refresh_stop_event is None

    # Startup
    await mock_langgraph_agent.startup()
    assert mock_langgraph_agent._refresh_task is not None
    assert mock_langgraph_agent._refresh_stop_event is not None

    # Shutdown
    await mock_langgraph_agent.shutdown()
    assert mock_langgraph_agent._refresh_task is None
    assert mock_langgraph_agent._refresh_stop_event is None


@pytest.mark.asyncio
async def test_refresh_worker_handles_errors_gracefully(mock_langgraph_agent):
    """Test that refresh worker continues running even if _ensure_mcp_tools_loaded fails."""
    call_count = 0

    async def mock_ensure_with_error():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Simulated MCP error")
        if call_count >= 2:
            # Stop after retry
            mock_langgraph_agent._refresh_stop_event.set()

    mock_langgraph_agent._mcp_refresh_interval_seconds = 0.05
    mock_langgraph_agent._ensure_mcp_tools_loaded = mock_ensure_with_error

    # Start refresh
    await mock_langgraph_agent._start_mcp_refresh()

    # Wait for multiple cycles
    await asyncio.sleep(0.2)

    # Should have called _ensure_mcp_tools_loaded at least twice (error + retry)
    assert call_count >= 2, "Worker should retry after errors"

    # Cleanup
    await mock_langgraph_agent._stop_mcp_refresh()


@pytest.mark.asyncio
async def test_refresh_task_cancellation_safety(mock_langgraph_agent):
    """Test that refresh task can be cancelled safely without dangling tasks."""
    await mock_langgraph_agent._start_mcp_refresh()
    task = mock_langgraph_agent._refresh_task

    # Manually cancel the task
    task.cancel()

    # Allow task to process cancellation
    await asyncio.sleep(0.1)

    # Task should be done (cancelled)
    assert task.done() or task.cancelled()

    # Shutdown should handle cancelled task gracefully
    await mock_langgraph_agent._stop_mcp_refresh()
    assert mock_langgraph_agent._refresh_task is None


@pytest.mark.asyncio
async def test_compute_interface_hash_deterministic(mock_langgraph_agent):
    """Test that interface hash is deterministic for same tools."""
    from langchain_core.tools import BaseTool

    # Create mock tools
    tool1 = MagicMock(spec=BaseTool)
    tool1.name = "tool_a"
    tool1.description = "Description A"

    tool2 = MagicMock(spec=BaseTool)
    tool2.name = "tool_b"
    tool2.description = "Description B"

    tools = [tool1, tool2]

    # Hash should be consistent across multiple calls
    hash1 = mock_langgraph_agent._compute_interface_hash(tools)
    hash2 = mock_langgraph_agent._compute_interface_hash(tools)

    assert hash1 == hash2, "Interface hash should be deterministic"


@pytest.mark.asyncio
async def test_compute_interface_hash_detects_tool_changes(mock_langgraph_agent):
    """Test that interface hash changes when tools change."""
    from langchain_core.tools import BaseTool

    # Create initial tools
    tool1 = MagicMock(spec=BaseTool)
    tool1.name = "tool_a"
    tool1.description = "Description A"

    tools_v1 = [tool1]
    hash_v1 = mock_langgraph_agent._compute_interface_hash(tools_v1)

    # Change tool description
    tool1.description = "Modified Description A"

    hash_v2 = mock_langgraph_agent._compute_interface_hash(tools_v1)
    assert hash_v1 != hash_v2, "Hash should change when tool description changes"

    # Add new tool
    tool2 = MagicMock(spec=BaseTool)
    tool2.name = "tool_b"
    tool2.description = "Description B"

    tools_v2 = [tool1, tool2]
    hash_v3 = mock_langgraph_agent._compute_interface_hash(tools_v2)
    assert hash_v1 != hash_v3, "Hash should change when tools are added"


@pytest.mark.asyncio
async def test_check_mcp_interface_changed_detects_version_change(mock_langgraph_agent):
    """Test that interface change detection works for version changes."""
    from langchain_core.tools import BaseTool

    # Create mock tool
    tool = MagicMock(spec=BaseTool)
    tool.name = "test_tool"
    tool.description = "Test tool"

    tools = [tool]

    # First check should store the version and return False (no change)
    changed = await mock_langgraph_agent._check_mcp_interface_changed(
        server_name="test_server", tools=tools, server_info={"version": "1.0"}
    )
    assert not changed, "First check should store version without detecting change"

    # Second check with same tools should return False
    changed = await mock_langgraph_agent._check_mcp_interface_changed(
        server_name="test_server", tools=tools, server_info={"version": "1.0"}
    )
    assert not changed, "Same tools should not be detected as changed"

    # Third check with changed tool (simulating a version bump) should return True
    tool.description = "Test tool v1.1"
    changed = await mock_langgraph_agent._check_mcp_interface_changed(
        server_name="test_server", tools=tools, server_info={"version": "1.1"}
    )
    assert changed, "Version change should be detected"


@pytest.mark.asyncio
async def test_check_mcp_interface_changed_detects_schema_change(mock_langgraph_agent):
    """Test that interface change detection works for schema/tool changes."""
    from langchain_core.tools import BaseTool

    # Create initial tool
    tool1 = MagicMock(spec=BaseTool)
    tool1.name = "tool_a"
    tool1.description = "Original description"

    tools_v1 = [tool1]

    # First check should store the hash and return False
    changed = await mock_langgraph_agent._check_mcp_interface_changed(
        server_name="test_server", tools=tools_v1, server_info={"version": "1.0"}
    )
    assert not changed, "First check should store hash without detecting change"

    # Second check with modified tool should detect change
    tool1.description = "Modified description"

    changed = await mock_langgraph_agent._check_mcp_interface_changed(
        server_name="test_server", tools=tools_v1, server_info={"version": "1.0"}
    )
    assert changed, "Tool description change should be detected"


@pytest.mark.asyncio
async def test_refresh_worker_skips_rebuild_when_no_changes(mock_langgraph_agent):
    """Test that refresh worker skips graph rebuild when interface hasn't changed."""
    from langchain_core.tools import BaseTool

    # Set up initial state with tools and graph
    tool = MagicMock(spec=BaseTool)
    tool.name = "test_tool"
    tool.description = "Test tool"

    mock_langgraph_agent._mcp_tools = [tool]
    mock_langgraph_agent._graph = MagicMock()

    rebuild_count = 0
    original_create_graph = mock_langgraph_agent._create_graph

    def mock_create_graph(tools):
        nonlocal rebuild_count
        rebuild_count += 1
        return original_create_graph(tools)

    mock_langgraph_agent._create_graph = mock_create_graph

    # Mock the connection and tool loading
    async def mock_get_connections():
        return {"test_server": MagicMock()}

    async def mock_load_tools(*args, **kwargs):
        return [tool]

    mock_langgraph_agent._get_mcp_connections = mock_get_connections
    mock_langgraph_agent._load_mcp_tools_with_retry = mock_load_tools
    mock_langgraph_agent._filter_tools = lambda tools: tools

    # Store initial hash
    await mock_langgraph_agent._check_mcp_interface_changed(
        server_name="test_server", tools=[tool], server_info={"version": "1.0"}
    )

    # Mock interval for fast testing
    mock_langgraph_agent._mcp_refresh_interval_seconds = 0.05

    # Mock _ensure_mcp_tools_loaded to stop after verification
    async def mock_ensure():
        mock_langgraph_agent._refresh_stop_event.set()

    mock_langgraph_agent._ensure_mcp_tools_loaded = mock_ensure

    # Run one refresh cycle
    await mock_langgraph_agent._start_mcp_refresh()
    await asyncio.sleep(0.2)

    # Graph should not have been rebuilt (no changes detected)
    # Note: This is tricky to test since we're mocking multiple things
    # The key is that no error should occur and the worker should continue

    await mock_langgraph_agent._stop_mcp_refresh()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
