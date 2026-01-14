"""Authentication Error Detection Middleware for deterministic auth error handling.

This middleware detects authentication errors from ANY tool (not just sub-agents) and
uses LangGraph's interrupt mechanism to pause execution in a resumable state.

Key Features:
- Detects structured JSON auth errors (errorCode: "need-credentials")
- Detects text-based auth error patterns
- Works with ALL tools (regular tools and sub-agent tasks)
- Uses interrupt() to pause graph execution when auth is required
- Supports resumable execution after authentication completion

Architecture:
- Wrap-style hooks: Intercept ALL tool calls to detect auth errors in responses
- Uses interrupt() to pause execution and surface auth requirements to client
- Graph can be resumed after authentication using Command.resume()

Integration:
    ```python
    agent = create_deep_agent(
        model=model,
        tools=tools,
        subagents=subagents,
        middleware=[
            AuthErrorDetectionMiddleware(),  # Auth detection (inner)
            ToolRetryMiddleware(),           # Retry logic (outer)
        ],
        checkpointer=MemorySaver()  # Required for interrupt/resume functionality
    )
    ```
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Dict

from a2a.types import TaskState
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT
from typing_extensions import NotRequired

logger = logging.getLogger(__name__)


class AuthErrorState(AgentState):
    """Extended agent state with authentication error tracking.

    Tracks authentication requirements for any tool that has encountered
    an auth error, enabling the orchestrator to properly handle auth flows.
    """

    auth_errors: NotRequired[Dict[str, Dict[str, Any]]]
    """Tracking data for authentication errors. Format: 
    {
        "tool_name": {
            "requires_auth": bool,
            "auth_url": str,
            "auth_message": str,
            "error_code": str,
            "timestamp": float
        }
    }
    """


class AuthErrorDetectionMiddleware(AgentMiddleware[AuthErrorState, ContextT]):
    """Middleware for deterministic authentication error detection using LangGraph interrupts.

    ARCHITECTURE:
    This middleware uses the LangGraph interrupt() mechanism to pause graph execution
    when authentication is required, allowing for resumable workflows.

    How it works:
    1. awrap_tool_call: Intercept ALL tool executions
    2. Tool executes and returns ToolMessage
    3. _detect_auth_error: Check response content for auth error patterns
    4. If auth error found: Call interrupt() with auth requirement data
    5. Graph execution pauses and surfaces the interrupt value to the client
    6. Client handles authentication and resumes with Command.resume()

    Resumable Execution:
    - The graph maintains its state when interrupted
    - After authentication, client can resume using: graph.stream(Command.resume(auth_token), config)
    - The graph resumes from where it was interrupted
    - Requires checkpointer to be enabled for state persistence

    Supported Auth Error Formats:
    - JSON: {"errorCode": "need-credentials", "authorizeUrl": "...", "message": "..."}
    - Text patterns: "authentication required", "401 unauthorized", etc.

    Interrupt Value Format:
    The interrupt value contains:
    {
        "task_state": TaskState.auth_required,
        "tool": "tool_name",
        "message": "Authentication required message",
        "auth_url": "https://oauth.example.com/authorize",
        "error_code": "need-credentials",
        "timestamp": 1234567890.123
    }

    This value is interpreted by the agent_executor to set TaskState.auth_required.
    """

    state_schema = AuthErrorState

    def __init__(self):
        """Initialize the authentication error detection middleware."""
        super().__init__()

    # def before_model(
    #     self,
    #     state: AuthErrorState,
    #     runtime: Runtime[ContextT]
    # ) -> Dict[str, Any] | None:
    #     """Extract authentication error metadata from tool responses.

    #     This hook runs at the START of each iteration, AFTER tool results have been
    #     added to messages. We examine ToolMessage results from the previous iteration
    #     to extract and persist authentication error metadata in state.

    #     Returns a dict with "auth_errors" key to be merged into state by LangGraph.
    #     """
    #     logger.info(f"[AUTH MIDDLEWARE before_model] Called with state keys: {list(state.keys())}")
    #     messages = state.get("messages", [])
    #     if not messages:
    #         logger.debug("[AUTH MIDDLEWARE before_model] No messages found")
    #         return None

    #     # Look for ToolMessage in the most recent message
    #     last_message = messages[-1]
    #     logger.info(f"[AUTH MIDDLEWARE before_model] Last message type: {type(last_message).__name__}")

    #     # # Check if the last message is a HumanMessage indicating authorization completion
    #     # from langchain_core.messages import HumanMessage
    #     # if isinstance(last_message, HumanMessage):
    #     #     content = last_message.content.lower() if isinstance(last_message.content, str) else ""
    #     #     auth_completion_patterns = [
    #     #         "authorized", "authentication complete", "logged in",
    #     #         "auth complete", "authorization complete", "signed in",
    #     #         "i've authorized", "authorization done", "auth done"
    #     #     ]

    #     #     if any(pattern in content for pattern in auth_completion_patterns):
    #     #         # User indicates they've completed authorization - clear all auth errors
    #     #         current_auth_errors = state.get("auth_errors", {})
    #     #         if current_auth_errors:
    #     #             logger.info("[AUTH MIDDLEWARE before_model] User indicated auth completion - clearing all auth errors")
    #     #             return {"auth_errors": {}}
    #     #     return None

    #     if not isinstance(last_message, ToolMessage):
    #         logger.debug("[AUTH MIDDLEWARE before_model] Last message is not ToolMessage")
    #         return None

    #     logger.debug("[AUTH MIDDLEWARE before_model] *** FOUND TOOLMESSAGE - CHECKING FOR AUTH ***")
    #     logger.debug(f"[AUTH MIDDLEWARE before_model] *** ToolMessage content: {last_message.content} ***")
    #     logger.debug(f"[AUTH MIDDLEWARE before_model] *** ToolMessage additional_kwargs: {getattr(last_message, 'additional_kwargs', {})} ***")

    #     # Check if ToolMessage has auth error metadata in additional_kwargs
    #     # This is placed here by _process_tool_message() after detecting auth errors
    #     additional_kwargs = getattr(last_message, 'additional_kwargs', {})
    #     auth_metadata = additional_kwargs.get('auth_error_metadata')
    #     auth_success = additional_kwargs.get('auth_success')

    #     # Find the corresponding tool call to determine which tool this message is for
    #     tool_name = None
    #     for msg in reversed(messages[:-1]):
    #         if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
    #             for tool_call in msg.tool_calls:
    #                 if tool_call.get('id') == last_message.tool_call_id:
    #                     tool_name = tool_call.get('name')
    #                     break
    #             if tool_name:
    #                 break

    #     if not tool_name:
    #         logger.debug("[AUTH MIDDLEWARE before_model] Could not determine tool name")
    #         return None

    #     # Get current auth errors state
    #     current_auth_errors = dict(state.get("auth_errors", {}))

    #     # Check if this is a successful tool execution after auth (clear auth errors)
    #     if auth_success and tool_name in current_auth_errors:
    #         logger.info(f"[AUTH MIDDLEWARE before_model] Clearing auth error for successful tool: {tool_name}")
    #         # TODO: the tools are grouped in MCP servers, so we may want to clear all tools for the same server

    #         del current_auth_errors[tool_name]
    #         return {"auth_errors": current_auth_errors}
    #     # if auth_success:
    #     #     return {"auth_errors": {}}

    #     # Check if this is an auth error
    #     if not auth_metadata:
    #         # If no metadata in additional_kwargs, try to detect auth error from content directly
    #         # This handles cases where ToolRetryMiddleware created the ToolMessage from an exception
    #         content = last_message.content if isinstance(last_message.content, str) else ""
    #         logger.error(f"[AUTH MIDDLEWARE before_model] *** Attempting direct detection on content: {content[:100]}... ***")
    #         auth_metadata = self._detect_auth_error(content)
    #         if auth_metadata:
    #             logger.error(f"[AUTH MIDDLEWARE before_model] *** SUCCESS! Detected auth error: {auth_metadata} ***")
    #         else:
    #             logger.error("[AUTH MIDDLEWARE before_model] *** FAILED - No auth error detected in ToolMessage content ***")
    #             return None

    #     logger.info(f"[AUTH MIDDLEWARE before_model] Authentication required for tool: {tool_name}")

    #     # Build state update for auth requirement
    #     current_auth_errors[tool_name] = {
    #         "requires_auth": True,
    #         "auth_url": auth_metadata.get("auth_url", ""),
    #         "auth_message": auth_metadata.get("auth_message", "Authentication required"),
    #         "error_code": auth_metadata.get("error_code", "auth-required"),
    #         "timestamp": auth_metadata.get("timestamp", 0.0)
    #     }

    #     logger.info(f"[AUTH MIDDLEWARE before_model] Stored auth requirement for {tool_name}")
    #     return {"auth_errors": current_auth_errors}

    # async def abefore_model(
    #     self,
    #     state: AuthErrorState,
    #     runtime: Runtime[ContextT]
    # ) -> Dict[str, Any] | None:
    #     """Async version of before_model.

    #     Reuses the sync implementation since auth error extraction is purely computational.
    #     """
    #     return self.before_model(state, runtime)

    # def wrap_tool_call(
    #     self,
    #     request: ToolCallRequest,
    #     handler: Callable[[ToolCallRequest], ToolMessage | Command],
    # ) -> ToolMessage | Command:
    #     """Detect authentication errors in tool responses (sync version).

    #     This wrap-style hook intercepts ALL tool calls to check for auth errors:
    #     1. Execute the tool via handler
    #     2. Check response for authentication error patterns
    #     3. If auth error detected: Mark ToolMessage with auth metadata
    #     4. Return processed ToolMessage for before_model to extract
    #     """
    #     tool_name = request.tool_call.get("name", "")
    #     logger.info(f"[AUTH MIDDLEWARE wrap_tool_call] Intercepting {tool_name} tool for auth error detection")

    #     # Execute the tool
    #     result = handler(request)

    #     # Check for auth errors in the response
    #     if isinstance(result, ToolMessage):
    #         return self._process_tool_message(result)
    #     elif isinstance(result, Command):
    #         return self._process_command(result)

    #     return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Detect authentication errors and use interrupt() to pause execution.

        This async wrap-style hook intercepts ALL tool calls to check for auth errors:
        1. Execute the tool via handler, catching auth exceptions
        2. If ToolException with auth error: Call interrupt() immediately
        3. If ToolMessage returned: Check for auth error patterns in content AND A2A metadata
        4. If auth error detected: Use interrupt() to pause graph execution

        For A2A sub-agents (task tool), also checks a2a_metadata for requires_auth flag
        and state=auth_required, providing a structured way to detect auth requirements.

        NOTE: We catch exceptions here to call interrupt() before ToolRetryMiddleware
        converts them to ToolMessages, ensuring immediate interruption.
        """
        from langchain_core.tools import ToolException
        from langgraph.types import interrupt

        tool_name = request.tool_call.get("name", "")
        logger.info(
            f"[AUTH MIDDLEWARE awrap_tool_call] Intercepting async {tool_name} tool for auth error detection (BEFORE retry middleware)"
        )

        # Extract subagent_type from tool call args (if this is a task tool)
        subagent_type = None
        if tool_name == "task":
            args = request.tool_call.get("args", {})
            subagent_type = args.get("subagent_type")

        try:
            # Execute the tool - catch auth exceptions before retry middleware
            result = await handler(request)
            logger.debug(f"[AUTH MIDDLEWARE awrap_tool_call] Tool executed successfully: {tool_name}")

            # Check for auth errors in successful ToolMessage responses
            if isinstance(result, ToolMessage):
                # First check A2A metadata (for task tool)
                additional_kwargs = getattr(result, "additional_kwargs", {})
                a2a_metadata = additional_kwargs.get("a2a_metadata")
                auth_metadata = self._check_a2a_auth_metadata(a2a_metadata, subagent_type or tool_name)

                # If no A2A auth requirement, fall back to content-based detection
                if not auth_metadata:
                    auth_metadata = self._detect_auth_error(result.content if isinstance(result.content, str) else "")

                if auth_metadata:
                    # Use interrupt() to pause graph execution with auth requirement
                    auth_requirement = {
                        "task_state": TaskState.auth_required,
                        "tool": tool_name,
                        "subagent": auth_metadata.get("subagent"),  # May be None for non-A2A tools
                        "message": auth_metadata.get("auth_message", "Authentication required"),
                        "auth_url": auth_metadata.get("auth_url", ""),
                        "error_code": auth_metadata.get("error_code", "auth-required"),
                        "timestamp": time.time(),
                    }

                    logger.info(f"[AUTH MIDDLEWARE] Interrupting graph for auth requirement: {tool_name}")

                    # This will pause graph execution and surface the auth requirement to the client
                    # NOTE: in this case the tool is not idempotent, and we will never hit this line again
                    #       upon resumption, since the graph will resume from the start of the node, and
                    #       in case the authorization is successful, the tool will succeed without hitting
                    #       this again. In case the authorization is not successful, the tool may hit this
                    #       again, but that's expected behavior, and the code just just continue after the interrupt,
                    #       and shall be handled by the model node.
                    #       TODO: could we though hit the edge case where another interrupt will collect the Command
                    #             which was meant to be catched here?
                    interrupt(auth_requirement)

                    # This line should not be reached due to the interrupt, but return for safety
                    return result

                return result
            elif isinstance(result, Command):
                # Check if Command contains ToolMessage with auth error
                if (
                    hasattr(result, "update")
                    and result.update
                    and "messages" in result.update
                    and result.update["messages"]
                ):
                    last_msg = result.update["messages"][-1]
                    if isinstance(last_msg, ToolMessage):
                        # First check A2A metadata (for task tool)
                        additional_kwargs = getattr(last_msg, "additional_kwargs", {})
                        a2a_metadata = additional_kwargs.get("a2a_metadata")
                        auth_metadata = self._check_a2a_auth_metadata(a2a_metadata, subagent_type or tool_name)

                        # If no A2A auth requirement, fall back to content-based detection
                        if not auth_metadata:
                            auth_metadata = self._detect_auth_error(
                                last_msg.content if isinstance(last_msg.content, str) else ""
                            )

                        if auth_metadata:
                            # Use interrupt() to pause graph execution with auth requirement
                            auth_requirement = {
                                "task_state": TaskState.auth_required,
                                "tool": tool_name,
                                "subagent": auth_metadata.get("subagent"),  # May be None for non-A2A tools
                                "message": auth_metadata.get("auth_message", "Authentication required"),
                                "auth_url": auth_metadata.get("auth_url", ""),
                                "error_code": auth_metadata.get("error_code", "auth-required"),
                                "timestamp": time.time(),
                            }

                            logger.info(f"[AUTH MIDDLEWARE] Interrupting graph for auth requirement: {tool_name}")
                            interrupt(auth_requirement)

                return result

            return result

        except ToolException as e:
            # Check if this is an auth-related ToolException
            exception_str = str(e)
            logger.info(f"[AUTH MIDDLEWARE] Caught ToolException: {exception_str}")

            auth_metadata = self._detect_auth_error(exception_str)
            if auth_metadata:
                # Use interrupt() to pause graph execution with auth requirement
                auth_requirement = {
                    "task_state": TaskState.auth_required,
                    "tool": tool_name,
                    "message": auth_metadata.get("auth_message", "Authentication required"),
                    "auth_url": auth_metadata.get("auth_url", ""),
                    "error_code": auth_metadata.get("error_code", "auth-required"),
                    "timestamp": time.time(),
                }

                logger.info(f"[AUTH MIDDLEWARE] Interrupting graph for ToolException auth requirement: {tool_name}")

                # This will pause graph execution and surface the auth requirement to the client
                interrupt(auth_requirement)

                # This line should not be reached due to the interrupt, but re-raise for safety
                raise

            # Not an auth error, let the exception propagate normally
            raise

    def _check_a2a_auth_metadata(
        self, a2a_metadata: Dict[str, Any] | None, subagent_name: str
    ) -> Dict[str, Any] | None:
        """Check A2A metadata for authentication requirements.

        This method specifically checks A2A protocol metadata (from task tool calls)
        for requires_auth flag and auth_required state. This provides a structured
        way to detect auth requirements from A2A sub-agents.

        Args:
            a2a_metadata: The A2A metadata dict from additional_kwargs, or None
            subagent_name: Name of the subagent (extracted from tool call args) or tool name as fallback

        Returns:
            Auth error metadata dict if auth required, None otherwise.
        """
        if not a2a_metadata:
            return None

        requires_auth = a2a_metadata.get("requires_auth", False)
        state_str = a2a_metadata.get("state", "").lower()

        # Check if A2A metadata indicates auth requirement
        if requires_auth or "auth_required" in state_str:
            # Get artifacts for checking auth URLs and extracting subagent name if needed
            artifacts = a2a_metadata.get("artifacts", [])

            # Use the subagent_name passed in (from tool call args)
            # Only fall back to extracting from artifacts if not provided
            if not subagent_name or subagent_name == "task":
                if artifacts and isinstance(artifacts, list) and len(artifacts) > 0:
                    # Try to extract subagent name from artifacts if available
                    first_artifact = artifacts[0]
                    if isinstance(first_artifact, dict) and "subagent" in first_artifact:
                        subagent_name = first_artifact["subagent"]

            # Check artifacts for auth URLs
            auth_url = ""
            auth_message = f"Authentication required for {subagent_name}"
            if artifacts:
                for artifact in artifacts:
                    if isinstance(artifact, dict):
                        if "auth_url" in artifact:
                            auth_url = artifact["auth_url"]
                        if "message" in artifact:
                            auth_message = artifact["message"]

            logger.info(f"[AUTH MIDDLEWARE] Detected A2A auth requirement from metadata: {subagent_name}")
            logger.info(f"[AUTH MIDDLEWARE] requires_auth={requires_auth}, state={state_str}")

            return {
                "auth_url": auth_url,
                "auth_message": auth_message,
                "error_code": "a2a-auth-required",
                "subagent": subagent_name,
            }

        return None

    def _detect_auth_error(self, content: str) -> Dict[str, Any] | None:
        """Detect authentication errors deterministically from tool message content.

        Checks for the specific error format returned by tools when authentication is required:
        {
          "errorCode": "need-credentials",
          "authorizeUrl": "https://....",
          "message": "This tool requires secondary authorization..."
        }

        Returns auth error metadata dict if auth error detected, None otherwise.
        """
        try:
            # First try to parse as JSON to detect structured auth errors
            content_dict = json.loads(content)

            # Check for the specific "need-credentials" error format
            if isinstance(content_dict, dict) and content_dict.get("errorCode") == "need-credentials":
                authorize_url = content_dict.get("authorizeUrl", "")
                error_message = content_dict.get("message", "Authentication required.")

                logger.info(f"[AUTH MIDDLEWARE] Detected JSON auth error: {error_message}")
                logger.info(f"[AUTH MIDDLEWARE] Auth URL: {authorize_url}")

                # Return auth error metadata
                return {"auth_url": authorize_url, "auth_message": error_message, "error_code": "need-credentials"}
        except json.JSONDecodeError:
            # Not JSON, check for text patterns that might indicate auth errors
            content_lower = content.lower()
            auth_patterns = [
                "authentication required",
                "authorization required",
                "need credentials",
                "please authorize",
                "login required",
                "401 unauthorized",
                "access denied",
            ]

            for pattern in auth_patterns:
                if pattern in content_lower:
                    logger.info(f"[AUTH MIDDLEWARE] Detected text auth error pattern: {pattern}")
                    return {"auth_url": "", "auth_message": content, "error_code": "auth-required"}

        return None
