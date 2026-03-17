"""
Model factory utilities for creating LangChain models.

This module provides utility functions for creating and configuring LangChain models
without introducing circular dependencies. It's used by both the orchestrator and
agent-runner services to create models dynamically.

Provider dependencies are lazily imported so that services only need to install
the providers they actually use (see pyproject.toml optional dependency groups).
"""

import json
import logging
import os

from langchain_core.language_models import BaseChatModel

from agent_common.models.base import DEFAULT_MODEL, ModelType, ThinkingLevel

# Model-specific configuration
MODEL_CONFIG = {
    "gpt-4o": {
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
    "claude-sonnet-4.6": {
        "bedrock_model_id": "global.anthropic.claude-sonnet-4-6",
    },
    "claude-haiku-4-5": {
        "bedrock_model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    },
    "gemini-3-pro-preview": {
        "model_id": "gemini-3-pro-preview",
    },
    "gemini-3-flash-preview": {
        "model_id": "gemini-3-flash-preview",
    },
}

logger = logging.getLogger(__name__)


def _resolve_bedrock_region(bedrock_region: str | None) -> str:
    """Resolve Bedrock region from explicit value or environment variables."""
    return bedrock_region or os.getenv("AWS_BEDROCK_REGION", os.getenv("AWS_REGION", "eu-west-1"))


def get_available_models() -> list[ModelType]:
    """Get list of all available model types.

    Returns:
        List of all supported model types.
    """
    return list(MODEL_CONFIG.keys())  # type: ignore


def is_valid_model(model_name: str) -> bool:
    """Check if a model name is valid.

    Args:
        model_name: Model name to validate.

    Returns:
        True if the model name is valid, False otherwise.
    """
    return model_name in MODEL_CONFIG


def get_default_model() -> ModelType:
    """Get the default model type.

    Returns:
        The default model type (configurable via DEFAULT_MODEL env var).
    """
    return DEFAULT_MODEL


def get_thinking_budget(thinking_level: ThinkingLevel) -> int:
    """Map thinking level to Claude token budget.

    Based on Anthropic's official documentation and recommendations:
    - minimal: 1024 tokens (hard minimum, simple queries)
    - low: 4096 tokens (standard agent tasks, balanced default)
    - medium: 10000 tokens (complex reasoning, multi-step analysis)
    - high: 16000 tokens (very complex problems, deep analysis)

    Args:
        thinking_level: The thinking depth level.

    Returns:
        Token budget for Claude extended thinking.
    """
    budget_map = {
        "minimal": 1024,
        "low": 4096,
        "medium": 10000,
        "high": 16000,
    }
    return budget_map[thinking_level]


def create_model(
    model_type: ModelType,
    bedrock_region: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    callbacks: list | None = None,
) -> BaseChatModel:
    """Create a model instance for the given model type.

    Utility function that can be used by both the orchestrator and agent-runner
    to create models dynamically.

    Provider-specific dependencies (langchain-openai, langchain-google-genai,
    langchain-aws) are imported lazily so that services only need to install
    the providers they actually use.

    Args:
        model_type: The type of model to create
        bedrock_region: AWS region for Bedrock models. If None, reads from
                       AWS_BEDROCK_REGION or AWS_REGION env vars.
        thinking_level: Thinking depth level (minimal/low/medium/high) for Claude Sonnet and Gemini models.
                       If None, thinking is disabled.
        callbacks: Optional list of LangChain callbacks (e.g., for cost tracking)

    Returns:
        BaseChatModel: The created model instance
    """
    if model_type in ("gemini-3-pro-preview", "gemini-3-flash-preview"):
        # Lazy import for Gemini provider
        from google.oauth2 import service_account
        from langchain_google_genai import ChatGoogleGenerativeAI

        # Gemini 3 models via Vertex AI
        # Temperature MUST be 1.0 for Gemini 3.0+ to prevent infinite loops and degraded reasoning
        model_config = MODEL_CONFIG[model_type]
        model_id = model_config["model_id"]

        # Vertex AI authentication with service account
        gcp_key = os.getenv("GCP_KEY")
        if not gcp_key:
            raise ValueError("GCP_KEY environment variable is required for Gemini models")

        try:
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(gcp_key),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"Failed to parse GCP_KEY as valid service account JSON: {e}")

        gcp_project = os.getenv("GCP_PROJECT_ID")
        gcp_location = os.getenv("GCP_LOCATION", "europe-west4")

        if not gcp_project:
            raise ValueError("GCP_PROJECT_ID environment variable is required for Gemini models")

        # Configure thinking mode if enabled
        gemini_thinking_level = None
        include_thoughts = False
        if thinking_level:
            gemini_thinking_level = thinking_level  # Pass level directly (minimal, low, medium, high)
            include_thoughts = True
            logger.info(f"Gemini thinking mode enabled with level={gemini_thinking_level}")

        logger.info(
            f"Creating Gemini Vertex AI model: model={model_id}, project={gcp_project}, "
            f"location={gcp_location}, thinking_level={gemini_thinking_level}"
        )

        return ChatGoogleGenerativeAI(
            model=model_id,
            credentials=credentials,
            project=gcp_project,
            location=gcp_location,
            temperature=1.0,  # CRITICAL: Gemini 3.0+ requires 1.0 to prevent infinite loops
            thinking_level=gemini_thinking_level,
            include_thoughts=include_thoughts,
            callbacks=callbacks,
        )
    elif model_type in ("claude-sonnet-4.5", "claude-sonnet-4.6", "claude-haiku-4-5"):
        # Lazy import for AWS Bedrock provider
        import boto3
        from botocore.config import Config as BotoConfig
        from langchain_aws import ChatBedrockConverse

        region = _resolve_bedrock_region(bedrock_region)

        # Both Claude Sonnet and Haiku support Extended Thinking
        if thinking_level:
            budget_tokens = get_thinking_budget(thinking_level)
            thinking_params = {"type": "enabled", "budget_tokens": budget_tokens}
            temperature = 1.0
            logger.info(
                f"Claude {model_type} thinking enabled with level={thinking_level}, budget={budget_tokens} tokens"
            )
        else:
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
            retries={  # type: ignore
                "max_attempts": max_attempts,
                "mode": retry_mode,
            },
        )

        # Create bedrock-runtime client with custom configuration
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=region,
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
            region_name=region,
            additional_model_request_fields={"thinking": thinking_params}
            if thinking_params["type"] == "enabled"
            else {},
            callbacks=callbacks,
        )
    else:
        # Lazy import for Azure OpenAI provider
        from langchain_openai import AzureChatOpenAI

        # Default to gpt-4o/gpt-4o-mini (Azure OpenAI)
        if thinking_level:
            logger.warning("Thinking mode is only supported for Claude Sonnet and Gemini models.")

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
