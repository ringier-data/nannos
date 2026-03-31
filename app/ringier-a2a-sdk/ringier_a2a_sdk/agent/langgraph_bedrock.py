"""LangGraph + Bedrock agent implementation.

This module provides a Bedrock-specific subclass of LangGraphAgent that uses
AWS Bedrock for the LLM and DynamoDB for checkpointing.
"""

import logging
import os

import boto3
import httpx
from botocore.config import Config as BotoConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    HumanMessage,
    ImageContentBlock,
    TextContentBlock,
)
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from ..middleware.bedrock_prompt_caching import BedrockPromptCachingMiddleware
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

    def __init__(self, tool_query_regex: str | None = None, recursion_limit: int | None = None):
        """Initialize the LangGraph Bedrock Agent.

        Sets up Bedrock configuration before calling the generic LangGraphAgent init,
        which will call _create_model() and _create_checkpointer().

        Args:
            tool_query_regex: Optional regex pattern to filter MCP tools by name
            recursion_limit: Maximum number of LangGraph steps (default: from LANGGRAPH_RECURSION_LIMIT env var or 50)
        """
        # Bedrock configuration (needed by _create_model before __init__ calls it)
        self.bedrock_region = os.getenv("AWS_BEDROCK_REGION", "eu-central-1")
        self.bedrock_model_id = self._get_bedrock_model_id()
        self.thinking_level = self._get_thinking_level()

        super().__init__(tool_query_regex=tool_query_regex, recursion_limit=recursion_limit)

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

    def _get_middleware(self) -> list[AgentMiddleware]:
        """Return agent middleware with Bedrock prompt caching and schema cleaning.

        Adds BedrockPromptCachingMiddleware before base middleware
        (ToolSchemaCleaningMiddleware) to ensure correct execution order:
        1. Prompt caching adds cache points to the request structure
        2. Schema cleaning converts tools to final format right before model

        Subclasses should call super()._get_middleware() to preserve this order.

        Returns:
            List of middleware: [BedrockPromptCachingMiddleware, ToolSchemaCleaningMiddleware]
        """
        return [BedrockPromptCachingMiddleware()] + super()._get_middleware()

    async def _preprocess_input_messages(self, messages: list[HumanMessage]) -> list[HumanMessage]:
        """Convert URL-based images to inline base64 for Bedrock Converse API.

        Bedrock's Converse API requires images as inline base64 data, not URLs.
        This downloads images from pre-signed S3 URLs and converts them to base64
        before passing to the graph.
        """
        import base64 as b64

        processed = []
        for msg in messages:
            # TODO: looks that just HumanMessage.content_blocks has proper typing.
            #       Unfortunately, just HumanMessage.content is guaranteed to be always set,
            #       so we here we use HumanMessage.content as the getter and discriminate based on the
            #       attributes of the dict instead of from the type.
            content = msg.content
            if not isinstance(content, list):
                processed.append(msg)
                continue

            needs_conversion = any(
                isinstance(b, dict) and b.get("type") == "image" and "url" in b and "base64" not in b for b in content
            )
            if not needs_conversion:
                processed.append(msg)
                continue

            new_blocks = []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "image"
                    and "url" in block
                    and "base64" not in block
                ):
                    url = block["url"]
                    mime_type = block.get("mime_type", "image/png")
                    filename = url.split("/")[-1].split("?")[0] if url else "unknown"
                    try:
                        async with httpx.AsyncClient(follow_redirects=True) as client:
                            resp = await client.get(url, timeout=60.0)
                            resp.raise_for_status()
                            b64_data = b64.b64encode(resp.content).decode("utf-8")
                        # Bedrock only sees the base64 pixels; include URL as text
                        # so the LLM can reference it in tool call arguments.
                        new_blocks.append(
                            TextContentBlock(
                                type="text",
                                text=f"[Attached image: {filename}, URL: {url}]",
                            )
                        )
                        new_blocks.append(
                            ImageContentBlock(
                                type="image",
                                base64=b64_data,
                                mime_type=mime_type,
                            )
                        )
                        logger.info(f"Converted URL image to inline base64 ({len(b64_data)} chars)")
                    except Exception:
                        logger.warning(
                            "Failed to download image from URL, converting to text description", exc_info=True
                        )
                        new_blocks.append(
                            TextContentBlock(
                                type="text",
                                text=f"[Image: {filename} ({mime_type}), URL: {url}] (could not load from URL)",
                            )
                        )
                else:
                    new_blocks.append(block)

            processed.append(HumanMessage(content=new_blocks))
        return processed

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
