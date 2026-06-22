"""Structured response support for LangGraph-based A2A sub-agents.

This module provides shared infrastructure for sub-agents that use LangGraph
with structured output (SubAgentResponseSchema) to explicitly determine
task state (completed, input_required, failed).

Components:
- SubAgentResponseSchema: Pydantic model for structured LLM output
- A2A_PROTOCOL_ADDENDUM: System prompt addition instructing the LLM to use the schema
- StructuredResponseMixin: Mixin providing result translation and response_format helpers
- get_response_format: Helper to get the correct response_format strategy for a model

Used by:
- GPAgentRunnable (general-purpose agent)
- DynamicLocalAgentRunnable (user-configured sub-agents)
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent_common.core.model_factory import get_model_provider

from .stream_events import (
    TaskResponseData,
)

logger = logging.getLogger(__name__)


class SubAgentResponseSchema(BaseModel):
    """Structured output schema for sub-agent responses.

    Sub-agents MUST use this schema to explicitly indicate their task state.
    This eliminates guessing based on message content patterns.
    """

    task_state: Literal["completed", "input_required", "failed"] = Field(
        description=(
            "The task state for this response:\n"
            "- completed: Task finished successfully, provide summary of what was done\n"
            "- input_required: Need more information from user to proceed, ask a clear question\n"
            "- failed: Encountered an error that prevents completion, explain what went wrong"
        )
    )

    message: str = Field(
        description=(
            "The response message to send back:\n"
            "- For 'completed': Summary of what was accomplished\n"
            "- For 'input_required': Clear question asking for the specific information needed\n"
            "- For 'failed': Explanation of the error and any possible remediation"
        )
    )


# System prompt addendum that instructs the agent to use structured output
A2A_PROTOCOL_ADDENDUM = """
<response_protocol>
You are a sub-agent communicating with an orchestrator. You must determine the appropriate task state for every response:

- completed: You have successfully completed the requested task. Provide a clear summary of what was accomplished.
- input_required: You need additional information from the user to proceed. Ask a specific, clear question.
- failed: You encountered an error that prevents completion. Explain what went wrong and any potential remediation steps.

