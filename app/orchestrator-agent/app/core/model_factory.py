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
ModelType = Literal["gpt4o", "gpt-4o-mini", "claude-sonnet-4.5", "claude-haiku-4-5"]

# Model-specific configuration
MODEL_CONFIG = {
    "gpt4o": {
        "api_version": "2024-08-01-preview",
        "deployment": "chatgpt-4o",
        "model_name": "gpt-4o",
    },
    "gpt-4o-mini": {
        "api_version": "2025-01-01-preview",
        "deployment": "gpt-4o-mini",
        "model_name": "gpt-4o-mini",
    },
    "claude-sonnet-4.5": {
        "bedrock_model_id": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    },
    "claude-haiku-4-5": {
        "bedrock_model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    },
}

logger = logging.getLogger(__name__)


def create_model(
    model_type: ModelType, config: Any, thinking: bool = False, callbacks: list | None = None
) -> BaseChatModel:
    """Create a model instance for the given model type.

    Utility function that can be used by both GraphFactory and other components
    that need to create models dynamically.

    Args:
        model_type: The type of model to create ('gpt4o', 'gpt-4o-mini', 'claude-sonnet-4.5', or 'claude-haiku-4-5')
        config: Agent settings with model configuration
        thinking: Enable thinking mode for Claude Sonnet models (not supported on Haiku)
        callbacks: Optional list of LangChain callbacks (e.g., for cost tracking)

    Returns:
        BaseChatModel: The created model instance
    """
    if model_type in ("claude-sonnet-4.5", "claude-haiku-4-5"):
        # Thinking mode only supported on Claude Sonnet, not Haiku
        if thinking and model_type == "claude-sonnet-4.5":
            thinking_params = {"type": "enabled", "budget_tokens": 1024}
            temperature = 1.0
        else:
            if thinking and model_type == "claude-haiku-4-5":
                logger.warning("Thinking mode is not supported for Claude Haiku model.")
            thinking_params = {"type": "disabled", "budget_tokens": 0}
            temperature = 0.0

        # Configure boto3 client with timeouts and retry logic from environment variables
        # to handle long-running Claude requests
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

        # Get model-specific Bedrock model ID
        bedrock_model_id = MODEL_CONFIG[model_type]["bedrock_model_id"]

        logger.info(
            f"Created Bedrock client with model={bedrock_model_id}, read_timeout={read_timeout}s, "
            f"connect_timeout={connect_timeout}s, max_retry_attempts={max_attempts} ({retry_mode} mode)"
        )

        return ChatBedrockConverse(
            client=bedrock_client,
            model=bedrock_model_id,
            temperature=temperature,
            region_name=config.get_bedrock_region(),
            additional_model_request_fields={"thinking": thinking_params}
            if thinking_params["type"] == "enabled"
            else {},
            callbacks=callbacks,
        )
    else:
        # Default to gpt4o/gpt-4o-mini (Azure OpenAI)
        if thinking:
            logger.warning("Thinking mode is only supported for Claude Sonnet models.")

        # Get model-specific configuration
        model_config = MODEL_CONFIG[model_type]
        api_version = model_config["api_version"]
        deployment = model_config["deployment"]
        model_name = model_config["model_name"]

        logger.info(
            f"Creating Azure OpenAI model: deployment={deployment}, model={model_name}, api_version={api_version}"
        )

        return AzureChatOpenAI(
            azure_deployment=deployment,
            api_version=api_version,
            temperature=0.7,
            model=model_name,
            callbacks=callbacks,
        )
