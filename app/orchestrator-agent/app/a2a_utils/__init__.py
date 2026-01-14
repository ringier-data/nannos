"""
A2A (Agent-to-Agent) Protocol Module.

This module provides core A2A protocol components for agent communication:
- Base classes and abstractions
- Configuration models
- Response models following A2A protocol
- Factory functions for creating A2A clients
- Remote A2A client implementation

For concrete agent implementations (dynamic agents, file analyzer, etc.),
see the agents/ module.

Usage:
    from app.a2a_utils import (
        BaseA2ARunnable,
        LocalA2ARunnable,
        A2AClientConfig,
        LocalSubAgentConfig,
        make_a2a_async_runnable,
        A2AClientRunnable,
    )

    # For authentication components, import directly:
    from app.authentication import SmartTokenInterceptor, OAuth2ClientConfig

Note: Authentication components are no longer re-exported from this module
to avoid circular dependencies (SOLID: Dependency Inversion Principle).
Import them directly from app.authentication instead.
"""

# Base classes
from .base import BaseA2ARunnable, LocalA2ARunnable, SubAgentInput

# A2A client for remote agents
from .client_runnable import A2AClientRunnable

# Configuration
from .config import A2AClientConfig

# Factory
from .factory import make_a2a_async_runnable

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
    # Client
    "A2AClientRunnable",
    # Configuration
    "A2AClientConfig",
    # Factory
    "make_a2a_async_runnable",
    # Models
    "A2ATaskResponse",
    "A2AMessageResponse",
    "LocalSubAgentConfig",
    "LocalLangGraphSubAgentConfig",
    "LocalFoundrySubAgentConfig",
]
