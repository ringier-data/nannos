"""
Authentication module for A2A agent-to-agent communication.

This module provides automatic authentication detection and OAuth2 token exchange
for secure service-to-service communication following RFC 8693.

Key components:
- SmartTokenInterceptor: Auto-detects auth from AgentCard and performs token exchange
- OktaTokenExchanger: Implements RFC 8693 OAuth2 token exchange
- AgentSecurityConfig: Analyzes AgentCard security requirements
"""

from .interceptor import SmartTokenInterceptor
from .okta_token_exchange import (
    OktaTokenExchanger,
    ExchangedToken,
    SubAgentTokenProvider,
    TokenExchangeError,
)
from .agent_security_config import (
    AgentSecurityConfig,
    requires_token_exchange,
    get_agent_client_id,
    get_required_scopes,
)
from .in_task_auth import (
    AuthenticationMethod,
    ServiceAuthRequirement,
    OAuth2ClientConfig,
    AuthPayload,
)

__all__ = [
    # Interceptor
    "SmartTokenInterceptor",
    
    # Token Exchange
    "OktaTokenExchanger",
    "ExchangedToken",
    "SubAgentTokenProvider",
    "TokenExchangeError",
    
    # Security Config
    "AgentSecurityConfig",
    "requires_token_exchange",
    "get_agent_client_id",
    "get_required_scopes",
    
    # In-Task Authentication
    "AuthenticationMethod",
    "ServiceAuthRequirement",
    "OAuth2ClientConfig",
    "AuthPayload",
]
