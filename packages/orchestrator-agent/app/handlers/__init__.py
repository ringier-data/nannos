"""
Handlers module for the Orchestrator Agent.

This module contains request/response processing utilities, context builders,
and various handler functions that support the core agent functionality.

Key Components:
- StreamHandler: Handles stream response generation and state parsing
- current_turn_messages: The shared definition of "the current turn"
- current_turn_final_response: The shared definition of "this turn's answer"
- OrchestratorRequestContextBuilder: Custom request context builder for A2A
- Utility functions: Helper functions for error handling and parsing

Usage:
    from app.handlers import (
        StreamHandler,
        OrchestratorRequestContextBuilder,
        should_retry,
        handle_tool_failure,
        parse_tool_exception,
    )
"""

from .stream_handler import StreamHandler, current_turn_final_response, current_turn_messages
from .utils import handle_tool_failure, parse_tool_exception, should_retry

# Backwards compatibility alias
handle_auth_error = handle_tool_failure

__all__ = [
    "StreamHandler",
    "current_turn_messages",
    "current_turn_final_response",
    "should_retry",
    "handle_tool_failure",
    "handle_auth_error",
    "parse_tool_exception",
]
