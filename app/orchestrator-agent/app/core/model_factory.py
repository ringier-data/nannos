"""
Model factory utilities for creating LangChain models.

This module provides utility functions for creating and configuring LangChain models
without introducing circular dependencies. It's used by both GraphFactory and
other components that need to create models dynamically.
"""

import logging
from typing import Any, Literal

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

        return ChatBedrockConverse(
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
