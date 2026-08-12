"""Authentication Error Detection Middleware for deterministic auth error handling.

This middleware detects authentication errors from tool responses and uses
LangGraph's interrupt mechanism to pause execution in a resumable state.

Key Features:
- Detects structured JSON auth errors (errorCode: "need-credentials") — the
  format the gatana MCP tool gateway emits when secondary authorization is
  required. Deliberately does NOT scan for free-text auth phrasing ("401
  unauthorized", "access denied", etc.): that's ambiguous against arbitrary
  tool payload data and is left for the LLM to interpret, same as A2A 401s.
- Uses interrupt() to pause graph execution when auth is required
- Supports resumable execution after authentication completion

Middleware Stack Position:
    This middleware sits INNER to ``DynamicToolDispatchMiddleware`` (see
    ``graph_factory._create_middleware_stack``).  The middleware list uses
    LangChain's convention for ``wrap_*`` hooks: **first in list = outermost**.
    Because ``DynamicToolDispatchMiddleware`` is first, it short-circuits the
    ``task`` tool for A2A sub-agent dispatch *before* this middleware ever sees
    the call.  As a result, A2A 401 errors are NOT intercepted here — they are
    returned as ToolMessages and handled by the LLM naturally.

    Additionally, response-schema tools (``FinalResponseSchema``,
    ``SubAgentResponseSchema``) are explicitly skipped to avoid false-positive
    interrupts when the LLM merely *reports* an upstream auth error.

Limitations — ``interrupt()`` and ToolNode's error handling:
    When ``interrupt()`` raises ``GraphBubbleUp`` from inside a middleware
    ``awrap_tool_call``, ToolNode's ``_arun_one`` catches it via a broad
    ``except Exception`` and converts it to an error ToolMessage.  This means
    interrupt-based auth detection only works reliably for tools executed through
    the standard ``_execute_tool_async`` path (where ``GraphBubbleUp`` is
    re-raised), **not** when the exception escapes the middleware wrapper.

Integration:
    ```python
    agent = create_deep_agent(
        model=model,
        tools=tools,
        subagents=subagents,
        middleware=[
            DynamicToolDispatchMiddleware(),  # [0] outermost
            ...
            AuthErrorDetectionMiddleware(),   # [5] inner
            ToolRetryMiddleware(),            # [6] inner
            ...
        ],
        checkpointer=MemorySaver()  # Required for interrupt/resume
    )
    ```
"""

import json
import logging
import re
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

# Compiled once at import: `_detect_auth_error` runs on every tool result.
# Used to pull the structured fields out of an embedded ``need-credentials`` payload.
_AUTHORIZE_URL_RE = re.compile(r'"authorizeUrl"\s*:\s*"([^"]+)"')
_AUTH_MESSAGE_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')

