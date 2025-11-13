"""Unit tests for GraphManager class."""

from unittest.mock import MagicMock, Mock, patch

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core import GraphManager


def create_mock_tool(name: str, description: str = "Test tool") -> Mock:
    """Create a properly mocked tool that works with LangChain."""
    tool = Mock(spec=BaseTool)
    tool.name = name
    tool.description = description
    tool.__name__ = name  # Required for tool decorator
    tool._run = MagicMock(return_value="test result")
    tool.run = MagicMock(return_value="test result")
    return tool


def create_mock_subagent(name: str, description: str = "Test agent") -> Mock:
    """Create a properly mocked subagent."""
    # CompiledSubAgent is a TypedDict, so we need to create a dict-like mock
    subagent = Mock()
    subagent.__getitem__ = Mock(side_effect=lambda k: {"name": name, "description": description}[k])
    subagent.__contains__ = Mock(return_value=True)
    subagent.get = Mock(side_effect=lambda k, default=None: {"name": name, "description": description}.get(k, default))
    return subagent


class TestGraphManagerInitialization:
    """Test GraphManager initialization."""

    def test_initialization_with_required_params(self):
        """Test GraphManager initializes with required parameters."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        system_prompt = "You are a helpful assistant"

        manager = GraphManager(model, checkpointer, system_prompt, middleware=[])

        assert manager.model == model
        assert manager.checkpointer == checkpointer
        assert manager.system_prompt == system_prompt
        assert manager.middleware == []
        assert manager.graphs == {}

    def test_initialization_with_middleware(self):
        """Test GraphManager initializes with middleware stack."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        system_prompt = "Test prompt"
        middleware = [Mock(), Mock()]

        manager = GraphManager(model, checkpointer, system_prompt, middleware=middleware)

        assert manager.middleware == middleware
        assert len(manager.middleware) == 2


