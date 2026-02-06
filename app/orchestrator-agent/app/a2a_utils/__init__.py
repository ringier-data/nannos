"""
A2A (Agent-to-Agent) Protocol Module.

This module provides core A2A protocol components for agent communication:
- Base classes and abstractions
- Configuration models
- Response models following A2A protocol

For concrete agent implementations (dynamic agents, file analyzer, etc.),
see the agents/ module.

Usage:
    from app.a2a_utils import (
        BaseA2ARunnable,
        LocalA2ARunnable,
        A2AClientConfig,
        LocalSubAgentConfig,
    )

    # Import factory and client directly to avoid circular dependencies:
    from app.a2a_utils.factory import make_a2a_async_runnable
    from app.a2a_utils.client_runnable import A2AClientRunnable

    # For authentication components, import directly:
    from app.a2a_utils.authentication import SmartTokenInterceptor, OAuth2ClientConfig

Note: Factory and client runnable are no longer re-exported from this module
to avoid circular dependencies (SOLID: Dependency Inversion Principle).
Import them directly from their respective modules instead.
"""

# Base classes
# Authentication models
from .authentication import (
    AuthenticationMethod,
    AuthPayload,
    OAuth2ClientConfig,
    ServiceAuthRequirement,
    SmartTokenInterceptor,
)
from .base import BaseA2ARunnable, LocalA2ARunnable, SubAgentInput

# Configuration
from .config import A2AClientConfig

# Models
from .models import (
    A2AMessageResponse,
    A2ATaskResponse,
    LocalFoundrySubAgentConfig,
    LocalLangGraphSubAgentConfig,
    LocalSubAgentConfig,
)

__all__ = [
    # Base classes
    "BaseA2ARunnable",
    "LocalA2ARunnable",
    "SubAgentInput",
    # Configuration
    "A2AClientConfig",
    # Models
    "A2ATaskResponse",
    "A2AMessageResponse",
    "LocalSubAgentConfig",
    "LocalLangGraphSubAgentConfig",
    "LocalFoundrySubAgentConfig",
    # Authentication
    "SmartTokenInterceptor",
    "AuthenticationMethod",
    "ServiceAuthRequirement",
    "OAuth2ClientConfig",
    "AuthPayload",
]