Do not leave the task state ambiguous.
</response_protocol>
"""


def select_response_format(
    model_type: Optional[str],
    schema: type,
    *,
    thinking_enabled: bool = False,
    has_builtin_tools: bool = False,
) -> tuple[Optional[Any], bool]:
    """Pick the structured-output strategy for a gateway-served model.

    Single source of truth for the provider -> strategy decision, shared by the
    sub-agent path (``get_response_format``) and the orchestrator main graph. The
    decision follows from the model's *provider* (gateway model_info via
    ``get_model_provider``), NOT the client class — every client is now a
    ``ChatOpenAI`` talking to the LiteLLM gateway.

    Returns ``(response_format, requires_response_tool)``:
      - ``(ToolStrategy(schema), False)`` — force the model to emit ``schema`` via a
        tool call. langchain binds structured-output tools with ``tool_choice="any"``.
      - ``(None, True)`` — don't force; the caller must bind ``schema`` as an ordinary
        tool and let the model call it on its own.

    ``ToolStrategy`` is the right default: the gateway normalizes provider responses
    (including Gemini's text-embedded JSON) into OpenAI-shape ``tool_calls``, and
    ``AutoStrategy`` would instead resolve to the native ``.parse()`` path, which
    requires every bound tool to be strict — dynamic MCP tools are not. Two situations
    can't tolerate the forced ``tool_choice="any"`` and so use the bind-as-tool path:
      - Extended thinking on a provider not known to allow forced tool use with
        thinking. Anthropic/Bedrock explicitly reject it ("Thinking may not be enabled
        when tool_choice forces tool use"); only OpenAI/Azure are known-safe to force.
        Anything else — Gemini, or an unknown/cold-cache provider (``""``) — is treated
        as unsafe, so a stale gateway snapshot can never force a forbidden combination.
      - Models with server-side built-in tools (e.g. Gemini google_search /
        code_execution): a forced function-call ``tool_choice`` can't coexist with
        built-in tools, which must be free to run before the final response.

    Args:
        model_type: The model alias (gateway-registered name), or None if unknown
        schema: The structured-output schema (e.g. SubAgentResponseSchema, FinalResponseSchema)
        thinking_enabled: Whether extended thinking is enabled
        has_builtin_tools: Whether server-side built-in tools will be bound to the model

    Returns:
        ``(response_format, requires_response_tool)``
    """
    provider = get_model_provider(model_type) if model_type else ""
    is_openai_like = "openai" in provider or provider.startswith("azure")

    if has_builtin_tools or (thinking_enabled and not is_openai_like):
        return None, True
    return ToolStrategy(schema=schema), False


def get_response_format(
    model_type: Optional[str],
    tools: List[BaseTool],
    thinking_enabled: bool = False,
) -> Optional[Any]:
    """Structured-output strategy for a sub-agent's model (SubAgentResponseSchema).

    Thin wrapper over ``select_response_format``: sub-agents never bind server-side
    built-in tools, so ``has_builtin_tools=False``. When the schema must be bound as a
    plain tool (Anthropic/Bedrock + thinking), it is appended to ``tools`` in place —
    the caller must pass the tools list BEFORE building the graph so it's included.

    Args:
        model_type: The model alias (gateway-registered name), or None if unknown
        tools: The tools list (mutated in place when the schema is bound as a tool)
        thinking_enabled: Whether extended thinking is enabled

    Returns:
        The response_format strategy, or None when the schema is bound as a tool
    """
    response_format, requires_response_tool = select_response_format(
        model_type,
        SubAgentResponseSchema,
        thinking_enabled=thinking_enabled,
        has_builtin_tools=False,
    )
    if requires_response_tool:
        tools.append(
            StructuredTool.from_function(
                func=lambda **kwargs: SubAgentResponseSchema(**kwargs),
                name="SubAgentResponseSchema",
                description="ALWAYS use this tool to format your final response to the user.",
                args_schema=SubAgentResponseSchema,
                return_direct=True,
            )
        )
    return response_format


class StructuredResponseMixin:
    """Mixin providing structured response translation for LangGraph-based sub-agents.

    Translates LangGraph agent results (which use SubAgentResponseSchema for
    structured output) into A2A protocol responses using the _build_*_response
    helpers inherited from LocalA2ARunnable.

    This mixin expects the class to also inherit from LocalA2ARunnable (or any
    class providing _build_success_response, _build_input_required_response,
    _build_error_response).

    Used by:
    - GPAgentRunnable
    - DynamicLocalAgentRunnable
    """

    def _translate_agent_result(
        self,
        result: Dict[str, Any],
        context_id: Optional[str],
        task_id: Optional[str],
    ) -> "TaskResponseData":
        """Translate LangGraph agent result to A2A protocol format.

        The agent uses SubAgentResponseSchema for structured output, so we
        extract the task_state and message from the structured response.

        This eliminates guessing based on message patterns - the LLM explicitly
        declares its task state just like the orchestrator does.

        Extraction order:
        1. result["structured_response"] (AutoStrategy / ToolStrategy on OpenAI)
        2. Tool call messages named "SubAgentResponseSchema" (Bedrock+thinking)
        3. Fallback to last message content with "completed" state (shouldn't happen)

        Args:
            result: The LangGraph agent's result dict
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID for this invocation

        Returns:
            Dict with 'messages' and A2A metadata
        """
        # Check for structured_response (AutoStrategy for OpenAI)
        structured_response = result.get("structured_response")
        if structured_response and isinstance(structured_response, SubAgentResponseSchema):
            return self._build_response_from_schema(structured_response, context_id, task_id)

        # Check messages for tool call with SubAgentResponseSchema (Bedrock)
        agent_name = getattr(self, "name", "unknown")
        logger.info(f"Translating agent result for '{agent_name}'")
        messages = result.get("messages", [])
        for msg in reversed(messages):
            # Check if this is a tool message with SubAgentResponseSchema result
            if hasattr(msg, "name") and msg.name == "SubAgentResponseSchema":
                try:
                    # The tool returns a SubAgentResponseSchema instance
                    if isinstance(msg.content, SubAgentResponseSchema):
                        return self._build_response_from_schema(msg.content, context_id, task_id)
                except Exception as e:
                    logger.warning(f"Failed to parse SubAgentResponseSchema from tool message: {e}")

            # Check for tool_calls that invoked SubAgentResponseSchema
            if hasattr(msg, "tool_calls"):
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "SubAgentResponseSchema":
                        try:
                            schema = SubAgentResponseSchema(**tool_call.get("args", {}))
                            return self._build_response_from_schema(schema, context_id, task_id)
                        except Exception as e:
                            logger.warning(f"Failed to parse SubAgentResponseSchema from tool_call: {e}")

        # Fallback: If no structured response found, extract last message and treat as completed
        # This shouldn't happen if the agent follows the protocol correctly
        if messages:
            last_message = messages[-1]
            content = last_message.content if hasattr(last_message, "content") else str(last_message)
            logger.warning(f"No structured response found for '{agent_name}', falling back to completed state")
            return self._build_success_response(content, context_id=context_id, task_id=task_id)  # type: ignore[attr-defined]

        return self._build_error_response(  # type: ignore[attr-defined]
            "Agent returned no response",
            context_id=context_id,
            task_id=task_id,
        )

    def _build_response_from_schema(
        self,
        schema: SubAgentResponseSchema,
        context_id: Optional[str],
        task_id: Optional[str],
    ) -> "TaskResponseData":
        """Build A2A response from SubAgentResponseSchema.

        Args:
            schema: The structured response from the agent
            context_id: Optional context ID for conversation continuity
            task_id: Optional task ID for this invocation

        Returns:
            TaskResponseData with typed lifecycle fields
        """
        if schema.task_state == "completed":
            return self._build_success_response(schema.message, context_id=context_id, task_id=task_id)  # type: ignore[attr-defined]
        elif schema.task_state == "input_required":
            return self._build_input_required_response(schema.message, context_id=context_id, task_id=task_id)  # type: ignore[attr-defined]
        else:  # failed
            return self._build_error_response(schema.message, context_id=context_id, task_id=task_id)  # type: ignore[attr-defined]
