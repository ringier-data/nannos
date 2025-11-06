"""
Models module for the Orchestrator Agent.

This module contains all data models, schemas, configuration classes, and exceptions
used throughout the application, providing a centralized location for data definitions.

Key Components:
- Configuration models (UserConfig, AgentSettings, ResponseFormat)
- Response models (AgentStreamResponse and other API response models)  
- Schema definitions (FinalResponseSchema for structured output)
- Custom exceptions (A2AClientError, AgentFrameworkAuthError)

Usage:
    from app.models import (
        UserConfig,
        AgentSettings, 
        AgentStreamResponse,
        FinalResponseSchema,
        A2AClientError,
    )
"""

from .config import UserConfig, AgentSettings, ResponseFormat
from .responses import AgentStreamResponse
from .schemas import FinalResponseSchema
from .exceptions import A2AClientError, AgentFrameworkAuthError

__all__ = [
    # Configuration
    "UserConfig",
    "AgentSettings", 
    "ResponseFormat",
    
    # Responses
    "AgentStreamResponse",
    
    # Schemas
    "FinalResponseSchema",
    
    # Exceptions
    "A2AClientError",
    "AgentFrameworkAuthError",
]
