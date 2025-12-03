"""Tests for agent base classes."""

import pytest
from a2a.types import TaskState
from pydantic import SecretStr

from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig


class TestBaseAgent:
    """Tests for BaseAgent abstract class."""

    def test_base_agent_is_abstract(self):
        """Test that BaseAgent cannot be instantiated directly."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            BaseAgent()

    def test_base_agent_supported_content_types(self):
        """Test that BaseAgent defines supported content types."""
        assert BaseAgent.SUPPORTED_CONTENT_TYPES == ["text", "text/plain"]

    @pytest.mark.asyncio
    async def test_base_agent_implementation(self):
        """Test that a concrete implementation of BaseAgent works correctly."""
        from unittest.mock import Mock
        
        class ConcreteAgent(BaseAgent):
            def __init__(self):
                self.closed = False
            
            async def close(self):
                self.closed = True
            
            async def stream(self, query: str, user_config: UserConfig, task):
                yield AgentStreamResponse(
                    state=TaskState.working,
                    content="Processing..."
                )
                yield AgentStreamResponse(
                    state=TaskState.completed,
                    content=f"Result for: {query}"
                )
        
        # Create instance
        agent = ConcreteAgent()
        assert not agent.closed
        
        # Test stream method
        user_config = UserConfig(
            user_id="test-user",
            access_token=SecretStr("test-token"),
            name="Test User",
            email="test@example.com"
        )
        
        # Create a mock task
        task = Mock()
        task.id = "task-1"
        task.context_id = "ctx-1"
        
        responses = []
        async for response in agent.stream("test query", user_config, task):
            responses.append(response)
        
        assert len(responses) == 2
        assert responses[0].state == TaskState.working
        assert responses[0].content == "Processing..."
        assert responses[1].state == TaskState.completed
        assert "test query" in responses[1].content
        
        # Test close method
        await agent.close()
        assert agent.closed

    def test_base_agent_requires_close_implementation(self):
        """Test that BaseAgent requires close() to be implemented."""
        
        class IncompleteAgent(BaseAgent):
            async def stream(self, query: str, user_config: UserConfig, task):
                yield AgentStreamResponse(
                    state=TaskState.completed,
                    content="Done"
                )
        
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteAgent()

    def test_base_agent_requires_stream_implementation(self):
        """Test that BaseAgent requires stream() to be implemented."""
        
        class IncompleteAgent(BaseAgent):
            async def close(self):
                pass
        
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteAgent()
