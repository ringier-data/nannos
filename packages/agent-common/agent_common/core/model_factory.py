"""
Model factory utilities for creating LangChain models.

This module provides utility functions for creating and configuring LangChain models
without introducing circular dependencies. It's used by both the orchestrator and
agent-runner services to create models dynamically.

Provider dependencies are lazily imported so that services only need to install
the providers they actually use (see pyproject.toml optional dependency groups).

MODEL_CONFIG is built dynamically at import time: only models whose cloud
credentials are detected in the environment (or whose provider is available
locally) are registered.  This allows the orchestrator to start without
*any* cloud env-vars when a local OpenAI-compatible server is configured.
"""

import json
import logging
import os

from langchain_core.language_models import BaseChatModel

from agent_common.models.base import DEFAULT_MODEL, ModelType, ThinkingLevel, get_resolved_default_model

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model metadata (labels, providers, capabilities) for API consumers
# ---------------------------------------------------------------------------

_MODEL_METADATA: dict[str, dict] = {
    "gpt-4o": {"label": "GPT-4o", "provider": "Azure OpenAI", "supports_thinking": False},
    "gpt-4o-mini": {"label": "GPT-4o Mini", "provider": "Azure OpenAI", "supports_thinking": False},
    "gpt-5.4-mini": {"label": "GPT-5.4 Mini", "provider": "Azure OpenAI", "supports_thinking": False},
    "gpt-5.4-nano": {"label": "GPT-5.4 Nano", "provider": "Azure OpenAI", "supports_thinking": False},
    "claude-sonnet-4.5": {"label": "Claude Sonnet 4.5", "provider": "AWS Bedrock", "supports_thinking": True},
    "claude-sonnet-4.6": {"label": "Claude Sonnet 4.6", "provider": "AWS Bedrock", "supports_thinking": True},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5", "provider": "AWS Bedrock", "supports_thinking": True},
    "gemini-3.1-pro-preview": {
        "label": "Gemini 3.1 Pro Preview",
        "provider": "Google Vertex AI",
        "supports_thinking": True,
        "thinking_levels": ["low", "medium", "high"],
    },
    "gemini-3-flash-preview": {
        "label": "Gemini 3 Flash Preview",
        "provider": "Google Vertex AI",
        "supports_thinking": True,
    },
    "local": {"label": "Local Model", "provider": "OpenAI Compatible", "supports_thinking": False},
}

# ---------------------------------------------------------------------------
# Provider-credential detection helpers
# ---------------------------------------------------------------------------

# All possible model entries – keyed by provider so each group can be toggled.
_AZURE_MODELS: dict[str, dict] = {
    "gpt-4o": {
        "api_version": "2024-08-01-preview",
        "deployment": "chatgpt-4o",
        "model_name": "gpt-4o",
        "input_modes": ["text", "image"],
        "backend": "azure_openai",
    },
    "gpt-4o-mini": {
        "api_version": "2025-01-01-preview",
        "deployment": "gpt-4o-mini",
        "model_name": "gpt-4o-mini",
        "input_modes": ["text", "image"],
        "backend": "azure_openai",
    },
    "gpt-5.4-mini": {
        "api_version": "2025-01-01-preview",
        "deployment": "gpt-5.4-mini",
        "model_name": "gpt-5.4-mini",
        "input_modes": ["text", "image"],
        "backend": "azure_openai",
    },
    "gpt-5.4-nano": {
        "api_version": "2025-01-01-preview",
        "deployment": "gpt-5.4-nano",
        "model_name": "gpt-5.4-nano",
        "input_modes": ["text", "image"],
        "backend": "azure_openai",
    },
}

_BEDROCK_MODELS: dict[str, dict] = {
    "claude-sonnet-4.5": {
        "bedrock_model_id": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "input_modes": ["text", "image", "file"],
        "backend": "bedrock",
    },
    "claude-sonnet-4.6": {
        "bedrock_model_id": "global.anthropic.claude-sonnet-4-6",
        "input_modes": ["text", "image", "file"],
        "backend": "bedrock",
    },
    "claude-haiku-4-5": {
        "bedrock_model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "input_modes": ["text", "image", "file"],
        "backend": "bedrock",
    },
}