class TestConfigSignatureGeneration:
    """Test configuration signature generation."""

    def test_get_config_signature_with_no_tools_or_subagents(self):
        """Test signature generation with empty lists."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        sig = manager.get_config_signature([], [])

        assert sig == "tools:|subagents:"

    def test_get_config_signature_with_tools_only(self):
        """Test signature generation with tools - includes description hash."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        tool1 = create_mock_tool("tool_a", "Description A")
        tool2 = create_mock_tool("tool_b", "Description B")

        sig = manager.get_config_signature([tool1, tool2], [])

        # Signature includes tool name and hash of description
        assert sig.startswith("tools:tool_a:")
        assert "tool_b:" in sig
        assert sig.endswith("|subagents:")
        # Verify both tools are present in sorted order
        parts = sig.split("|")[0].replace("tools:", "").split(",")
        assert len(parts) == 2
        assert parts[0].startswith("tool_a:")
        assert parts[1].startswith("tool_b:")

    def test_get_config_signature_with_subagents_only(self):
        """Test signature generation with subagents - uses dict format."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        # Subagents are TypedDict objects
        agent1 = create_mock_subagent("agent_x", "Agent X description")
        agent2 = create_mock_subagent("agent_y", "Agent Y description")

        sig = manager.get_config_signature([], [agent1, agent2])

        # Signature includes agent name and hash of description
        assert sig.startswith("tools:|subagents:agent_x:")
        assert "agent_y:" in sig
        # Verify both agents are present in sorted order
        parts = sig.split("|")[1].replace("subagents:", "").split(",")
        assert len(parts) == 2
        assert parts[0].startswith("agent_x:")
        assert parts[1].startswith("agent_y:")

    def test_get_config_signature_with_tools_and_subagents(self):
        """Test signature generation with both tools and subagents."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        tool1 = create_mock_tool("tool_z", "Tool Z")
        agent1 = create_mock_subagent("agent_a", "Agent A")

        sig = manager.get_config_signature([tool1], [agent1])

        # Signature includes hashes
        assert sig.startswith("tools:tool_z:")
        assert "subagents:agent_a:" in sig

    def test_get_config_signature_sorted_order(self):
        """Test that signature generation sorts names alphabetically."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        tool1 = Mock()
        tool1.name = "zebra"
        tool1.description = "Z"
        tool2 = Mock()
        tool2.name = "alpha"
        tool2.description = "A"
        tool3 = Mock()
        tool3.name = "beta"
        tool3.description = "B"

        sig = manager.get_config_signature([tool1, tool2, tool3], [])

        # Verify alphabetical sorting by checking the order of tool names
        tools_part = sig.split("|")[0].replace("tools:", "")
        tool_entries = tools_part.split(",")
        assert len(tool_entries) == 3
        assert tool_entries[0].startswith("alpha:")
        assert tool_entries[1].startswith("beta:")
        assert tool_entries[2].startswith("zebra:")


class TestGraphCaching:
    """Test graph caching functionality."""

    def test_get_cached_graph_returns_none_when_empty(self):
        """Test that cache miss returns None."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        result = manager.get_cached_graph("some_signature")

        assert result is None

    def test_get_cached_graph_returns_graph_when_present(self):
        """Test that cache hit returns the graph."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        mock_graph = Mock()
        manager.graphs["test_sig"] = mock_graph

        result = manager.get_cached_graph("test_sig")

        assert result == mock_graph

    @patch("app.core.graph_manager.create_deep_agent")
    def test_create_and_cache_graph_creates_new_graph(self, mock_create):
        """Test that create_and_cache_graph creates a new graph."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "Test prompt", middleware=[])

        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        tool1 = create_mock_tool("tool_a")

        result = manager.create_and_cache_graph("sig_123", [tool1], [])

        assert result == mock_compiled
        # Note: The actual call includes additional parameters like response_format
        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert call_args.kwargs["model"] == model
        assert call_args.kwargs["tools"] == [tool1]
        assert call_args.kwargs["subagents"] == []
        assert call_args.kwargs["checkpointer"] == checkpointer
        assert call_args.kwargs["system_prompt"] == "Test prompt"
        assert call_args.kwargs["middleware"] == []

    @patch("app.core.graph_manager.create_deep_agent")
    def test_create_and_cache_graph_with_middleware(self, mock_create):
        """Test that create_and_cache_graph passes middleware."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)

        # Create proper middleware mocks with tools attribute
        middleware1 = Mock()
        middleware1.tools = []
        middleware2 = Mock()
        middleware2.tools = []
        middleware = [middleware1, middleware2]

        manager = GraphManager(model, checkpointer, "prompt", middleware=middleware)

        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        manager.create_and_cache_graph("sig_456", [], [])

        mock_create.assert_called_once()
        call_args = mock_create.call_args
        assert call_args.kwargs["model"] == model
        assert call_args.kwargs["tools"] == []
        assert call_args.kwargs["subagents"] == []
        assert call_args.kwargs["checkpointer"] == checkpointer
        assert call_args.kwargs["system_prompt"] == "prompt"
        assert call_args.kwargs["middleware"] == middleware

    @patch("app.core.graph_manager.create_deep_agent")
    def test_create_and_cache_graph_stores_in_cache(self, mock_create):
        """Test that created graph is stored in cache."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        result = manager.create_and_cache_graph("sig_789", [], [])

        assert "sig_789" in manager.graphs
        assert manager.graphs["sig_789"] == mock_compiled
        assert result == mock_compiled

    def test_clear_cache_empties_graph_dict(self):
        """Test that clear_cache removes all cached graphs."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        manager.graphs["sig_1"] = Mock()
        manager.graphs["sig_2"] = Mock()
        assert len(manager.graphs) == 2

        manager.clear_cache()

        assert len(manager.graphs) == 0
        assert manager.graphs == {}


class TestGetOrCreateGraph:
    """Test the main get_or_create_graph method."""

    @patch("app.core.graph_manager.create_deep_agent")
    def test_get_or_create_graph_creates_on_cache_miss(self, mock_create):
        """Test that get_or_create_graph creates a new graph when not cached."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        tool1 = create_mock_tool("tool_a", "Tool A description")

        graph = manager.get_or_create_graph([tool1], [])

        assert graph == mock_compiled
        # Signature includes hash of description
        assert manager.signature.startswith("tools:tool_a:")
        assert manager.signature.endswith("|subagents:")
        mock_create.assert_called_once()

    def test_get_or_create_graph_returns_cached_on_cache_hit(self):
        """Test that get_or_create_graph returns cached graph."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        tool1 = create_mock_tool("tool_a", "Tool A description")

        # Generate the expected signature
        expected_sig = manager.get_config_signature([tool1], [])

        mock_cached_graph = Mock()
        manager.graphs[expected_sig] = mock_cached_graph

        graph = manager.get_or_create_graph([tool1], [])

        assert graph == mock_cached_graph
        assert manager.signature == expected_sig

    @patch("app.core.graph_manager.create_deep_agent")
    def test_get_or_create_graph_caches_multiple_configurations(self, mock_create):
        """Test that different configurations are cached separately."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        mock_graph_1 = Mock()
        mock_graph_2 = Mock()
        mock_create.side_effect = [mock_graph_1, mock_graph_2]

        tool1 = create_mock_tool("tool_a")
        tool2 = create_mock_tool("tool_b")

        graph1 = manager.get_or_create_graph([tool1], [])
        sig1 = manager.signature
        graph2 = manager.get_or_create_graph([tool2], [])
        sig2 = manager.signature

        assert graph1 == mock_graph_1
        assert graph2 == mock_graph_2
        assert sig1 != sig2
        assert len(manager.graphs) == 2

    @patch("app.core.graph_manager.create_deep_agent")
    def test_get_or_create_graph_reuses_same_configuration(self, mock_create):
        """Test that same configuration returns cached graph without recreation."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        tool1 = create_mock_tool("tool_a", "Tool A description")

        graph1 = manager.get_or_create_graph([tool1], [])
        sig1 = manager.signature
        graph2 = manager.get_or_create_graph([tool1], [])
        sig2 = manager.signature

        assert graph1 == graph2
        assert sig1 == sig2
        mock_create.assert_called_once()  # Only created once


class TestGraphManagerEdgeCases:
    """Test edge cases and error handling."""

    def test_get_config_signature_with_none_values(self):
        """Test signature generation handles None gracefully."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        sig = manager.get_config_signature(None, None)

        assert sig == "tools:|subagents:"

    @patch("app.core.graph_manager.create_deep_agent")
    def test_create_and_cache_graph_with_empty_signature(self, mock_create):
        """Test that empty signature is valid."""
        model = Mock(spec=BaseChatModel)
        checkpointer = Mock(spec=BaseCheckpointSaver)
        manager = GraphManager(model, checkpointer, "prompt", middleware=[])

        mock_compiled = Mock()
        mock_create.return_value = mock_compiled

        result = manager.create_and_cache_graph("", [], [])

        assert result == mock_compiled
        assert "" in manager.graphs
