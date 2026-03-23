"""LangGraph + Anthropic API agent implementation.

This module provides an Anthropic-specific subclass of LangGraphAgent that uses
the Anthropic API directly (via langchain-anthropic) for the LLM and DynamoDB for checkpointing.
"""

import logging
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from .dynamodb_checkpointer_mixin import DynamoDBCheckpointerMixin
from .langgraph import FinalResponseSchema, LangGraphAgent  # noqa: F401

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


class LangGraphAnthropicAgent(DynamoDBCheckpointerMixin, LangGraphAgent):
    """LangGraph agent using Anthropic API directly and DynamoDB checkpointing.

    Supports both standard Claude models and extended thinking mode for complex reasoning.

    This is a concrete implementation of LangGraphAgent that:
    - Uses Anthropic API (Claude models) as the LLM
    - Uses DynamoDB checkpointers with optional S3 offloading
    - Optionally enables Claude extended thinking mode

    Configuration:
    - ANTHROPIC_API_KEY: Anthropic API key (required, can be set via environment)
    - ANTHROPIC_MODEL_ID: Model ID (default: claude-3-5-sonnet-20241022)
    - ANTHROPIC_THINKING_LEVEL: Thinking level (minimal/low/medium/high, optional)
    - ANTHROPIC_TIMEOUT: Request timeout in seconds (default: 300)
    - ANTHROPIC_MAX_RETRIES: Maximum retries (default: 3)

    Subclasses must still implement:
    - _get_mcp_connections(): Return MCP server connection configuration
    - _get_system_prompt(): Return agent-specific system prompt
    - _get_checkpoint_namespace(): Return unique checkpoint namespace

    Optional overrides:
    - _get_anthropic_model_id(): Return Anthropic model ID (default: Claude 3.5 Sonnet)
    - _get_thinking_level(): Return thinking level (minimal/low/medium/high, default: None for disabled)
    - _get_middleware(): Return agent middleware list (default: [])
    - _get_tool_interceptors(): Return tool interceptors (default: [])
    - _create_graph(): Create LangGraph with tools (has default implementation)
    """

    def __init__(self, tool_query_regex: str | None = None):
        """Initialize the LangGraph Anthropic Agent.

        Sets up Anthropic configuration before calling the generic LangGraphAgent init,
        which will call _create_model() and _create_checkpointer().
        """
        # Anthropic configuration (needed by _create_model before __init__ calls it)

        self.anthropic_model_id = self._get_anthropic_model_id()
        self.thinking_level = self._get_thinking_level()

        raw_key = os.getenv("ANTHROPIC_API_KEY")

        if not raw_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required for LangGraphAnthropicAgent")
        self.anthropic_api_key = SecretStr(raw_key)
        super().__init__(tool_query_regex=tool_query_regex)

    def _create_model(self) -> BaseChatModel:
        """Create ChatAnthropic model with optional extended thinking.

        Supports Claude extended thinking mode for complex reasoning tasks.
        Temperature is set to 1.0 when thinking is enabled, 0 otherwise.

        Returns:
            ChatAnthropic model instance
        """
        # Configure thinking mode if enabled
        thinking_config = None
        temperature = 0

        if self.thinking_level:
            budget_tokens = _get_thinking_budget(self.thinking_level)
            thinking_config = {"type": "enabled", "budget_tokens": budget_tokens}
            temperature = 1.0  # CRITICAL: Required when extended thinking is enabled
            logger.info(
                f"Claude extended thinking enabled with level={self.thinking_level}, budget={budget_tokens} tokens"
            )

        timeout = int(os.getenv("ANTHROPIC_TIMEOUT", "300"))
        max_retries = int(os.getenv("ANTHROPIC_MAX_RETRIES", "3"))

        model = ChatAnthropic(
            api_key=self.anthropic_api_key,
            model_name=self.anthropic_model_id,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            thinking=thinking_config,
            stop=None,
        )

        logger.info(
            f"Initialized Anthropic model: {self.anthropic_model_id}, "
            f"thinking_level={self.thinking_level}, temperature={temperature}, "
            f"timeout={timeout}s, max_retries={max_retries}"
        )
        return model

    # Optional overrides with defaults

    def _get_anthropic_model_id(self) -> str:
        """Return Anthropic model ID. Default: Claude 3.5 Sonnet via env var."""
        return os.getenv("ANTHROPIC_MODEL_ID", "claude-3-5-sonnet-20241022")

    def _get_thinking_level(self) -> str | None:
        """Return thinking level if enabled. Can be: minimal, low, medium, high. Default: None (disabled)."""
        return os.getenv("ANTHROPIC_THINKING_LEVEL")

    def _create_graph(self, tools):
        """Create LangGraph with thinking-aware response format.

        When extended thinking is enabled, the Anthropic API rejects tool_choice
        that forces tool use (same restriction as Bedrock). Uses response_format=None
        with an explicit FinalResponseSchema tool instead.

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
