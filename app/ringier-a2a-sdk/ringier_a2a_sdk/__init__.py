"""Ringier A2A SDK - Shared components for A2A agents and servers."""

from ringier_a2a_sdk.cost_tracking.logger import (
    CostLogger,
    get_request_access_token,
    get_request_credentials,
    get_request_user_sub,
    set_request_access_token,
    set_request_user_sub,
)

from .models import AgentStreamResponse, BaseAgentStreamResponse, UserConfig


# Lazy import for CostTrackingCallback (requires langchain_core)
def __getattr__(name):
    if name == "CostTrackingCallback":
        from .cost_tracking import CostTrackingCallback

        return CostTrackingCallback
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__version__ = "0.1.0"

__all__ = [
    "BaseAgentStreamResponse",
    "AgentStreamResponse",
    "UserConfig",
    "CostLogger",
    "CostTrackingCallback",
    "set_request_access_token",
    "set_request_user_sub",
    "get_request_access_token",
    "get_request_user_sub",
    "get_request_credentials",
]
