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

from agent_common.core.hitl_resume import (
    NO_WORKAROUND_CLAUSE,
    NOT_MISSING_CLAUSE,
    authorization_verdict,
    classify_reply,
    name_or_nothing,
    pending_authorization_answer,
)
from typing_extensions import NotRequired

logger = logging.getLogger(__name__)

# Compiled once at import: `_detect_auth_error` runs on every tool result.
# Used to pull the structured fields out of an embedded ``need-credentials`` payload.
_AUTHORIZE_URL_RE = re.compile(r'"authorizeUrl"\s*:\s*"([^"]+)"')
_AUTH_MESSAGE_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')

# "Run the call again": what a resumed auth interrupt returns when the user said
# the authorization is done. A sentinel rather than a bare retry inside
# `_after_auth_interrupt`, so the retry goes back through the SAME detection loop
# in `awrap_tool_call` and a still-unauthorized call asks once more.
_RETRY = object()

# The actual JSON field, not a bare substring — a business payload merely
# mentioning the word "need-credentials" in prose must not match this.
_NEED_CREDENTIALS_FIELD_RE = re.compile(r'"errorCode"\s*:\s*"need-credentials"')

# The inner tool / server the PTC guard stamps onto the payload before it escapes
# the sandbox (ptc_guard.annotate_need_credentials), read back out of the wrapped
# text the same way as authorizeUrl.
_AUTH_TOOL_RE = re.compile(r'"tool"\s*:\s*"([^"]+)"')
_AUTH_SERVICE_RE = re.compile(r'"service"\s*:\s*"([^"]+)"')


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
        2. If the result (or a ToolException) carries an auth error: call interrupt()
        3. On resume, act on the answer — retry the call, or hand the model a refusal
        4. A retry goes through the SAME detection, so a still-missing credential
           asks again as a card instead of reaching the model as prose

        For A2A sub-agents (task tool), also checks a2a_metadata for requires_auth flag
        and state=auth_required, providing a structured way to detect auth requirements.

        NOTE: We catch exceptions here to call interrupt() before ToolRetryMiddleware
        converts them to ToolMessages, ensuring immediate interruption.
        """
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

        # A refusal that arrived while this tool was waiting on authorization has to
        # be honored BEFORE the tool runs. Below, the answer is only read where the
        # call fails a second time — so once the user has completed the login in
        # their browser the retry succeeds, and their "no" is read by no one.
        veto = self._refusal_before_running(request, tool_name)
        if veto is not None:
            return veto

        # Extract subagent_type from tool call args (if this is a task tool)
        subagent_type = None
        if tool_name == "task":
            args = request.tool_call.get("args", {})
            subagent_type = args.get("subagent_type")

        # One pass = run the tool, look at what came back. An APPROVED resume comes
        # back HERE rather than calling the handler on its own: the detection lives
        # in this loop, so a retry that is still unauthorized raises the card again
        # instead of handing the model a `need-credentials` payload to read aloud —
        # the exact failure this middleware exists to remove.
        while True:
            result, auth_requirement = await self._call_and_detect(request, handler, tool_name, subagent_type)
            if auth_requirement is None:
                return result

            logger.info(f"[AUTH MIDDLEWARE] Interrupting graph for auth requirement: {tool_name}")

            # Pause the graph and surface the auth requirement to the client.
            # On the FIRST pass this raises; on a resume it RETURNS the client's
            # answer, and `_after_auth_interrupt` acts on it — asking for a retry,
            # or handing the model a refusal — rather than falling through with
            # the stale auth error in hand.
            #       TODO: could we though hit the edge case where another interrupt will collect the Command
            #             which was meant to be catched here?
            resume = interrupt(auth_requirement)
            outcome = await self._after_auth_interrupt(resume, request, tool_name, auth_requirement)
            if outcome is _RETRY:
                continue
            return outcome

    async def _call_and_detect(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
        tool_name: str,
        subagent_type: str | None,
    ) -> tuple[ToolMessage | Command | None, Dict[str, Any] | None]:
        """Run the tool once: ``(result, None)``, or ``(None, auth_requirement)``.

        The single place a tool result — plain, wrapped in a ``Command``, or raised
        as a ``ToolException`` — is examined for an auth error, so the first call
        and every resumed retry are checked identically.
        """
        from langchain_core.tools import ToolException

        try:
            result = await handler(request)
        except ToolException as e:
            # Auth-related ToolExceptions become an interrupt before
            # ToolRetryMiddleware can turn them into a ToolMessage.
            exception_str = str(e)
            logger.info(f"[AUTH MIDDLEWARE] Caught ToolException: {exception_str}")
            auth_metadata = self._detect_auth_error(exception_str)
            if auth_metadata:
                logger.info(f"[AUTH MIDDLEWARE] ToolException carries an auth requirement: {tool_name}")
                return None, self._auth_requirement(auth_metadata, request, tool_name)
            # Not an auth error, let the exception propagate normally
            raise

        logger.debug(f"[AUTH MIDDLEWARE awrap_tool_call] Tool executed successfully: {tool_name}")

        # The ToolMessage to inspect: returned directly, or carried in a Command's
        # state update (the shape the deep-agent tools use).
        message: ToolMessage | None = None
        if isinstance(result, ToolMessage):
            message = result
        elif isinstance(result, Command):
            update = getattr(result, "update", None) or {}
            messages = update.get("messages") if isinstance(update, dict) else None
            if messages and isinstance(messages[-1], ToolMessage):
                message = messages[-1]
        if message is None:
            return result, None

        # First check A2A metadata (for task tool), then fall back to
        # content-based detection.
        additional_kwargs = getattr(message, "additional_kwargs", {})
        auth_metadata = self._check_a2a_auth_metadata(
            additional_kwargs.get("a2a_metadata"), subagent_type or tool_name
        ) or self._detect_auth_error(message.content)
        if not auth_metadata:
            return result, None
        return None, self._auth_requirement(auth_metadata, request, tool_name)

    @staticmethod
    def _auth_requirement(
        auth_metadata: Dict[str, Any], request: ToolCallRequest, tool_name: str
    ) -> Dict[str, Any]:
        """The interrupt value describing what needs authorizing."""
        return {
            "task_state": TaskState.TASK_STATE_AUTH_REQUIRED,
            # The INNER tool when the sandbox stamped one: a `need-credentials`
            # raised by an MCP call inside `eval` is reported against `eval`, and
            # naming that tells both the user and the model the wrong thing.
            "tool": auth_metadata.get("tool") or tool_name,
            "service": auth_metadata.get("service") or "",
            "subagent": auth_metadata.get("subagent"),  # May be None for non-A2A tools
            "message": auth_metadata.get("auth_message", "Authentication required"),
            "auth_url": auth_metadata.get("auth_url", ""),
            "error_code": auth_metadata.get("error_code", "need-credentials"),
            # WHICH call is blocked. The executor echoes it back on the answer, so
            # a "no" settles the call it was asked about and not its parallel
            # siblings; it also rides out as the auth payload's correlation id.
            "tool_call_id": request.tool_call.get("id", ""),
            "timestamp": time.time(),
        }

    def _refusal_before_running(self, request: ToolCallRequest, tool_name: str) -> ToolMessage | None:
        """The user's refusal of a pending authorization, honored before the tool runs.

        The answer to an auth prompt is otherwise read where the tool fails a
        SECOND time — and once the user has completed the login in their browser
        the retry SUCCEEDS, so that point is never reached: "No way I'll authorize
        this!" was read by nobody and the profile was fetched anyway.

        The answer is self-identifying (``{"authorization": {...}}``), so it can be
        found wherever it sits among the task's resume values — it is rarely the one
        the next ``interrupt()`` consumes, since a settled tool-approval replays
        first. Words never reach here: the executor classifies them into this shape
        while the pending interrupt's kind is still known
        (``_classify_authorization_reply``).

        Scoped to the call it was asked about. Every tool call in a ToolNode shares
        the task's resume log, so an unscoped "no" refused the model's OTHER
        parallel calls too: authorization declined for `github_get_me` also
        returned "the user DECLINED..." for the `web_search` beside it, which was
        never blocked on anything. The answer carries the ``tool_call_id`` of the
        blocked call (stamped into the interrupt value by ``_auth_requirement``,
        echoed back by the executor), so only that call is vetoed. An answer with
        no id — an in-flight checkpoint from before the stamping — is honored as
        before rather than dropped.

        Returns the refusal to hand the model, or ``None`` to let the call proceed.
        Nothing is consumed either way.
        """
        answer = pending_authorization_answer()
        if answer is None:
            return None
        verdict, message = self._resume_decision(answer)
        if verdict != "declined":
            return None
        settled_call_id = str((answer.get("authorization") or {}).get("tool_call_id") or "")
        if settled_call_id and settled_call_id != str(request.tool_call.get("id") or ""):
            logger.info(
                f"[AUTH MIDDLEWARE] Refusal was for call {settled_call_id}, not {tool_name}; letting it run"
            )
            return None
        logger.info(f"[AUTH MIDDLEWARE] Refusal read BEFORE running {tool_name}; not executing it")
        return self._refusal_message(request, tool_name, message, None)

    @staticmethod
    def _resume_decision(resume: Any) -> tuple[str, str]:
        """What the client's resume value meant: ``approved``/``declined``/``unclear``.

        A client that negotiated the in-task-auth extension resumes with
        ``{"authorization": {"decision": ..., "message": ...}}`` and there is
        nothing to interpret. Anything else is the user's own words — typed into
        the composer while the turn was parked — and is deliberately reported as
        ``unclear`` rather than guessed at here.
        """
        verdict, message = authorization_verdict(resume)
        if verdict is not None:
            return verdict, message
        return "unclear", resume if isinstance(resume, str) else message

    async def _after_auth_interrupt(
        self,
        resume: Any,
        request: ToolCallRequest,
        tool_name: str,
        auth_requirement: Dict[str, Any] | None = None,
    ) -> ToolMessage | Command | object:
        """Turn a resumed auth interrupt back into a tool result, or ask for a retry.

        The graph resumes INSIDE the tool call that was blocked, holding the auth
        error it already got. Returning that error unchanged — what this used to
        do — hands the model a payload whose text says "visit this URL", and the
        model dutifully relays it as prose. That is why a second attempt never
        produced a card: the interrupt had been consumed, and nothing ever asked
        again.

        So the resume is acted on instead:

        - **approved** — ``_RETRY``, so the caller runs the tool again now that the
          credential should exist. The retry goes back through the detection loop,
          which is what makes a still-unauthorized call interrupt a SECOND time —
          the prompt comes back as a card instead of turning into a paragraph.
          Calling the handler here instead would skip the detection entirely and
          hand the raw `need-credentials` payload straight to the model.
        - **declined** — hand the model a refusal, explicitly telling it not to
          retry, so it digests the "no" instead of pushing the link again.
        - **unclear** — the user typed something instead of clicking. A small
          fast-LLM classifier reads it first ("ok, done" -> approved, "no, those
          scopes are too wide" -> declined); only a clear verdict is acted on.
          When it cannot tell, the words and the two options are handed to the
          model, which is about to run anyway. Re-calling the tool re-enters the
          branch above, so a genuine "done" still ends in a retry.
        """
        subject_tool = str((auth_requirement or {}).get("tool") or "") or tool_name
        verdict, message = self._resume_decision(resume)
        if verdict == "unclear" and message.strip():
            # No structured answer: the client never negotiated the in-task-auth
            # extension, or the user just kept typing. A keyword match cannot tell
            # "ok, done" from "no, those scopes are too wide", so a small fast-LLM
            # classifier reads it — and only a clear verdict is acted on (None
            # keeps the old behaviour of handing the words to the model).
            intent = await classify_reply(
                message,
                [],
                question=(
                    f"The assistant asked the user to authorize `{subject_tool}` "
                    "(a one-time login in their browser) before it could run. "
                    "Read their reply as: approve = the authorization is done / go ahead and retry now; "
                    "reject = they refuse to authorize it."
                ),
            )
            if intent == "approve":
                verdict = "approved"
            elif intent == "reject":
                verdict = "declined"
        logger.info(f"[AUTH MIDDLEWARE] Resumed auth interrupt for {tool_name}: {verdict}")

        if verdict == "approved":
            return _RETRY

        if verdict == "declined":
            return self._refusal_message(request, tool_name, message, auth_requirement)

        reply = message.strip() or "(no reply)"
        content = (
            f"The authorization required by {self._subject(tool_name, auth_requirement)} is still "
            f"pending. They replied: {reply}\n"
            f"{NOT_MISSING_CLAUSE}\n"
            "If that means they completed it, call the tool again now — it will "
            "ask once more if it is still unauthorized. If it means they refuse, "
            "do not retry and do not repeat the authorization link: say what you "
            f"cannot do without it. {NO_WORKAROUND_CLAUSE}"
        )
        return ToolMessage(content=content, tool_call_id=request.tool_call.get("id", ""), name=tool_name)

    @staticmethod
    def _subject(tool_name: str, auth_requirement: Dict[str, Any] | None) -> str:
        """What to call the thing being authorized, in a sentence the model reads.

        NEVER sandbox plumbing. A `need-credentials` raised by an MCP call made in
        the sandbox is reported against `eval`, and "the user declined to authorize
        eval" is what made the agent conclude the real tool did not exist and
        answer "github_get_me is not available in the current environment".
        """
        inner = str((auth_requirement or {}).get("tool") or "") or tool_name
        named = name_or_nothing(inner) or name_or_nothing(tool_name)
        service = name_or_nothing((auth_requirement or {}).get("service"))
        if service and named:
            return f"{service} (`{named}`)"
        if service:
            return service
        if named:
            return f"`{named}`"
        return "the call that needed it"

    def _refusal_message(
        self,
        request: ToolCallRequest,
        tool_name: str,
        message: str,
        auth_requirement: Dict[str, Any] | None,
    ) -> ToolMessage:
        """The refusal handed to the model — identical whichever path read the "no"."""
        reason = f" They said: {message}" if message.strip() else ""
        content = (
            f"The user DECLINED the authorization required by "
            f"{self._subject(tool_name, auth_requirement)}.{reason} "
            f"{NOT_MISSING_CLAUSE} "
            "Do not retry the call and do not send the authorization link again. "
            "Tell them they skipped the authorization and say plainly what you "
            f"cannot do without it. {NO_WORKAROUND_CLAUSE}"
        )
        return ToolMessage(content=content, tool_call_id=request.tool_call.get("id", ""), name=tool_name)

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
                return {
                    "auth_url": authorize_url,
                    "auth_message": error_message,
                    "error_code": "need-credentials",
                    # Stamped by the PTC guard where the inner tool is still known
                    # (ptc_guard.annotate_need_credentials). Absent for a tool that
                    # failed outside the sandbox, where the outer name IS the truth.
                    "tool": content_dict.get("tool") or "",
                    "service": content_dict.get("service") or "",
                }
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
            tool_match = _AUTH_TOOL_RE.search(content)
            service_match = _AUTH_SERVICE_RE.search(content)
            logger.info(f"[AUTH MIDDLEWARE] Detected embedded auth error. Auth URL: {authorize_url}")
            return {
                "auth_url": authorize_url,
                "auth_message": error_message,
                "error_code": "need-credentials",
                "tool": tool_match.group(1) if tool_match else "",
                "service": service_match.group(1) if service_match else "",
            }

        return None
