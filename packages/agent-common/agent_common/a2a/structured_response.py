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

from langchain.agents.structured_output import AutoStrategy, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

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


def get_response_format(
    model: BaseChatModel,
    tools: List[BaseTool],
    thinking_enabled: bool = False,
) -> Optional[Any]:
    """Get the appropriate response_format strategy for a model.

    Encapsulates the model-specific logic for structured output:
    - OpenAI/Azure: ToolStrategy (avoids .parse() API that requires strict tools)
    - Bedrock without thinking: AutoStrategy
    - Bedrock with thinking: None + SubAgentResponseSchema added as a tool
    - Others (Gemini, etc.): AutoStrategy

    When thinking is enabled on Bedrock, the response_format is set to None and
    a SubAgentResponseSchema StructuredTool is appended to the tools list in-place.
    The caller should pass the tools list BEFORE calling create_agent so the tool
    is included in the graph.

    Args:
        model: The LangChain model instance
        tools: The tools list (may be mutated for Bedrock+thinking)
        thinking_enabled: Whether extended thinking is enabled

    Returns:
        The response_format strategy, or None for Bedrock+thinking
    """
    model_class = model.__class__.__name__

    if model_class == "AzureChatOpenAI":
        return ToolStrategy(schema=SubAgentResponseSchema)
    elif model_class == "ChatBedrockConverse":
        if thinking_enabled:
            # AWS Bedrock doesn't allow forcing tool usage via 'tool_choice = "any"' when
            # thinking is enabled, so we softly enforce it by adding the response tool
            # directly to the tools list while setting response_format to None
            tools.append(
                StructuredTool.from_function(
                    func=lambda **kwargs: SubAgentResponseSchema(**kwargs),
                    name="SubAgentResponseSchema",
                    description="ALWAYS use this tool to format your final response to the user.",
                    args_schema=SubAgentResponseSchema,
                    return_direct=True,
                )
            )
            return None
        else:
            return AutoStrategy(schema=SubAgentResponseSchema)
    elif model_class == "ChatGoogleGenerativeAI":
        # Gemini models: use explicit SubAgentResponseSchema tool instead of AutoStrategy
        # because Gemini outputs structured JSON in content text rather than via tool_call_chunks,
        # causing raw JSON to be streamed to the client
        tools.append(
            StructuredTool.from_function(
                func=lambda **kwargs: SubAgentResponseSchema(**kwargs),
                name="SubAgentResponseSchema",
                description="ALWAYS use this tool to format your final response to the user.",
                args_schema=SubAgentResponseSchema,
                return_direct=True,
            )
        )
        return None
    else:
        return AutoStrategy(schema=SubAgentResponseSchema)


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