_GEMINI_MODELS: dict[str, dict] = {
    "gemini-3.1-pro-preview": {
        "model_id": "gemini-3.1-pro-preview",
        "input_modes": ["text", "image", "audio", "video", "file"],
        "backend": "google",
    },
    "gemini-3-flash-preview": {
        "model_id": "gemini-3-flash-preview",
        "input_modes": ["text", "image", "audio", "video", "file"],
        "backend": "google",
    },
}


def _has_azure_credentials() -> bool:
    """Check whether Azure OpenAI credentials are configured."""
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY"))


def _has_aws_credentials() -> bool:
    """Check whether AWS credentials are available (env vars, profile, or instance role)."""
    try:
        import botocore.session

        session = botocore.session.get_session()
        credentials = session.get_credentials()
        return credentials is not None
    except Exception:
        return False


def _has_gcp_credentials() -> bool:
    """Check whether GCP credentials are configured for Gemini."""
    return bool(os.getenv("GCP_KEY") and os.getenv("GCP_PROJECT_ID"))


def _has_local_provider() -> bool:
    """Check whether a local OpenAI-compatible endpoint is configured."""
    return bool(os.getenv("OPENAI_COMPATIBLE_BASE_URL"))


def _fetch_local_models(v1_base_url: str) -> list[str]:
    """Query GET /v1/models on a local LLM server and return model IDs.

    Returns an empty list if the server is unreachable or returns no models.
    Called once at import time with a short timeout.
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(f"{v1_base_url}/models")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return [m["id"] for m in data.get("data", []) if "id" in m]
    except Exception as e:
        logger.debug("Could not fetch local models from %s/models: %s", v1_base_url, e)
        return []


def _build_model_config() -> dict[str, dict]:
    """Build MODEL_CONFIG dynamically based on available credentials.

    Only models whose provider credentials are detected are registered.
    This is called once at module import time.
    """
    config: dict[str, dict] = {}

    if _has_azure_credentials():
        config.update(_AZURE_MODELS)
        logger.info("Azure OpenAI credentials detected – registered models: %s", list(_AZURE_MODELS))
    else:
        logger.info("Azure OpenAI credentials not found – GPT models unavailable")

    if _has_aws_credentials():
        config.update(_BEDROCK_MODELS)
        logger.info("AWS credentials detected – registered models: %s", list(_BEDROCK_MODELS))
    else:
        logger.info("AWS credentials not found – Claude models unavailable")

    if _has_gcp_credentials():
        config.update(_GEMINI_MODELS)
        logger.info("GCP credentials detected – registered models: %s", list(_GEMINI_MODELS))
    else:
        logger.info("GCP credentials not found – Gemini models unavailable")

    if _has_local_provider():
        base_url_raw = os.getenv("OPENAI_COMPATIBLE_BASE_URL", "").rstrip("/")
        # Normalize to /v1 for the discovery call
        v1_base = base_url_raw + "/v1" if not base_url_raw.endswith("/v1") else base_url_raw
        api_key = os.getenv("OPENAI_COMPATIBLE_API_KEY", "not-needed")

        local_model_ids = _fetch_local_models(v1_base)
        if local_model_ids:
            for model_id in local_model_ids:
                config[model_id] = {
                    "base_url": base_url_raw,
                    "model_name": model_id,
                    "api_key": api_key,
                    "is_local": True,
                }
            logger.info(
                "Local OpenAI-compatible provider – registered %d model(s): %s",
                len(local_model_ids),
                local_model_ids,
            )
        else:
            # Fallback: register a single generic 'local' entry
            model_name = os.getenv("OPENAI_COMPATIBLE_MODEL", "default")
            config["local"] = {
                "base_url": base_url_raw,
                "model_name": model_name,
                "api_key": api_key,
                "is_local": True,
            }
            logger.info(
                "Local OpenAI-compatible provider – base_url=%s, model=%s (could not reach /v1/models)",
                base_url_raw,
                model_name,
            )
    else:
        logger.info("OPENAI_COMPATIBLE_BASE_URL not set – local model unavailable")

    if not config:
        logger.warning(
            "No model provider credentials found. Set cloud credentials or "
            "OPENAI_COMPATIBLE_BASE_URL to enable at least one model."
        )

    return config


# Model-specific configuration – built dynamically from environment
MODEL_CONFIG: dict[str, dict] = _build_model_config()


def _resolve_bedrock_region(bedrock_region: str | None) -> str:
    """Resolve Bedrock region from explicit value or environment variables."""
    return bedrock_region or os.getenv("AWS_BEDROCK_REGION", os.getenv("AWS_REGION", "eu-west-1"))


def get_available_models() -> list[ModelType]:
    """Get list of all available model types.

    Returns:
        List of all supported model types.
    """
    return list(MODEL_CONFIG.keys())  # type: ignore


def get_available_models_metadata() -> list[dict]:
    """Get available models with their metadata for API responses.

    Returns only models that have valid credentials configured.
    Each entry includes: value, label, provider, supports_thinking,
    and optionally thinking_levels and is_default.
    """
    default_model = get_default_model()
    result = []
    all_thinking_levels = [level.value for level in ThinkingLevel]
    for model_key in MODEL_CONFIG:
        model_cfg = MODEL_CONFIG[model_key]
        if model_cfg.get("is_local"):
            # Dynamically registered local model — no static metadata entry
            model_name = model_cfg.get("model_name", model_key)
            entry = {
                "value": model_key,
                "label": model_name,
                "provider": "OpenAI Compatible",
                "supports_thinking": False,
                "is_default": model_key == default_model,
            }
        else:
            meta = _MODEL_METADATA.get(model_key, {})
            entry = {
                "value": model_key,
                "label": meta.get("label", model_key),
                "provider": meta.get("provider", "Unknown"),
                "supports_thinking": meta.get("supports_thinking", False),
                "is_default": model_key == default_model,
            }
            if entry["supports_thinking"]:
                entry["thinking_levels"] = meta.get("thinking_levels", all_thinking_levels)
        result.append(entry)
    return result


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

    Returns the configured DEFAULT_MODEL if it is available (has credentials),
    otherwise falls back to the first available model.

    Returns:
        The default model type.
    """
    return get_resolved_default_model()


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


