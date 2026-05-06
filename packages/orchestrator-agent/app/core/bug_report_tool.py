"""Bug report tool for the orchestrator.

Uses LangGraph interrupt() to pause execution and collect user confirmation
before submitting a bug report to console-backend.

This is a **last resort** tool — the LLM should only call it after exhausting
recovery options (retries, alternative tools, plan changes) and the error
remains unresolvable and user-visible.
"""

import logging

from langchain_core.tools import tool
from langgraph.types import interrupt

logger = logging.getLogger(__name__)


@tool
def report_bug_tool(reason: str) -> str:
    """Report a bug when an unrecoverable error prevents fulfilling the user's request.

    Only use this as a last resort after exhausting recovery options (retries,
    alternative tools, plan changes). Never use for errors you recovered from.

    Args:
        reason: Why a bug report is warranted — shown to the user as context.
    """
    # Interrupt pauses graph execution. The client presents a bug report form.
    # On resume, the graph receives {"confirmed": True, "description": "..."} or {"confirmed": False}.
    response = interrupt(
        value={
            "type": "bug_report",
            "reason": reason,
            "message": f"I encountered an issue and would like to file a bug report.\n\nReason: {reason}\n\nWould you like to confirm this report?",
        }
    )

    if isinstance(response, dict) and response.get("confirmed"):
        description = response.get("description", reason)
        logger.info(f"Bug report confirmed by user: {description[:100]}")
        return f"Bug report submitted with description: {description}"
    else:
        logger.info("Bug report declined by user")
        return "User declined to submit a bug report."
