"""Unit tests for thinking level → reasoning_effort and gateway model creation."""

import os
from unittest.mock import Mock, patch

from agent_common.core.model_factory import create_model, get_reasoning_effort
from agent_common.models.base import ThinkingLevel

from app.models.config import AgentSettings

_GW_ENV = {"LLM_GATEWAY_URL": "http://litellm-proxy.test", "LLM_GATEWAY_API_KEY": "sk-test"}


class TestGetReasoningEffort:
    """thinking_level → unified reasoning_effort (ADR-0003)."""

    def test_mapping(self):
        assert get_reasoning_effort(ThinkingLevel.minimal) == "low"  # no distinct tier; Bedrock floors at 1024
        assert get_reasoning_effort(ThinkingLevel.low) == "low"
        assert get_reasoning_effort(ThinkingLevel.medium) == "medium"
        assert get_reasoning_effort(ThinkingLevel.high) == "high"

    def test_none(self):
        assert get_reasoning_effort(None) is None


class TestCreateModelGateway:
    """create_model builds a single gateway-backed ChatOpenAI (no provider branches)."""

    @patch.dict(os.environ, _GW_ENV)
    @patch("langchain_openai.ChatOpenAI")
    def test_thinking_model_sets_reasoning_effort(self, mock_chat):
        create_model("claude-sonnet-4.6", "eu-central-1", thinking_level=ThinkingLevel.high)
        kwargs = mock_chat.call_args[1]
        assert kwargs["model"] == "claude-sonnet-4.6"
        assert kwargs["base_url"] == "http://litellm-proxy.test"
        assert kwargs["stream_usage"] is True
        assert kwargs["model_kwargs"]["reasoning_effort"] == "high"

    @patch.dict(os.environ, _GW_ENV)
    @patch("langchain_openai.ChatOpenAI")
    def test_effort_always_forwarded(self, mock_chat):
        # No per-model capability table in the app: reasoning_effort is always
        # forwarded when a thinking_level is set; the gateway drops it for
        # non-reasoning models via drop_params (ADR-0003).
        create_model("gpt-4o", thinking_level=ThinkingLevel.low)
        kwargs = mock_chat.call_args[1]
        assert kwargs["model_kwargs"]["reasoning_effort"] == "low"

    @patch.dict(os.environ, _GW_ENV)
    @patch("langchain_openai.ChatOpenAI")
    def test_no_thinking_level(self, mock_chat):
        create_model("claude-sonnet-4.6", thinking_level=None)
        kwargs = mock_chat.call_args[1]
        assert kwargs["model_kwargs"] == {}

    def test_missing_gateway_url_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_GATEWAY_URL", None)
            try:
                create_model("claude-sonnet-4.6")
                assert False, "expected RuntimeError when LLM_GATEWAY_URL unset"
            except RuntimeError as e:
                assert "LLM_GATEWAY_URL" in str(e)


class TestThinkingLevelCaching:
    """GraphFactory caches models by (model_type, thinking_level)."""

    @patch.dict(os.environ, _GW_ENV)
    @patch("langchain_openai.ChatOpenAI")
    def test_different_thinking_levels_create_separate_instances(self, mock_chat):
        from app.core.graph_factory import GraphFactory

        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_TTL_DAYS = 30
        config.POSTGRES_USER = "test"
        config.POSTGRES_PASSWORD = "test"
        config.POSTGRES_HOST = "localhost"
        config.POSTGRES_PORT = 5432
        config.POSTGRES_DB = "test"
        config.POSTGRES_SCHEMA = "public"
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2
        config.get_bedrock_region.return_value = "eu-central-1"

        with patch("agent_common.core.cost_tracking_embeddings.CostTrackingBedrockEmbeddings"):
            factory = GraphFactory(config)
            factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.low)
            factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.high)
            assert len(factory._models) == 2
            assert ("claude-sonnet-4.5", ThinkingLevel.low) in factory._models
            assert ("claude-sonnet-4.5", ThinkingLevel.high) in factory._models

    @patch.dict(os.environ, _GW_ENV)
    @patch("langchain_openai.ChatOpenAI")
    def test_same_thinking_level_reuses_instance(self, mock_chat):
        from app.core.graph_factory import GraphFactory

        config = Mock(spec=AgentSettings)
        config.CHECKPOINT_TTL_DAYS = 30
        config.POSTGRES_USER = "test"
        config.POSTGRES_PASSWORD = "test"
        config.POSTGRES_HOST = "localhost"
        config.POSTGRES_PORT = 5432
        config.POSTGRES_DB = "test"
        config.POSTGRES_SCHEMA = "public"
        config.MAX_RETRIES = 3
        config.BACKOFF_FACTOR = 2
        config.get_bedrock_region.return_value = "eu-central-1"

        with patch("agent_common.core.cost_tracking_embeddings.CostTrackingBedrockEmbeddings"):
            factory = GraphFactory(config)
            model1 = factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.low)
            model2 = factory._get_or_create_model("claude-sonnet-4.5", ThinkingLevel.low)
            assert model1 is model2
            assert len(factory._models) == 1
