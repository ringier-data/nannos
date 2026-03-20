"""Cost tracking module for A2A agents."""

from .logger import CostLogger, get_request_access_token, set_request_access_token


# Lazy import for CostTrackingCallback (requires langchain_core)
def __getattr__(name):
    if name == "CostTrackingCallback":
        from .callback import CostTrackingCallback

        return CostTrackingCallback
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["CostTrackingCallback", "CostLogger", "set_request_access_token", "get_request_access_token"]
