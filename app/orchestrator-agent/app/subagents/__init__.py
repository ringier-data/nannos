"""
Subagents module for A2A (Agent-to-Agent) communication.

This module provides all the necessary components for interacting with A2A subagents,
including configuration, factory methods, models, middleware, and authentication.

Key Components:
- BaseA2ARunnable: Abstract base class for all A2A runnables
- A2AClientRunnable: Remote A2A agent client (extends BaseA2ARunnable)
- LocalA2ARunnable: Base class for in-process sub-agents
- DynamicLocalAgentRunnable: User-configurable local sub-agent with LangGraph
- A2AClientConfig: Configuration for A2A clients
- A2ATaskTrackingMiddleware: Middleware for tracking A2A task state
- Authentication: OAuth2 and token exchange capabilities
- Models: Response models and configuration for A2A protocol compliance

Usage:
    from app.subagents import (
        A2AClientRunnable,
        A2AClientConfig,
        make_a2a_async_runnable,
        A2ATaskTrackingMiddleware,
        LocalSubAgentConfig,
        create_dynamic_local_subagent,
    )
"""

# Core A2A components
# Authentication components (imported from parent module)
from ..authentication import (
    AuthenticationMethod,
    AuthPayload,
    OAuth2ClientConfig,
    ServiceAuthRequirement,
    SmartTokenInterceptor,
)
from .base import BaseA2ARunnable, LocalA2ARunnable, SubAgentInput
from .config import A2AClientConfig
from .dynamic_agent import (
    DynamicLocalAgentRunnable,
    SubAgentResponseSchema,
    create_dynamic_local_subagent,
)
from .factory import make_a2a_async_runnable
from .file_analyzer import (
    FileAnalyzerRunnable,
    create_file_analyzer_subagent,
)
from .middleware import A2ATaskTrackingMiddleware, A2ATrackingState
from .models import A2AMessageResponse, A2ATaskResponse, LocalSubAgentConfig
from .runnable import A2AClientRunnable

__all__ = [
    # Base classes
    "BaseA2ARunnable",
    "LocalA2ARunnable",
    "SubAgentInput",
    # Core components
    "A2AClientConfig",
    "make_a2a_async_runnable",
    "A2ATaskResponse",
    "A2AMessageResponse",
    "A2AClientRunnable",
    "A2ATaskTrackingMiddleware",
    "A2ATrackingState",
    # File analyzer sub-agent
    "FileAnalyzerRunnable",
    "create_file_analyzer_subagent",
    # Dynamic local sub-agents
    "LocalSubAgentConfig",
    "DynamicLocalAgentRunnable",
    "SubAgentResponseSchema",
    "create_dynamic_local_subagent",
    # Authentication
    "SmartTokenInterceptor",
    "AuthenticationMethod",
    "ServiceAuthRequirement",
    "OAuth2ClientConfig",
    "AuthPayload",
]
