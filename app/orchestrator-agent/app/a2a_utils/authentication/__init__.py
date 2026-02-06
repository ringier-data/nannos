"""Authentication components for A2A agent-to-agent communication.

This module provides authentication mechanisms for A2A protocol:
- SmartTokenInterceptor: Token exchange and authentication for A2A calls
- In-task authentication models: Models for downstream service auth requirements
"""

from .in_task_auth import (
    AuthenticationMethod,
    AuthPayload,
    OAuth2ClientConfig,
    ServiceAuthRequirement,
)
from .interceptor import SmartTokenInterceptor

__all__ = [
    "SmartTokenInterceptor",
    "AuthenticationMethod",
    "ServiceAuthRequirement",
    "OAuth2ClientConfig",
    "AuthPayload",
]
