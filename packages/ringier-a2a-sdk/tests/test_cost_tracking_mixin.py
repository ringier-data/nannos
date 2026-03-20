"""Unit tests for CostTrackingMixin."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import TaskState

from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.cost_tracking.logger import CostLogger, set_request_access_token
from ringier_a2a_sdk.models import AgentStreamResponse


class TestAgent(BaseAgent):
    """Test agent implementation for testing CostTrackingMixin."""

    async def close(self):
        pass

    async def _stream_impl(self, query, user_config, task):
        yield AgentStreamResponse(content="test", state=TaskState.completed)


class TestCostTrackingMixin:
    """Tests for CostTrackingMixin functionality."""

    def test_mixin_initialization(self):
        """Test that mixin initializes without cost tracking enabled."""
        agent = TestAgent()

        assert not agent._cost_tracking_enabled
        assert agent._cost_logger is None
        assert agent._langchain_callbacks == []

    def test_enable_cost_tracking(self):
        """Test enabling cost tracking initializes logger and callbacks."""
        agent = TestAgent()

        # Mock CostTrackingCallback at the import location
        mock_callback_class = MagicMock()
        mock_callback_instance = MagicMock()
        mock_callback_class.return_value = mock_callback_instance

        with patch.dict(
            "sys.modules",
            {"ringier_a2a_sdk.cost_tracking.callback": MagicMock(CostTrackingCallback=mock_callback_class)},
        ):
            with patch("ringier_a2a_sdk.cost_tracking.CostTrackingCallback", mock_callback_class):
                # Enable cost tracking
                agent.enable_cost_tracking(backend_url="https://backend.test.com", batch_size=5, flush_interval=10.0)

                # Verify state is updated
                assert agent._cost_tracking_enabled
                assert agent._cost_logger is not None
                assert isinstance(agent._cost_logger, CostLogger)
                assert len(agent._langchain_callbacks) == 1
                assert agent._langchain_callbacks[0] == mock_callback_instance

    def test_enable_cost_tracking_import_error(self):
        """Test that ImportError for CostTrackingCallback is handled gracefully but cost tracking remains enabled."""
        agent = TestAgent()

        # Create a custom mock module that raises ImportError for CostTrackingCallback
        class MockCostTrackingModule:
            CostLogger = CostLogger
            set_request_access_token = MagicMock()
            get_request_access_token = MagicMock()

            def __getattr__(self, name):
                if name == "CostTrackingCallback":
                    raise ImportError("langchain_core not installed")
                raise AttributeError(f"module has no attribute {name!r}")

        with patch.dict("sys.modules", {"ringier_a2a_sdk.cost_tracking": MockCostTrackingModule()}):
            agent.enable_cost_tracking(backend_url="https://backend.test.com")

        # Cost tracking should still be enabled for manual instrumentation
        assert agent._cost_tracking_enabled
        assert agent._cost_logger is not None
        assert isinstance(agent._cost_logger, CostLogger)
        # But LangChain callbacks should be empty
        assert agent._langchain_callbacks == []

    @pytest.mark.asyncio
    async def test_manual_tracking_without_langchain(self):
        """Test that manual cost tracking works without langchain_core installed."""
        agent = TestAgent()

        # Create a custom mock module that raises ImportError for CostTrackingCallback
        class MockCostTrackingModule:
            CostLogger = CostLogger
            set_request_access_token = MagicMock()
            get_request_access_token = MagicMock()

            def __getattr__(self, name):
                if name == "CostTrackingCallback":
                    raise ImportError("langchain_core not installed")
                raise AttributeError(f"module has no attribute {name!r}")

        with patch.dict("sys.modules", {"ringier_a2a_sdk.cost_tracking": MockCostTrackingModule()}):
            agent.enable_cost_tracking(backend_url="https://backend.test.com")

        # Verify cost tracking is enabled but without LangChain callbacks
        assert agent._cost_tracking_enabled
        assert agent._cost_logger is not None
        assert agent._langchain_callbacks == []

        # Mock the logger's log_cost_async method
        agent._cost_logger.log_cost_async = MagicMock()

        # Manual reporting should still work
        await agent.report_llm_usage(
            user_sub="sub-123",
            provider="bedrock_converse",
            model_name="claude-3-sonnet",
            billing_unit_breakdown={"input_tokens": 100, "output_tokens": 50},
            conversation_id="conv-789",
        )

        # Verify the log was recorded
        agent._cost_logger.log_cost_async.assert_called_once()
        call_kwargs = agent._cost_logger.log_cost_async.call_args[1]
        assert call_kwargs["user_sub"] == "sub-123"
        assert call_kwargs["provider"] == "bedrock_converse"
        assert call_kwargs["model_name"] == "claude-3-sonnet"
        assert call_kwargs["billing_unit_breakdown"] == {"input_tokens": 100, "output_tokens": 50}

        # get_langchain_callbacks should return empty list
        callbacks = agent.get_langchain_callbacks()
        assert callbacks == []

    @pytest.mark.asyncio
    async def test_report_llm_usage_disabled(self):
        """Test that report_llm_usage does nothing when tracking is disabled."""
        agent = TestAgent()

        # Should not raise even though cost tracking is disabled
        await agent.report_llm_usage(
            user_sub="sub-123",
            provider="openai",
            model_name="gpt-4o",
            billing_unit_breakdown={"input_tokens": 100, "output_tokens": 50},
        )

    @pytest.mark.asyncio
    async def test_report_llm_usage_enabled(self):
        """Test that report_llm_usage logs cost when tracking is enabled."""
        agent = TestAgent()

        # Mock cost logger
        mock_logger = MagicMock()
        mock_logger.log_cost_async = MagicMock()
        agent._cost_logger = mock_logger
        agent._cost_tracking_enabled = True

        # Report usage
        billing_unit_breakdown = {"input_tokens": 100, "output_tokens": 50}
        conversation_id = "conv-123"

        await agent.report_llm_usage(
            user_sub="sub-123",
            provider="openai",
            model_name="gpt-4o",
            billing_unit_breakdown=billing_unit_breakdown,
            conversation_id=conversation_id,
            langsmith_run_id="run-456",
        )

        # Verify log_cost_async was called correctly
        mock_logger.log_cost_async.assert_called_once()
        call_kwargs = mock_logger.log_cost_async.call_args[1]

        assert call_kwargs["user_sub"] == "sub-123"
        assert call_kwargs["provider"] == "openai"
        assert call_kwargs["model_name"] == "gpt-4o"
        assert call_kwargs["billing_unit_breakdown"] == billing_unit_breakdown
        assert call_kwargs["conversation_id"] == conversation_id
        assert call_kwargs["langsmith_run_id"] == "run-456"
        assert call_kwargs.get("sub_agent_id") is None  # Should not be passed

    def test_get_langchain_callbacks_disabled(self):
        """Test that get_langchain_callbacks returns empty list when disabled."""
        agent = TestAgent()

        callbacks = agent.get_langchain_callbacks()

        assert callbacks == []

    def test_get_langchain_callbacks_enabled(self):
        """Test that get_langchain_callbacks returns callback list when enabled."""
        agent = TestAgent()

        mock_callback = MagicMock()
        agent._cost_tracking_enabled = True
        agent._langchain_callbacks = [mock_callback]

        callbacks = agent.get_langchain_callbacks()

        assert len(callbacks) == 1
        assert callbacks[0] == mock_callback
        # Should return a copy, not the original list
        assert callbacks is not agent._langchain_callbacks

    @pytest.mark.asyncio
    async def test_flush_cost_tracking(self):
        """Test that flush_cost_tracking calls logger shutdown."""
        agent = TestAgent()

        # Mock cost logger with async shutdown
        mock_logger = MagicMock()
        mock_logger.shutdown = AsyncMock()
        agent._cost_logger = mock_logger

        await agent.flush_cost_tracking()

        mock_logger.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_cost_tracking_no_logger(self):
        """Test that flush_cost_tracking does nothing without logger."""
        agent = TestAgent()

        # Should not raise even without logger
        await agent.flush_cost_tracking()

    def test_integration_with_langchain_model(self):
        """Test integration pattern with LangChain models."""
        agent = TestAgent()

        # Mock CostTrackingCallback at the import location
        mock_callback_class = MagicMock()
        mock_callback_instance = MagicMock()
        mock_callback_class.return_value = mock_callback_instance

        with patch.dict(
            "sys.modules",
            {"ringier_a2a_sdk.cost_tracking.callback": MagicMock(CostTrackingCallback=mock_callback_class)},
        ):
            with patch("ringier_a2a_sdk.cost_tracking.CostTrackingCallback", mock_callback_class):
                # Enable cost tracking
                agent.enable_cost_tracking(backend_url="https://backend.test.com")

                # Get callbacks for model
                callbacks = agent.get_langchain_callbacks()

                # Verify we can pass callbacks to a model
                assert len(callbacks) == 1
                assert callbacks[0] == mock_callback_instance

    def test_create_runnable_config_basic(self):
        """Test create_runnable_config creates correct config structure."""
        agent = TestAgent()
        agent._cost_tracking_enabled = True
        agent._langchain_callbacks = []

        config = agent.create_runnable_config(
            user_sub="sub-123",
            conversation_id="conv-456",
        )

        # Should be a dict (fallback when RunnableConfig not available) or RunnableConfig
        assert config is not None
        if isinstance(config, dict):
            assert config["tags"] == ["user_sub:sub-123", "conversation:conv-456"]
            assert config["callbacks"] == []
            assert config["configurable"] == {}
        else:
            # RunnableConfig object
            assert config.tags == ["user_sub:sub-123", "conversation:conv-456"]
            assert config.callbacks == []

    def test_create_runnable_config_with_checkpointer(self):
        """Test create_runnable_config with checkpointer parameters."""
        agent = TestAgent()
        agent._cost_tracking_enabled = True
        agent._langchain_callbacks = []

        mock_checkpointer = MagicMock()

        config = agent.create_runnable_config(
            user_sub="sub-123",
            conversation_id="conv-456",
            thread_id="thread-789",
            checkpoint_ns="test-namespace",
            checkpointer=mock_checkpointer,
        )

        # Check configurable dict
        if isinstance(config, dict):
            assert config["configurable"]["thread_id"] == "thread-789"
            assert config["configurable"]["checkpoint_ns"] == "test-namespace"
            assert config["configurable"]["__pregel_checkpointer"] == mock_checkpointer
        else:
            assert config.configurable["thread_id"] == "thread-789"
            assert config.configurable["checkpoint_ns"] == "test-namespace"
            assert config.configurable["__pregel_checkpointer"] == mock_checkpointer

    def test_create_runnable_config_with_callbacks(self):
        """Test create_runnable_config includes callbacks."""
        agent = TestAgent()

        # Enable cost tracking with a mock cost logger
        mock_cost_logger = MagicMock()
        agent.enable_cost_tracking(cost_logger=mock_cost_logger)

        config = agent.create_runnable_config(
            user_sub="sub-123",
            conversation_id="conv-456",
        )

        # Should include the CostTrackingCallback
        if isinstance(config, dict):
            assert len(config["callbacks"]) == 1
            # The callback should be a CostTrackingCallback instance
            from ringier_a2a_sdk.cost_tracking import CostTrackingCallback

            assert isinstance(config["callbacks"][0], CostTrackingCallback)
        else:
            assert len(config.callbacks) == 1
            from ringier_a2a_sdk.cost_tracking import CostTrackingCallback

            assert isinstance(config.callbacks[0], CostTrackingCallback)

    def test_create_runnable_config_with_sub_agent_id(self):
        """Test create_runnable_config adds sub_agent tag from ContextVar."""
        agent = TestAgent()
        agent._cost_tracking_enabled = True
        agent._langchain_callbacks = []

        # Mock the ContextVar in the utility module (where create_runnable_config is defined)
        with patch("ringier_a2a_sdk.utils.config._has_sub_agent_id_contextvar", True):
            mock_contextvar = MagicMock()
            mock_contextvar.get.return_value = 42

            with patch("ringier_a2a_sdk.utils.config.current_sub_agent_id", mock_contextvar):
                config = agent.create_runnable_config(
                    user_sub="sub-123",
                    conversation_id="conv-456",
                )

                # Should include sub_agent tag
                if isinstance(config, dict):
                    assert "sub_agent:42" in config["tags"]
                    assert config["tags"] == ["user_sub:sub-123", "conversation:conv-456", "sub_agent:42"]
                else:
                    assert "sub_agent:42" in config.tags
                    assert config.tags == ["user_sub:sub-123", "conversation:conv-456", "sub_agent:42"]

    def test_create_runnable_config_with_extra_configurable(self):
        """Test create_runnable_config accepts extra configurable parameters."""
        agent = TestAgent()
        agent._cost_tracking_enabled = True
        agent._langchain_callbacks = []

        config = agent.create_runnable_config(
            user_sub="sub-123",
            conversation_id="conv-456",
            custom_param="custom-value",
            another_param=123,
        )

        # Should include extra parameters in configurable
        if isinstance(config, dict):
            assert config["configurable"]["custom_param"] == "custom-value"
            assert config["configurable"]["another_param"] == 123
        else:
            assert config.configurable["custom_param"] == "custom-value"
            assert config.configurable["another_param"] == 123

    def test_create_runnable_config_disabled_tracking(self):
        """Test create_runnable_config works when cost tracking is disabled."""
        agent = TestAgent()
        agent._cost_tracking_enabled = False

        config = agent.create_runnable_config(
            user_sub="sub-123",
            conversation_id="conv-456",
        )

        # Should still create config, just with empty callbacks
        assert config is not None
        if isinstance(config, dict):
            assert config["callbacks"] == []
        else:
            assert config.callbacks == []

    @pytest.mark.asyncio
    async def test_cost_logger_token_attached_to_record(self):
        """Test CostLogger batching and token propagation using httpx.AsyncClient.post patching."""
        from unittest.mock import AsyncMock, patch

        backend_url = "http://test-backend"
        post_calls = []

        async def mock_post(url, json, headers, timeout):
            post_calls.append(
                {
                    "url": url,
                    "json": json,
                    "headers": headers,
                    "timeout": timeout,
                }
            )

            class MockResponse:
                status_code = 201
                text = "ok"

            return MockResponse()

        logger = CostLogger(backend_url=backend_url, batch_size=2)
        await logger.start()

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            # Simulate two requests with different tokens
            set_request_access_token("tokenA")
            logger.log_cost_async(
                user_sub="subA",
                provider="openai",
                model_name="gpt-4o",
                billing_unit_breakdown={"input_tokens": 10, "output_tokens": 5},
                conversation_id="convA",
            )
            set_request_access_token("tokenB")
            logger.log_cost_async(
                user_sub="subB",
                provider="openai",
                model_name="gpt-4o",
                billing_unit_breakdown={"input_tokens": 20, "output_tokens": 10},
                conversation_id="convB",
            )
            set_request_access_token(None)

            # Force flush to trigger batch send
            await logger.flush()

        # There should be two post calls, one for each token group
        assert len(post_calls) == 2
        tokens = set(call["headers"]["Authorization"].split()[1] for call in post_calls)
        assert tokens == {"tokenA", "tokenB"}
        payloads = [call["json"]["logs"] for call in post_calls]
        assert any(r["conversation_id"] == "convA" for r in payloads[0] + payloads[1])
        assert any(r["conversation_id"] == "convB" for r in payloads[0] + payloads[1])
