"""LangGraph + Bedrock agent implementation.

This module provides a Bedrock-specific subclass of LangGraphAgent that uses
AWS Bedrock for the LLM and DynamoDB for checkpointing.
"""

import logging
import os

import boto3
from botocore.config import Config as BotoConfig
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from .dynamodb_checkpointer_mixin import DynamoDBCheckpointerMixin
from .langgraph import LangGraphAgent

logger = logging.getLogger(__name__)


def _get_thinking_budget(thinking_level: str) -> int:
    """Map thinking level to Claude token budget.

    Based on Anthropic's official documentation:
    - minimal: 1024 tokens (hard minimum, simple queries)
    - low: 4096 tokens (standard agent tasks, balanced default)
    - medium: 10000 tokens (complex reasoning, multi-step analysis)
    - high: 16000 tokens (very complex problems, deep analysis)

    Args:
        thinking_level: The thinking depth level (minimal, low, medium, high).

    Returns:
        Token budget for Claude extended thinking.
    """
    budget_map = {
        "minimal": 1024,
        "low": 4096,
        "medium": 10000,
        "high": 16000,
    }
    return budget_map.get(thinking_level, 4096)


# Re-export FinalResponseSchema from langgraph module for backward compatibility
from .langgraph import FinalResponseSchema  # noqa: F401, E402


class LangGraphBedrockAgent(DynamoDBCheckpointerMixin, LangGraphAgent):
    """LangGraph agent using AWS Bedrock and DynamoDB checkpointing.

    Supports both standard Claude models and extended thinking mode for complex reasoning.

    This is a concrete implementation of LangGraphAgent that:
    - Uses AWS Bedrock (Claude/other models) as the LLM
    - Uses DynamoDB checkpointers with optional S3 offloading
    - Optionally enables Claude extended thinking mode

    Configuration:
    - AWS_BEDROCK_REGION: AWS region (default: eu-central-1)
    - BEDROCK_MODEL_ID: Model ID (default: claude-sonnet-4-5)
    - BEDROCK_THINKING_LEVEL: Thinking level (minimal/low/medium/high, optional)
    - BEDROCK_READ_TIMEOUT, BEDROCK_CONNECT_TIMEOUT, etc.: Bedrock client config

    Subclasses must still implement:
    - _get_mcp_connections(): Return MCP server connection configuration
    - _get_system_prompt(): Return agent-specific system prompt
    - _get_checkpoint_namespace(): Return unique checkpoint namespace

    Optional overrides:
    - _get_bedrock_model_id(): Return Bedrock model ID (default: Claude Sonnet 4.5)
    - _get_thinking_level(): Return thinking level (minimal/low/medium/high, default: None for disabled)
    - _get_middleware(): Return agent middleware list (default: [])
    - _get_tool_interceptors(): Return tool interceptors (default: [])
    - _create_graph(): Create LangGraph with tools (has default implementation)
    """

    def __init__(self, tool_query_regex: str | None = None):
        """Initialize the LangGraph Bedrock Agent.

        Sets up Bedrock configuration before calling the generic LangGraphAgent init,
        which will call _create_model() and _create_checkpointer().
        """
        # Bedrock configuration (needed by _create_model before __init__ calls it)
        self.bedrock_region = os.getenv("AWS_BEDROCK_REGION", "eu-central-1")
        self.bedrock_model_id = self._get_bedrock_model_id()
        self.thinking_level = self._get_thinking_level()

        super().__init__(tool_query_regex=tool_query_regex)

    def _create_model(self) -> BaseChatModel:
        """Create ChatBedrockConverse model with optional extended thinking.

        Supports Claude extended thinking mode for complex reasoning tasks.
        Temperature is set to 1.0 when thinking is enabled, 0 otherwise.

        Returns:
            ChatBedrockConverse model instance
        """
        bedrock_client = self._create_bedrock_client()

        # Configure thinking mode if enabled
        thinking_params = None
        temperature = 0
        if self.thinking_level:
            budget_tokens = _get_thinking_budget(self.thinking_level)
            thinking_params = {"type": "enabled", "budget_tokens": budget_tokens}
            temperature = 1.0  # CRITICAL: Required when extended thinking is enabled
            logger.info(
                f"Claude extended thinking enabled with level={self.thinking_level}, budget={budget_tokens} tokens"
            )

        additional_fields = {}
        if thinking_params:
            additional_fields["thinking"] = thinking_params

        model = ChatBedrockConverse(
            client=bedrock_client,
            region_name=self.bedrock_region,
            model=self.bedrock_model_id,
            temperature=temperature,
            additional_model_request_fields=additional_fields if additional_fields else None,
        )

        logger.info(
            f"Initialized Bedrock model: {self.bedrock_model_id}, "
            f"thinking_level={self.thinking_level}, temperature={temperature}"
        )
        return model

    def _create_bedrock_client(self) -> boto3.client:
        """Create configured Bedrock client from environment variables."""
        read_timeout = int(os.getenv("BEDROCK_READ_TIMEOUT", "300"))
        connect_timeout = int(os.getenv("BEDROCK_CONNECT_TIMEOUT", "10"))
        max_attempts = int(os.getenv("BEDROCK_MAX_RETRY_ATTEMPTS", "3"))
        retry_mode = os.getenv("BEDROCK_RETRY_MODE", "adaptive")

        boto_config = BotoConfig(
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
            retries={
                "max_attempts": max_attempts,
                "mode": retry_mode,
            },
        )

        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=self.bedrock_region,
            config=boto_config,
        )

        logger.info(
            f"Created Bedrock client with read_timeout={read_timeout}s, "
            f"connect_timeout={connect_timeout}s, max_retry_attempts={max_attempts} ({retry_mode} mode)"
        )

        return bedrock_client

    # Optional override with default

    def _get_bedrock_model_id(self) -> str:
        """Return Bedrock model ID. Default: Claude Sonnet 4.5 via env var."""
        return os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0")

    def _get_thinking_level(self) -> str | None:
        """Return thinking level if enabled. Can be: minimal, low, medium, high. Default: None (disabled)."""
        return os.getenv("BEDROCK_THINKING_LEVEL")

    def _create_graph(self, tools: list[BaseTool]) -> CompiledStateGraph:
        """Create LangGraph with thinking-aware response format.

        When extended thinking is enabled, Bedrock's API cannot handle forcing
        structured output via AutoStrategy. Uses response_format=None with an
        explicit FinalResponseSchema tool instead, matching the orchestrator's
        approach in graph_factory.py.

        Without thinking, uses the default AutoStrategy from the base class.
        """
        if self.thinking_level:
            from deepagents import create_deep_agent

            return create_deep_agent(
                model=self._model,
                tools=tools + [self._create_response_tool()],
                subagents=[],
                system_prompt=self._get_system_prompt(),
                checkpointer=self._checkpointer,
                middleware=self._get_middleware(),
                response_format=None,
            )
        return super()._create_graph(tools)
