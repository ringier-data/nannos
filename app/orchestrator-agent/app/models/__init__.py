"""
Models module for the Orchestrator Agent.

This module contains all data models, schemas, configuration classes, and exceptions
used throughout the application, providing a centralized location for data definitions.

Key Components:
- Configuration models (UserConfig, UserContext, AgentSettings, ResponseFormat)
- Response models (AgentStreamResponse and other API response models)
- Schema definitions (FinalResponseSchema for structured output)
- Custom exceptions (A2AClientError, AgentFrameworkAuthError)

Usage:
    from app.models import (
        UserConfig,
        UserContext,
        AgentSettings,
        AgentStreamResponse,
        FinalResponseSchema,
        A2AClientError,
    )
"""

from .config import AgentSettings, GraphRuntimeContext, MessageFormatting, ResponseFormat, UserConfig
from .exceptions import A2AClientError, AgentFrameworkAuthError
from .responses import AgentStreamResponse
from .runtime import build_runtime_context
from .schemas import FinalResponseSchema

__all__ = [
    # Configuration
    "UserConfig",
    "GraphRuntimeContext",
    "AgentSettings",
    "ResponseFormat",
    "MessageFormatting",
    "build_runtime_context",
    # Responses
    "AgentStreamResponse",
    # Schemas
    "FinalResponseSchema",
    # Exceptions
    "A2AClientError",
    "AgentFrameworkAuthError",
]
