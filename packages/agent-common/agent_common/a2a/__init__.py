"""A2A (Agent-to-Agent) protocol components.

Provides base classes, configuration, models, and authentication
for agent-to-agent communication.

For concrete agent implementations, see the agents/ module.

Usage:
    from agent_common.a2a import (
        BaseA2ARunnable,
        LocalA2ARunnable,
        SubAgentInput,
        A2AClientConfig,
        LocalSubAgentConfig,
        LocalLangGraphSubAgentConfig,
        LocalFoundrySubAgentConfig,
    )

    # Import factory and client directly to avoid circular dependencies:
    from agent_common.a2a.client_runnable import A2AClientRunnable
    from agent_common.a2a.factory import make_a2a_async_runnable
"""

# Authentication models
from .authentication import (
    AuthenticationMethod,
    AuthPayload,
    OAuth2ClientConfig,
    ServiceAuthRequirement,
    SmartTokenInterceptor,
)

# Base classes
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

# Structured response
from .structured_response import (
    A2A_PROTOCOL_ADDENDUM,
    StructuredResponseMixin,
    SubAgentResponseSchema,
    get_response_format,
)

__all__ = [
    # Base classes
    "BaseA2ARunnable",
    "LocalA2ARunnable",
    "SubAgentInput",
    # Config
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
    # Structured response
    "SubAgentResponseSchema",
    "A2A_PROTOCOL_ADDENDUM",
    "StructuredResponseMixin",
    "get_response_format",
]
