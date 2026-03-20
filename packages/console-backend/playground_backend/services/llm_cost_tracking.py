"""Cost tracking helper for LLM calls in the playground backend."""

import logging
import os

from ringier_a2a_sdk.cost_tracking import CostLogger, CostTrackingCallback

logger = logging.getLogger(__name__)

# Global cost logger instance
_cost_logger: CostLogger | None = None


def get_cost_logger() -> CostLogger:
    """Get or create the global cost logger instance.

    Returns:
        CostLogger instance configured for the playground backend.
    """
    global _cost_logger
    if _cost_logger is None:
        backend_url = os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")
        _cost_logger = CostLogger(
            backend_url=backend_url,
            batch_size=10,
            flush_interval=5.0,
        )
        logger.info(f"Initialized cost logger for backend URL: {backend_url}")
    return _cost_logger


def get_llm_cost_callback(user_id: str, sub_agent_id: int | None = None) -> CostTrackingCallback:
    """Create a cost tracking callback for LLM invocations.

    Args:
        user_id: User ID for cost attribution
        sub_agent_id: Optional sub-agent ID for cost attribution

    Returns:
        CostTrackingCallback instance that can be passed to LLM invocations.
    """
    cost_logger = get_cost_logger()
    callback = CostTrackingCallback(cost_logger=cost_logger, sub_agent_id=sub_agent_id)
    logger.info(f"Created cost tracking callback for user_id={user_id}, sub_agent_id={sub_agent_id}")
    return callback
