"""Tool Schema Cleaning Middleware - Cleans tool schemas for Gemini compatibility.

This middleware handles tool schema cleaning at model-binding time to ensure
compatibility with Gemini's strict tool validation while keeping BaseTool instances
intact for ToolNode execution.

The middleware intercepts wrap_model_call to clean tool schemas before sending them
to the model, but does NOT modify the original BaseTool instances stored in ToolNode.

Why This Matters:
- Gemini rejects tool schemas with None annotations/defaults (strict validation)
- OpenAI and Claude accept these schemas (lenient validation)
- ToolNode needs BaseTool instances for execution (not dicts)
- Solution: Clean dicts for model binding, keep BaseTool for execution

This middleware is simpler than DynamicToolDispatchMiddleware because:
- It doesn't handle dynamic tool injection (tools are already registered)
- It only cleans schemas, doesn't dispatch execution
- Used in sub-agents where tools are pre-defined

Progressive Retry Strategy:
- Try MINIMAL cleanup first (only None values - documented requirement)
- On INVALID_ARGUMENT error, retry with MODERATE (+ ALL enums - global state limit)
- On INVALID_ARGUMENT error, retry with AGGRESSIVE (+ format/min/max/array constraints)
- Log which level succeeds to track patterns (usually succeeds at MODERATE for 80+ tools)

See app/utils.py (Tool Schema Cleaning section) for detailed explanation.
"""

import logging
from typing import Any, Awaitable, Callable, cast

from langchain.agents.middleware.types import AgentMiddleware, ModelCallResult, ModelRequest, ModelResponse
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError

from ..utils import CleanupLevel, validate_and_clean_tool_dict

logger = logging.getLogger(__name__)


class ToolSchemaCleaningMiddleware(AgentMiddleware):
    """Middleware for cleaning tool schemas at model-binding time.

    Intercepts model calls and converts BaseTool instances to cleaned dict format
    for Gemini compatibility without modifying the original tools in ToolNode.

    This ensures:
    1. Model receives clean schemas (Gemini-compatible)
    2. ToolNode keeps original BaseTool instances (execution works)
    3. No in-place modifications that could break tool execution
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Clean tool schemas before model call.

        Converts BaseTool instances to cleaned dict format for model binding
        without modifying the original tools.

        Args:
            request: Model request with tools
            handler: Callback to execute the model

        Returns:
            Model response
        """
        if not request.tools:
            return handler(request)

        # Convert tools to cleaned dict format
        cleaned_tools = self._clean_tools(request.tools)

        # Override request with cleaned tools (dict format bypasses validation)
        # Cast needed because list is invariant in Python typing
        return handler(request.override(tools=cast(list[BaseTool | dict], cleaned_tools)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Async version of wrap_model_call with progressive retry.

        Args:
            request: Model request with tools
            handler: Async callback to execute the model

        Returns:
            Model response
        """
        if not request.tools:
            return await handler(request)

        # Try progressive cleanup levels on INVALID_ARGUMENT errors
        # MODERATE removes all enums (solves global state space limit with 80+ tools)
        for level in [CleanupLevel.MINIMAL, CleanupLevel.MODERATE, CleanupLevel.AGGRESSIVE]:
            try:
                cleaned_tools = self._clean_tools(request.tools, level)
                result = await handler(request.override(tools=cast(list[BaseTool | dict], cleaned_tools)))
                return result
            except Exception as e:
                # Check if it's a Gemini INVALID_ARGUMENT error
                is_schema_error = (
                    ChatGoogleGenerativeAIError
                    and isinstance(e, ChatGoogleGenerativeAIError)
                    and "INVALID_ARGUMENT" in str(e)
                    and "schema" in str(e).lower()
                )

                if is_schema_error and level != CleanupLevel.AGGRESSIVE:
                    # Log and retry with next level
                    tool_count = len(request.tools)
                    logger.warning(
                        f"Schema validation failed with {level.value} cleanup, retrying with next level. "
                        f"Tool count: {tool_count}, Error: {str(e)[:200]}"
                    )
                    continue
                else:
                    # Not a schema error or already tried aggressive - re-raise
                    raise

        # Should not reach here, but for type checker
        raise RuntimeError("All cleanup levels exhausted")

    def _clean_tools(self, tools: list[Any], level: CleanupLevel = CleanupLevel.MINIMAL) -> list[dict[str, Any]]:
        """Convert tools to cleaned dict format.

        Creates dict representations without modifying original BaseTool instances.

        Args:
            tools: List of BaseTool instances or dicts
            level: Cleanup level to apply

        Returns:
            List of cleaned tool dicts
        """
        cleaned_tools = []

        for tool in tools:
            if isinstance(tool, BaseTool):
                # Convert to dict (creates a copy, doesn't modify original)
                tool_dict = convert_to_openai_tool(tool)
                # Clean the dict schema for Gemini
                tool_dict = validate_and_clean_tool_dict(tool_dict, level)
                cleaned_tools.append(tool_dict)
            elif isinstance(tool, dict):
                # Already in dict format, just clean
                tool_dict = validate_and_clean_tool_dict(tool, level)
                cleaned_tools.append(tool_dict)
            else:
                # Unknown format, pass through
                cleaned_tools.append(tool)

        return cleaned_tools

    def _get_tool_name(self, tool: Any) -> str:
        """Extract tool name for logging."""
        if isinstance(tool, BaseTool):
            return tool.name
        elif isinstance(tool, dict):
            return tool.get("function", {}).get("name", "unknown")
        return "unknown"
