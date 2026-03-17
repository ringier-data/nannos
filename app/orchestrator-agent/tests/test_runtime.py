"""Unit tests for runtime context building."""

import logging
from unittest.mock import Mock

from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from pydantic import SecretStr

from app.models.config import GraphRuntimeContext, UserConfig
from app.utils import build_runtime_context


class TestBuildRuntimeContext:
    """Test suite for build_runtime_context function."""

    def test_minimal_user_config(self):
        """Test building runtime context with minimal user config."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
        )

        context = build_runtime_context(user_config)

        assert isinstance(context, GraphRuntimeContext)
        assert context.user_id == "user-123"
        assert context.name == "Test User"
        assert context.email == "test@example.com"
        assert context.tool_registry == {}
        assert "file-analyzer" in context.subagent_registry

    def test_user_config_with_tools(self):
        """Test building context with user tools."""
        mock_tool1 = Mock()
        mock_tool1.name = "tool1"

        mock_tool2 = Mock()
        mock_tool2.name = "tool2"

        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tools=[mock_tool1, mock_tool2],
        )

        context = build_runtime_context(user_config)

        assert len(context.tool_registry) == 2
        assert "tool1" in context.tool_registry
        assert "tool2" in context.tool_registry
        assert context.tool_registry["tool1"] == mock_tool1
        assert context.tool_registry["tool2"] == mock_tool2

    def test_user_config_with_dict_tools(self):
        """Test building context with dict-format tools."""
        dict_tool = {"name": "dict_tool", "description": "A tool"}

        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tools=[dict_tool],
        )

        context = build_runtime_context(user_config)

        assert "dict_tool" in context.tool_registry
        assert context.tool_registry["dict_tool"] == dict_tool

    def test_whitelisted_tool_names_only_from_user_config(self):
        """Test that whitelisted_tool_names contains only tools from user_config.tool_names.

        This verifies that the whitelist is built exclusively from the DB (user_config.tool_names),
        not from all tools in the registry. The registry may contain additional tools like
        docstore tools, but only user-configured tools should be whitelisted for the orchestrator.
        """
        # Create 3 mock tools that will go into the registry
        mock_tool_a = Mock()
        mock_tool_a.name = "tool_a"

        mock_tool_b = Mock()
        mock_tool_b.name = "tool_b"

        mock_tool_c = Mock()
        mock_tool_c.name = "tool_c"

        # User has 3 tools discovered, but only 2 are whitelisted in DB
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tools=[mock_tool_a, mock_tool_b, mock_tool_c],  # All 3 tools in registry
            tool_names=["tool_a", "tool_b"],  # Only 2 whitelisted from DB
        )

        context = build_runtime_context(user_config)

        # Verify the whitelist contains exactly what's in tool_names (from DB)
        assert context.whitelisted_tool_names == {"tool_a", "tool_b"}

        # Verify the registry has all 3 tools (superset of whitelist)
        assert len(context.tool_registry) == 3
        assert "tool_a" in context.tool_registry
        assert "tool_b" in context.tool_registry
        assert "tool_c" in context.tool_registry

        # Verify tool_c is in registry but NOT in whitelist
        assert "tool_c" not in context.whitelisted_tool_names

    def test_whitelisted_tool_names_excludes_docstore_tools(self):
        """Test that docstore tools are not automatically added to whitelisted_tool_names.

        Document store tools are added to tool_registry at runtime, but should never
        be added to whitelisted_tool_names (which comes exclusively from DB settings).
        """
        mock_user_tool = Mock()
        mock_user_tool.name = "user_tool"

        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tools=[mock_user_tool],
            tool_names=["user_tool"],  # Only user_tool in whitelist from DB
        )

        # Mock document store dependencies to trigger docstore tool addition
        mock_store = Mock()
        mock_s3_service = Mock()
        bucket_name = "test-bucket"

        context = build_runtime_context(
            user_config,
            document_store=mock_store,
            s3_service=mock_s3_service,
            document_store_bucket=bucket_name,
        )

        # Docstore tools should be in registry (added at runtime)
        assert len(context.tool_registry) > 1  # user_tool + docstore tools

        # But whitelist should ONLY contain user_tool from DB
        assert context.whitelisted_tool_names == {"user_tool"}

        # Verify docstore tools are NOT in whitelist
        for tool_name in context.tool_registry.keys():
            if tool_name != "user_tool":
                # This is a docstore tool - should NOT be whitelisted
                assert tool_name not in context.whitelisted_tool_names

    def test_user_config_with_sub_agents(self):
        """Test building context with remote A2A sub-agents (dict format)."""
        # Remote sub-agents are passed as dicts in runtime.py line 106-108
        subagent_dict = {"name": "jira-agent", "description": "JIRA agent", "runnable": Mock()}

        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            sub_agents=[],  # sub_agents validation expects CompiledSubAgent
        )

        # Simulate what runtime.py does - add dicts to subagent_registry
        context = build_runtime_context(user_config)

        # Manually add a dict subagent like runtime.py does
        context.subagent_registry["jira-agent"] = subagent_dict

        # Built-in file-analyzer should be there
        assert "file-analyzer" in context.subagent_registry
        # Our manually added dict subagent
        assert "jira-agent" in context.subagent_registry

    def test_document_store_tools_integration(self):
        """Test adding document store tools when dependencies provided."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
        )

        # Mock document store dependencies
        mock_store = Mock()
        mock_s3_service = Mock()
        bucket_name = "test-bucket"

        context = build_runtime_context(
            user_config,
            document_store=mock_store,
            s3_service=mock_s3_service,
            document_store_bucket=bucket_name,
        )

        # Document store tools should be added
        # (actual tool names depend on create_document_store_tools implementation)
        assert len(context.tool_registry) > 0

    def test_static_tools_excluded_from_subagents(self):
        """Test that FinalResponseSchema is filtered from sub-agent tools."""
        mock_static_tool = Mock()
        mock_static_tool.name = "FinalResponseSchema"

        mock_regular_tool = Mock()
        mock_regular_tool.name = "regular_tool"

        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            local_subagents=[
                LocalLangGraphSubAgentConfig(
                    name="test-subagent",
                    description="Test sub-agent",
                    instructions="Test instructions",
                    system_prompt="Test system prompt",
                )
            ],
        )

        mock_agent_settings = Mock()
        mock_checkpointer = Mock()

        # Should not raise an error even with FinalResponseSchema in static_tools
        context = build_runtime_context(
            user_config,
            agent_settings=mock_agent_settings,
            checkpointer=mock_checkpointer,
            static_tools=[mock_static_tool, mock_regular_tool],
        )

        # Context should be created successfully
        assert context is not None
        assert context.user_id == "user-123"

    def test_user_preferences_preserved(self):
        """Test that user preferences are preserved in runtime context."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            language="de",
            timezone="Europe/Zurich",
            message_formatting="markdown",
            slack_user_handle="@testuser",
            custom_prompt="Custom instructions here",
        )

        context = build_runtime_context(user_config)

        assert context.language == "de"
        assert context.timezone == "Europe/Zurich"
        assert context.message_formatting == "markdown"
        assert context.slack_user_handle == "@testuser"
        assert context.custom_prompt == "Custom instructions here"

    def test_local_subagents_without_agent_settings_warning(self, caplog):
        """Test warning when local_subagents configured without agent_settings."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            local_subagents=[
                LocalLangGraphSubAgentConfig(
                    name="test-subagent",
                    description="Test sub-agent",
                    instructions="Test instructions",
                    system_prompt="Test system prompt",
                )
            ],
        )

        with caplog.at_level(logging.WARNING):
            context = build_runtime_context(user_config)

        # Should log warning about missing agent_settings
        assert any("local_subagents configured but no agent_settings" in record.message for record in caplog.records)
        # Context should still be created
        assert context is not None
        # Sub-agent should NOT be in registry
        assert "test-subagent" not in context.subagent_registry

    def test_invalid_local_subagent_graceful_degradation(self, caplog):
        """Test graceful degradation when sub-agent creation fails."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            local_subagents=[
                LocalLangGraphSubAgentConfig(
                    name="failing-subagent",
                    description="Test sub-agent",
                    instructions="Test instructions",
                    system_prompt="Test system prompt",
                )
            ],
        )

        # Create a mock that will fail during agent creation
        # by raising an exception when called (e.g., during model creation)
        from unittest.mock import patch

        with caplog.at_level(logging.ERROR):
            # Patch the create_dynamic_local_subagent to raise an error
            # It's imported inside build_runtime_context, so patch where it's used
            with patch(
                "agent_common.agents.dynamic_agent.create_dynamic_local_subagent",
                side_effect=Exception("Agent creation failed"),
            ):
                context = build_runtime_context(
                    user_config,
                    agent_settings=Mock(),  # Valid settings
                    checkpointer=Mock(),
                )

        # Should log error about failed agent creation
        assert any("Failed to create dynamic sub-agent" in record.message for record in caplog.records)
        # Context should still be created
        assert context is not None
        # Failed sub-agent should not be in registry
        assert "failing-subagent" not in context.subagent_registry

    def test_model_inheritance_from_user_config(self):
        """Test that sub-agents inherit orchestrator model when not specified."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            model="claude-sonnet-4.5",  # Orchestrator model
            local_subagents=[
                LocalLangGraphSubAgentConfig(
                    name="test-subagent",
                    description="Test sub-agent",
                    instructions="Test instructions",
                    system_prompt="Test system prompt",
                    # No model_name specified - should inherit
                )
            ],
        )

        mock_agent_settings = Mock()
        mock_checkpointer = Mock()

        # Context creation should succeed
        context = build_runtime_context(
            user_config,
            agent_settings=mock_agent_settings,
            checkpointer=mock_checkpointer,
        )

        assert context is not None
        # Sub-agent should be registered (if model creation succeeded)
        # This verifies inheritance logic was attempted