def get_model_input_capabilities(model_type: ModelType) -> list[str]:
    """Get supported input modes (content types) for a model.

    Args:
        model_type: The type of model to query

    Returns:
        List of supported content types (e.g., ["text", "image"])

    Raises:
        ValueError: If model type is not recognized
    """
    if model_type not in MODEL_CONFIG:
        raise ValueError(f"Unknown model type: {model_type}")
    return MODEL_CONFIG[model_type]["input_modes"]


def get_model_backend(model_type: ModelType) -> str:
    """Get the provider backend for a model type.

    Args:
        model_type: The type of model to query

    Returns:
        Provider backend string: "bedrock", "openai", or "google"

    Raises:
        ValueError: If model type is not recognized
    """
    if model_type not in MODEL_CONFIG:
        raise ValueError(f"Unknown model type: {model_type}")
    return MODEL_CONFIG[model_type]["backend"]


def create_model(
    model_type: ModelType,
    bedrock_region: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    callbacks: list | None = None,
    streaming: bool = True,
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
    if model_type in ("gemini-3.1-pro-preview", "gemini-3-flash-preview"):
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
            streaming=streaming,  # Enable token-level streaming
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
            # NOTE: Bedrock streams automatically when using .astream() - no 'streaming' parameter needed
        )
    elif model_type == "local" or MODEL_CONFIG.get(model_type, {}).get("is_local"):
        # Local OpenAI-compatible provider (Ollama, LM Studio, vLLM, etc.)
        # Handles both the generic "local" fallback key and individually registered model IDs.
        from langchain_openai import ChatOpenAI

        if thinking_level:
            logger.warning("Thinking mode is not supported for local OpenAI-compatible models.")

        model_config = MODEL_CONFIG[model_type]
        base_url = model_config["base_url"].rstrip("/")
        # langchain_openai appends /chat/completions directly to base_url, so it
        # must already include the /v1 prefix (e.g. LM Studio, Ollama, vLLM all
        # serve on /v1/chat/completions).  Auto-append /v1 when absent.
        if not base_url.endswith("/v1"):
            base_url = base_url + "/v1"
        model_name = model_config["model_name"]
        api_key = model_config["api_key"]

        logger.info(f"Creating local OpenAI-compatible model: base_url={base_url}, model={model_name}")

        return ChatOpenAI(
            base_url=base_url,
            model=model_name,
            api_key=api_key,
            temperature=0.7,
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
            streaming=streaming,  # Enable token-level streaming
        )
