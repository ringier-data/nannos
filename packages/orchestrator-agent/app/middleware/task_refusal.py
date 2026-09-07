"""Telling a concurrency refusal apart from a real sub-agent result.

``DynamicToolDispatchMiddleware`` refuses a second concurrent ``task`` call to
one sub-agent (see ``surplus_same_agent_call``). The refusal travels as a
``task`` ToolMessage, which is exactly what a delegation result looks like — and
it lands *after* the owner's, because siblings are written in ``tool_calls``
order. Every consumer that reads "the latest ``task`` result" therefore reads
the refusal unless it checks this tag:

- ``StreamHandler.parse_agent_response`` adopts the most recent non-empty
  ``task`` result when ``include_subagent_output`` is set — untagged, the user's
  visible reply becomes "This call was NOT executed…" and the real answer is
  dropped;
- ``A2ATaskTrackingMiddleware`` reads the trailing ToolMessage for
  ``a2a_metadata`` — untagged, the owner's ``task_id``/``context_id`` are never
  recorded, and a parked ``input-required``/``auth-required`` owner can no longer
  be resumed.

This module holds nothing but the tag and its predicate so that all three
middlewares can share it without importing each other.
"""

from __future__ import annotations

from typing import Any

#: Key set in the refusal's ``additional_kwargs``.
CONCURRENT_TASK_REFUSAL_KEY = "concurrent_task_refusal"


def is_concurrent_task_refusal(message: Any) -> bool:
    """Whether ``message`` is a concurrency refusal rather than a sub-agent result."""
    return bool((getattr(message, "additional_kwargs", None) or {}).get(CONCURRENT_TASK_REFUSAL_KEY))
