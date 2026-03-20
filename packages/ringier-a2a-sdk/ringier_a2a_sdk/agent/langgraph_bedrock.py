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
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph_checkpoint_aws import DynamoDBSaver

from .langgraph import LangGraphAgent

logger = logging.getLogger(__name__)


# Re-export FinalResponseSchema from langgraph module for backward compatibility
from .langgraph import FinalResponseSchema  # noqa: F401, E402


class LangGraphBedrockAgent(LangGraphAgent):
    """LangGraph agent using AWS Bedrock and DynamoDB checkpointing.

    This is a concrete implementation of LangGraphAgent that:
    - Uses AWS Bedrock (Claude/other models) as the LLM
    - Uses DynamoDB checkpointers with optional S3 offloading

    Subclasses must still implement:
    - _get_mcp_connections(): Return MCP server connection configuration
    - _get_system_prompt(): Return agent-specific system prompt
    - _get_checkpoint_namespace(): Return unique checkpoint namespace

    Optional overrides:
    - _get_bedrock_model_id(): Return Bedrock model ID (default: Claude Sonnet 4.5)
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

        super().__init__(tool_query_regex=tool_query_regex)

    def _create_model(self) -> BaseChatModel:
        """Create ChatBedrockConverse model via boto3.

        Returns:
            ChatBedrockConverse model instance
        """
        bedrock_client = self._create_bedrock_client()

        model = ChatBedrockConverse(
            client=bedrock_client,
            region_name=self.bedrock_region,
            model=self.bedrock_model_id,
            temperature=0,
        )

        logger.info(f"Initialized Bedrock model (callbacks will be set at runtime): {self.bedrock_model_id}")
        return model

    def _create_checkpointer(self) -> BaseCheckpointSaver:
        """Create DynamoDB checkpointer with optional S3 offloading.

        Reads configuration from:
        - CHECKPOINT_DYNAMODB_TABLE_NAME (required)
        - CHECKPOINT_AWS_REGION (default: eu-central-1)
        - CHECKPOINT_TTL_DAYS (default: 14)
        - CHECKPOINT_COMPRESSION_ENABLED (default: true)
        - CHECKPOINT_S3_BUCKET_NAME (optional, enables S3 offloading)

        Returns:
            Configured DynamoDBSaver instance

        Raises:
            ValueError: If CHECKPOINT_DYNAMODB_TABLE_NAME is not set
        """
        checkpoint_table = os.getenv("CHECKPOINT_DYNAMODB_TABLE_NAME")
        if not checkpoint_table:
            raise ValueError("CHECKPOINT_DYNAMODB_TABLE_NAME environment variable is required")

        checkpoint_region = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
        checkpoint_ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
        checkpoint_compression = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"
        checkpoint_s3_bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")

        s3_config = None
        if checkpoint_s3_bucket:
            s3_config = {"bucket_name": checkpoint_s3_bucket}
            logger.info(f"S3 offloading enabled for large checkpoints: {checkpoint_s3_bucket}")

        checkpointer = DynamoDBSaver(
            table_name=checkpoint_table,
            region_name=checkpoint_region,
            ttl_seconds=checkpoint_ttl_days * 24 * 60 * 60,
            enable_checkpoint_compression=checkpoint_compression,
            s3_offload_config=s3_config,  # type: ignore[arg-type]
        )

        logger.info(f"Initialized DynamoDB checkpointer: {checkpoint_table}")
        return checkpointer

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
