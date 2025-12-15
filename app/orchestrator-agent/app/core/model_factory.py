"""
Model factory utilities for creating LangChain models.

This module provides utility functions for creating and configuring LangChain models
without introducing circular dependencies. It's used by both GraphFactory and
other components that need to create models dynamically.
"""

import logging
import os
from typing import Any, Literal

import boto3
from botocore.config import Config as BotoConfig
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_openai import AzureChatOpenAI

# Model type literal for type safety (duplicated here to avoid circular import)
ModelType = Literal["gpt4o", "claude-sonnet-4.5"]


logger = logging.getLogger(__name__)


def create_model(model_type: ModelType, config: Any, thinking: bool = False) -> BaseChatModel:
    """Create a model instance for the given model type.

    Utility function that can be used by both GraphFactory and other components
    that need to create models dynamically.

    Args:
        model_type: The type of model to create ('gpt4o' or 'claude-sonnet-4.5')
        config: Agent settings with model configuration
        thinking: Enable thinking mode for Claude models

    Returns:
        BaseChatModel: The created model instance
    """
    if model_type == "claude-sonnet-4.5":
        if thinking:
            thinking_params = {"type": "enabled", "budget_tokens": 1024}
            temperature = 1.0
        else:
            thinking_params = {"type": "disabled", "budget_tokens": 0}
            temperature = 0.0

        # Configure boto3 client with timeouts and retry logic from environment variables
        # to handle long-running Claude Sonnet 4.5 requests
        read_timeout = int(os.getenv("BEDROCK_READ_TIMEOUT", "300"))  # Default: 5 minutes
        connect_timeout = int(os.getenv("BEDROCK_CONNECT_TIMEOUT", "10"))  # Default: 10 seconds
        max_attempts = int(os.getenv("BEDROCK_MAX_RETRY_ATTEMPTS", "3"))  # Default: 3 retries
        retry_mode = os.getenv("BEDROCK_RETRY_MODE", "adaptive")  # Default: adaptive

        boto_config = BotoConfig(
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
            retries={
                "max_attempts": max_attempts,
                "mode": retry_mode,
            },
        )

        # Create bedrock-runtime client with custom configuration
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=config.get_bedrock_region(),
            config=boto_config,
        )

        logger.info(
            f"Created Bedrock client with read_timeout={read_timeout}s, "
            f"connect_timeout={connect_timeout}s, max_retry_attempts={max_attempts} ({retry_mode} mode)"
        )

        return ChatBedrockConverse(
            client=bedrock_client,
            model=config.get_bedrock_model_id(),
            temperature=temperature,
            region_name=config.get_bedrock_region(),
            additional_model_request_fields={"thinking": thinking_params}
            if thinking_params["type"] == "enabled"
            else {},
        )
    else:
        # Default to gpt4o (Azure OpenAI)
        if thinking:
            logger.warning("Thinking mode is only supported for Claude Sonnet 4.5 model.")

        return AzureChatOpenAI(
            azure_deployment=config.get_azure_deployment(),
            temperature=0.7,
            model=config.get_azure_model_name(),
        )