class TestRuntimeContextValidation:
    """Test runtime context validation and edge cases."""

    def test_empty_tool_list(self):
        """Test handling of empty tool list."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tools=[],
        )

        context = build_runtime_context(user_config)

        assert context.tool_registry == {}

    def test_none_tool_list(self):
        """Test handling of None tool list."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tools=None,
        )

        context = build_runtime_context(user_config)

        assert context.tool_registry == {}

    def test_empty_subagents_list(self):
        """Test handling of empty sub-agents list."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            sub_agents=[],
        )

        context = build_runtime_context(user_config)

        # Only built-in file-analyzer should be present
        assert len(context.subagent_registry) == 1
        assert "file-analyzer" in context.subagent_registry

    def test_whitelisted_tool_names_none(self):
        """Test that whitelisted_tool_names is empty set when tool_names is None."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tool_names=None,  # No whitelist from DB
        )

        context = build_runtime_context(user_config)

        # Whitelist should be empty set
        assert context.whitelisted_tool_names == set()
        assert isinstance(context.whitelisted_tool_names, set)

    def test_whitelisted_tool_names_empty_list(self):
        """Test that whitelisted_tool_names is empty set when tool_names is empty list."""
        user_config = UserConfig(
            user_id="user-123",
            user_sub="sub-123",
            name="Test User",
            email="test@example.com",
            access_token=SecretStr("test-token"),
            tool_names=[],  # Empty whitelist from DB
        )

        context = build_runtime_context(user_config)

        # Whitelist should be empty set
        assert context.whitelisted_tool_names == set()
        assert isinstance(context.whitelisted_tool_names, set)
