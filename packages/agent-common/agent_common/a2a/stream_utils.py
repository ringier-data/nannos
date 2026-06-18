"""Shared streaming utilities for LangGraph-based A2A agents."""

from typing import Any, Dict


async def retrieve_final_state(graph: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieve and validate the final state values from a LangGraph graph.

    Wraps the common ``aget_state() → validate → .values`` pattern used by
    GPAgentRunnable and DynamicLocalAgentRunnable after streaming completes.

    Uses the async ``aget_state``: AsyncPostgresSaver forbids synchronous
    ``get_state()`` from the running event loop.

    Args:
        graph: A compiled LangGraph ``StateGraph`` instance.
        config: The config dict that was passed to ``astream()``.

    Returns:
        The final state values dict.

    Raises:
        ValueError: If the state is ``None`` or has no values.
    """
    final_state = await graph.aget_state(config)
    if final_state is None or not final_state.values:
        raise ValueError("Stream completed but could not retrieve final state")
    return final_state.values
