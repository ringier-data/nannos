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
                super().__init__()
                self.closed = False

            async def close(self):
                self.closed = True

            async def _stream_impl(self, query: str, user_config: UserConfig, task):
                yield AgentStreamResponse(state=TaskState.working, content="Processing...")
                yield AgentStreamResponse(state=TaskState.completed, content=f"Result for: {query}")

        # Create instance
        agent = ConcreteAgent()
        assert not agent.closed

        # Test stream method
        user_config = UserConfig(
            user_id="test-user", access_token=SecretStr("test-token"), name="Test User", email="test@example.com"
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
            async def _stream_impl(self, query: str, user_config: UserConfig, task):
                yield AgentStreamResponse(state=TaskState.completed, content="Done")

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteAgent()

    @pytest.mark.asyncio
    async def test_stream_sets_context_variables(self):
        """Test that stream() sets user_id and access_token in context variables."""
        from unittest.mock import Mock

        from ringier_a2a_sdk.cost_tracking.logger import (
            get_request_access_token,
            get_request_user_id,
        )

        class ConcreteAgent(BaseAgent):
            def __init__(self):
                super().__init__()
                self.captured_user_id = None
                self.captured_token = None

            async def close(self):
                pass

            async def _stream_impl(self, query: str, user_config: UserConfig, task):
                # Capture context variables during stream execution
                self.captured_user_id = get_request_user_id()
                self.captured_token = get_request_access_token()

                yield AgentStreamResponse(state=TaskState.completed, content="Done")

        agent = ConcreteAgent()

        # Create user config with credentials
        user_config = UserConfig(
            user_id="test-user-123",
            access_token=SecretStr("test-token-abc"),
            name="Test User",
            email="test@example.com",
        )

        # Create a mock task
        task = Mock()
        task.id = "task-1"
        task.context_id = "ctx-1"

        # Stream should set context variables
        responses = []
        async for response in agent.stream("test query", user_config, task):
            responses.append(response)

        # Verify context variables were set during stream execution
        assert agent.captured_user_id == "test-user-123"
        assert agent.captured_token == "test-token-abc"
        assert len(responses) == 1

    @pytest.mark.asyncio
    async def test_get_request_credentials_helper(self):
        """Test that get_request_credentials() returns both user_id and access_token."""
        from unittest.mock import Mock

        from ringier_a2a_sdk.cost_tracking.logger import get_request_credentials

        class ConcreteAgent(BaseAgent):
            def __init__(self):
                super().__init__()
                self.captured_credentials = None

            async def close(self):
                pass

            async def _stream_impl(self, query: str, user_config: UserConfig, task):
                # Use convenience function to get both at once
                self.captured_credentials = get_request_credentials()

                yield AgentStreamResponse(state=TaskState.completed, content="Done")

        agent = ConcreteAgent()

        user_config = UserConfig(
            user_id="user-456", access_token=SecretStr("token-xyz"), name="Test User", email="test@example.com"
        )

        task = Mock()
        task.id = "task-2"
        task.context_id = "ctx-2"

        async for _ in agent.stream("test", user_config, task):
            pass

        # Verify tuple unpacking works
        user_id, token = agent.captured_credentials
        assert user_id == "user-456"
        assert token == "token-xyz"

    @pytest.mark.asyncio
    async def test_context_variables_isolated_per_request(self):
        """Test that context variables are isolated between concurrent requests."""
        import asyncio
        from unittest.mock import Mock

        from ringier_a2a_sdk.cost_tracking.logger import get_request_credentials

        class ConcreteAgent(BaseAgent):
            def __init__(self):
                super().__init__()

            async def close(self):
                pass

            async def _stream_impl(self, query: str, user_config: UserConfig, task):
                # Capture credentials at start
                user_id, token = get_request_credentials()

                # Simulate some async work
                await asyncio.sleep(0.01)

                # Verify credentials are still the same after async operations
                user_id_after, token_after = get_request_credentials()

                yield AgentStreamResponse(
                    state=TaskState.completed, content=f"user={user_id},{token}|after={user_id_after},{token_after}"
                )

        agent = ConcreteAgent()

        # Create two different user configs
        user_config_1 = UserConfig(
            user_id="user-1", access_token=SecretStr("token-1"), name="User 1", email="user1@example.com"
        )

        user_config_2 = UserConfig(
            user_id="user-2", access_token=SecretStr("token-2"), name="User 2", email="user2@example.com"
        )

        task1 = Mock()
        task1.id = "task-1"
        task1.context_id = "ctx-1"

        task2 = Mock()
        task2.id = "task-2"
        task2.context_id = "ctx-2"

        # Run two streams concurrently
        async def run_stream(user_config, task, expected_user, expected_token):
            responses = []
            async for response in agent.stream("test", user_config, task):
                responses.append(response)

            # Verify the response contains the correct credentials
            content = responses[0].content
            assert f"user={expected_user},{expected_token}" in content
            assert f"after={expected_user},{expected_token}" in content
            return responses

        # Execute both streams concurrently
        results = await asyncio.gather(
            run_stream(user_config_1, task1, "user-1", "token-1"),
            run_stream(user_config_2, task2, "user-2", "token-2"),
        )

        # Both should complete successfully with isolated contexts
        assert len(results) == 2
        assert len(results[0]) == 1
        assert len(results[1]) == 1

    @pytest.mark.asyncio
    async def test_context_variables_without_access_token(self):
        """Test that stream() handles missing access_token gracefully."""
        from unittest.mock import Mock

        from ringier_a2a_sdk.cost_tracking.logger import get_request_credentials

        class ConcreteAgent(BaseAgent):
            def __init__(self):
                super().__init__()
                self.captured_credentials = None

            async def close(self):
                pass

            async def _stream_impl(self, query: str, user_config: UserConfig, task):
                self.captured_credentials = get_request_credentials()

                yield AgentStreamResponse(state=TaskState.completed, content="Done")

        agent = ConcreteAgent()

        # User config without access_token
        user_config = UserConfig(user_id="user-no-token", name="Test User", email="test@example.com")

        task = Mock()
        task.id = "task-1"
        task.context_id = "ctx-1"

        async for _ in agent.stream("test", user_config, task):
            pass

        # User ID should be set, but token should be None
        user_id, token = agent.captured_credentials
        assert user_id == "user-no-token"
        assert token is None

    def test_base_agent_requires_stream_implementation(self):
        """Test that BaseAgent requires _stream_impl() to be implemented."""

        class IncompleteAgent(BaseAgent):
            async def close(self):
                pass

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteAgent()