# The actual JSON field, not a bare substring — a business payload merely
# mentioning the word "need-credentials" in prose must not match this.
_NEED_CREDENTIALS_FIELD_RE = re.compile(r'"errorCode"\s*:\s*"need-credentials"')


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

    How it works:
    1. awrap_tool_call: Intercept tool executions (excluding response-schema tools)
    2. Tool executes via ``handler(request)`` and returns ToolMessage
    3. _detect_auth_error: Check response content for auth error patterns
    4. If auth error found: Call ``interrupt()`` with auth requirement data
    5. Graph execution pauses and surfaces the interrupt value to the client
    6. Client handles authentication and resumes with ``Command.resume()``

    Skipped Tools:
    - ``FinalResponseSchema`` / ``SubAgentResponseSchema``: These carry the LLM's
      message to the user, not an external-service response.  Scanning their content
      caused false-positive interrupts when the LLM reported an upstream 401.

    Visibility Limitation:
    - ``DynamicToolDispatchMiddleware`` is outermost in the middleware stack and
      short-circuits the ``task`` tool for A2A dispatch.  This middleware (inner)
      therefore never sees ``task`` tool calls — A2A auth errors propagate back
      as normal ToolMessages and are handled by the LLM.

    ``interrupt()`` Caveat:
    - When ``interrupt()`` is called from a middleware ``awrap_tool_call``,
      ``GraphBubbleUp`` can be caught by ToolNode's ``_arun_one`` broad
      ``except Exception`` and silently converted to an error ToolMessage.
      The interrupt only propagates correctly when raised inside
      ``_execute_tool_async`` where ``GraphBubbleUp`` is explicitly re-raised.

    Supported Auth Error Formats:
    - JSON: {"errorCode": "need-credentials", "authorizeUrl": "...", "message": "..."}
      (whole-content or embedded in ToolRetryMiddleware's wrapped exception text).
      This is the only format detected here — deliberately structured-only, since
      free-text auth phrasing is ambiguous against arbitrary tool payload data and
      is left for the LLM to interpret (see "Key Features" above).

    Interrupt Value Format::

        {
            "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
            "tool": "tool_name",
            "message": "Authentication required message",
            "auth_url": "https://oauth.example.com/authorize",
            "error_code": "need-credentials",
            "timestamp": 1234567890.123
        }

    This value is interpreted by the agent_executor to set TaskState.TASK_STATE_AUTH_REQUIRED.
    """

    state_schema = AuthErrorState

    def __init__(self):
        """Initialize the authentication error detection middleware."""
        super().__init__()

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

        # # Skip response schema tools — they carry the LLM's message to the user,
        # # not an actual external-service response.  Scanning their content causes
        # # false-positive interrupts when the LLM merely *reports* a 401 it already
        # # handled (e.g. "I encountered a 401 Unauthorized error…").
        RESPONSE_TOOLS = {"FinalResponseSchema", "SubAgentResponseSchema"}
        if tool_name in RESPONSE_TOOLS:
            return await handler(request)

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

                # If no A2A auth requirement, fall back to content-based detection.
                if not auth_metadata:
                    auth_metadata = self._detect_auth_error(result.content)

                if auth_metadata:
                    # Use interrupt() to pause graph execution with auth requirement
                    auth_requirement = {
                        "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
                        "tool": tool_name,
                        "subagent": auth_metadata.get("subagent"),  # May be None for non-A2A tools
                        "message": auth_metadata.get("auth_message", "Authentication required"),
                        "auth_url": auth_metadata.get("auth_url", ""),
                        "error_code": auth_metadata.get("error_code", "need-credentials"),
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

                        # If no A2A auth requirement, fall back to content-based detection.
                        if not auth_metadata:
                            auth_metadata = self._detect_auth_error(last_msg.content)

                        if auth_metadata:
                            # Use interrupt() to pause graph execution with auth requirement
                            auth_requirement = {
                                "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
                                "tool": tool_name,
                                "subagent": auth_metadata.get("subagent"),  # May be None for non-A2A tools
                                "message": auth_metadata.get("auth_message", "Authentication required"),
                                "auth_url": auth_metadata.get("auth_url", ""),
                                "error_code": auth_metadata.get("error_code", "need-credentials"),
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
                    "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
                    "tool": tool_name,
                    "message": auth_metadata.get("auth_message", "Authentication required"),
                    "auth_url": auth_metadata.get("auth_url", ""),
                    "error_code": auth_metadata.get("error_code", "need-credentials"),
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

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """Normalise tool-message content to a single string.

        Provider content can arrive as a plain string, a list of content blocks
        (``[{"type": "text", "text": "..."}]`` — common with Gemini/Anthropic),
        or other shapes.  We flatten everything to text so pattern/JSON detection
        below has something to scan.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(part.get("text") or part.get("content") or "")
                else:
                    parts.append(str(part))
            return " ".join(p for p in parts if p)
        if content is None:
            return ""
        return str(content)

    def _detect_auth_error(self, content: Any) -> Dict[str, Any] | None:
        """Detect authentication errors deterministically from tool message content.

        Checks for the specific error format returned by tools when authentication is required:
        {
          "errorCode": "need-credentials",
          "authorizeUrl": "https://....",
          "message": "This tool requires secondary authorization..."
        }

        The structured payload is detected in two positions, in order:
        1. The *entire* content is that JSON object (tool returned it verbatim).
        2. The JSON is *embedded* in a larger string.  ``ToolRetryMiddleware``
           wraps the original ``ToolException`` as
           ``"Tool 'X' failed after N attempts with ToolException: {...}. Please try again."``
           so by the time this (outer) middleware sees the result the JSON is no
           longer the whole payload — we locate the ``need-credentials`` marker
           and pull ``authorizeUrl`` / ``message`` out of the surrounding text.

        Deliberately deterministic and structured-only: this is the format the
        gatana MCP tool gateway actually emits when a tool needs secondary
        authorization, and detecting it doesn't require guessing. Free-text
        auth phrasing ("access denied", "please authorize", …) is NOT scanned
        here — it's ambiguous (ordinary business data can legitimately contain
        those words) and, per this middleware's class docstring, is already
        left for the LLM to interpret from the raw tool content, the same way
        A2A 401s and LLM-reported errors are handled.

        Returns auth error metadata dict if auth error detected, None otherwise.
        """
        content = self._content_to_text(content)
        if not content:
            return None

        # 1. Fast path: the whole content is the structured error JSON.
        try:
            content_dict = json.loads(content)
            if isinstance(content_dict, dict) and content_dict.get("errorCode") == "need-credentials":
                authorize_url = content_dict.get("authorizeUrl", "")
                error_message = content_dict.get("message", "Authentication required.")
                logger.info(f"[AUTH MIDDLEWARE] Detected JSON auth error: {error_message}")
                logger.info(f"[AUTH MIDDLEWARE] Auth URL: {authorize_url}")
                return {"auth_url": authorize_url, "auth_message": error_message, "error_code": "need-credentials"}
        except json.JSONDecodeError:
            pass

        # 2. Embedded structured error (e.g. wrapped by ToolRetryMiddleware).
        #    Detect the marker and extract the fields directly from the text so
        #    we don't depend on the whole payload being parseable JSON. Match the
        #    actual `"errorCode":"need-credentials"` field, not a bare substring —
        #    business data merely mentioning "need-credentials" in prose must not
        #    match here.
        if _NEED_CREDENTIALS_FIELD_RE.search(content):
            url_match = _AUTHORIZE_URL_RE.search(content)
            msg_match = _AUTH_MESSAGE_RE.search(content)
            authorize_url = url_match.group(1) if url_match else ""
            if msg_match:
                try:
                    error_message = json.loads(f'"{msg_match.group(1)}"')
                except json.JSONDecodeError:
                    error_message = msg_match.group(1)
            else:
                error_message = "This tool requires secondary authorization."
            logger.info(f"[AUTH MIDDLEWARE] Detected embedded auth error. Auth URL: {authorize_url}")
            return {"auth_url": authorize_url, "auth_message": error_message, "error_code": "need-credentials"}

        return None
