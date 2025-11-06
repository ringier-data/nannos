"""
Subagents module for A2A (Agent-to-Agent) communication.

This module provides all the necessary components for interacting with A2A subagents,
including configuration, factory methods, models, middleware, and authentication.

Key Components:
- A2AClientRunnable: Core runnable for A2A communication
- A2AClientConfig: Configuration for A2A clients
- A2ATaskTrackingMiddleware: Middleware for tracking A2A task state
- Authentication: OAuth2 and token exchange capabilities
- Models: Response models for A2A protocol compliance

Usage:
    from app.subagents import (
        A2AClientRunnable,
        A2AClientConfig,
        make_a2a_async_runnable,
        A2ATaskTrackingMiddleware,
    )
"""

# Core A2A components
from .config import A2AClientConfig
from .factory import make_a2a_async_runnable
from .models import A2ATaskResponse, A2AMessageResponse
from .runnable import A2AClientRunnable, SubAgentInput
from .middleware import A2ATaskTrackingMiddleware, A2ATrackingState

# Authentication components (imported from parent module)
from ..authentication import (
    SmartTokenInterceptor,
    OktaTokenExchanger,
    ExchangedToken,
    SubAgentTokenProvider,
    TokenExchangeError,
    AgentSecurityConfig,
    requires_token_exchange,
    get_agent_client_id,
    get_required_scopes,
    AuthenticationMethod,
    ServiceAuthRequirement,
    OAuth2ClientConfig,
    AuthPayload,
)

__all__ = [
    # Core components
    "A2AClientConfig",
    "make_a2a_async_runnable", 
    "A2ATaskResponse",
    "A2AMessageResponse",
    "A2AClientRunnable",
    "SubAgentInput",
    "A2ATaskTrackingMiddleware",
    "A2ATrackingState",
    
    # Authentication
    "SmartTokenInterceptor",
    "OktaTokenExchanger", 
    "ExchangedToken",
    "SubAgentTokenProvider",
    "TokenExchangeError",
    "AgentSecurityConfig",
    "requires_token_exchange",
    "get_agent_client_id",
    "get_required_scopes",
    "AuthenticationMethod",
    "ServiceAuthRequirement",
    "OAuth2ClientConfig",
    "AuthPayload",
]
