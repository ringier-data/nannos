"""
Authentication module for A2A agent-to-agent communication.

This module provides automatic authentication detection and OAuth2 token exchange
for secure service-to-service communication following RFC 8693.

Key components:
- SmartTokenInterceptor: Auto-detects auth from AgentCard and performs token exchange
- AgentSecurityConfig: Analyzes AgentCard security requirements
"""

from .in_task_auth import (
    AuthenticationMethod,
    AuthPayload,
    OAuth2ClientConfig,
    ServiceAuthRequirement,
)
from .interceptor import SmartTokenInterceptor

__all__ = [
    # Interceptor
    "SmartTokenInterceptor",
    # In-Task Authentication
    "AuthenticationMethod",
    "ServiceAuthRequirement",
    "OAuth2ClientConfig",
    "AuthPayload",
]
