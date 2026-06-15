"""
Final response schema for agent completion status determination.

This schema is used as structured output by the model to explicitly determine
the A2A task status when the graph execution completes. The model considers:
1. Conversation history
2. Todo list state (pending, in-progress, completed tasks)
3. Whether user input or authentication is needed
"""

from typing import Literal

from a2a.types import TaskState
from pydantic import BaseModel, Field, field_validator

# A2A v1.0+ TaskState is a protobuf int enum and can't be a Pydantic/LLM-tool field
# type, so the schema exposes a short string the model picks; ``a2a_state`` maps it back.
_TASK_STATE_BY_NAME: dict[str, int] = {
    "completed": TaskState.TASK_STATE_COMPLETED,
    "working": TaskState.TASK_STATE_WORKING,
    "input_required": TaskState.TASK_STATE_INPUT_REQUIRED,
    "failed": TaskState.TASK_STATE_FAILED,
}
_TASK_STATE_TO_NAME: dict[int, str] = {v: k for k, v in _TASK_STATE_BY_NAME.items()}


class FinalResponseSchema(BaseModel):
    """Structured output schema for agent final response with explicit task status.

    The model uses this to determine the appropriate A2A task state based on:
    - Current todo list state (all completed, some pending, some in progress)
    - Conversation state (waiting for auth, waiting for user input, complete)
    - Whether the task has actually been accomplished or needs more work

    This replaces the hardcoded "completed" assumption and allows the agent
    to signal when tasks are still ongoing, need input, or have failed.
    """

    task_state: Literal["completed", "working", "input_required", "failed"] = Field(
        description=(
            "The A2A task state for this response. Choose based on:\n"
            "- completed: All todos done, user request fully satisfied, no further action needed\n"
            "- working: Long-running task in progress, some todos pending/in-progress, will continue asynchronously\n"
            "- input_required: Need clarification or additional information from user to proceed\n"
            "- failed: Encountered an unrecoverable error or cannot complete the task\n"
            "\n"
            "Consider the todo list state:\n"
            "- If all todos are 'completed' and the request is satisfied -> completed\n"
            "- If some todos are 'pending' or 'in_progress' for long-running work -> working\n"
            "- If blocked on user input and cannot proceed -> input_required\n"
            "- If an error occurred that prevents completion -> failed"
        )
    )

    message: str = Field(
        description=(
            "The final message to display to the user. Should be:\n"
            "- For 'completed': Clear and concise summary of what was accomplished\n"
            "- For 'working': Explain what's happening asynchronously and estimated timeline\n"
            "- For 'input_required': Ask a clear, specific question that the user needs to answer\n"
            "- For 'failed': Explain what went wrong and suggest possible next steps or alternatives\n"
            "\n"
            "⚠️ CRITICAL - When include_subagent_output=true:\n"
            "Use EMPTY STRING '' in 99% of cases! Sub-agents include their own introductions.\n"
            "Adding your own introduction creates redundant, confusing text.\n"
            "\n"
            "Examples:\n"
            "✅ include_subagent_output=true, message='' (sub-agent output is self-contained)\n"
            "❌ include_subagent_output=true, message='Here\\'s the result:' (creates redundancy!)\n"
            "\n"
            "ONLY add a message if sub-agent returns raw data WITHOUT explanation (rare)."
        )
    )

    include_subagent_output: bool = Field(
        default=False,
        description=(
            "Set to true when the most recent sub-agent response should be appended to your message. "
            "Use this when:"
            "\n- The sub-agent returned a complete, well-formatted answer that fully addresses the user's request"
            "\n- The response is long or detailed (analysis, data, reports, etc.) and regenerating would be wasteful"
            "\n- You want to preserve the sub-agent's exact output without modification"
            "\n\nWhen true:"
            "\n- Set 'message' field to EMPTY STRING '' (default - sub-agents have their own intros)"
            "\n- The full sub-agent output will be automatically appended"
            "\n- This saves tokens and preserves the original formatting and details"
            "\n\nIMPORTANT: Use message='' to avoid redundant introductions!"
        ),
    )

    @field_validator("task_state", mode="before")
    @classmethod
    def normalize_task_state(cls, v):
        """Normalize task_state to one of the short string names.

        Accepts the A2A hyphenated form ('input-required'), a protobuf TaskState int
        value, or the short name directly, and returns the short name the field expects.
        """
        # Protobuf TaskState enum value (int)
        if isinstance(v, int) and not isinstance(v, bool) and v in _TASK_STATE_TO_NAME:
            return _TASK_STATE_TO_NAME[v]
        # String form — normalize hyphens and strip any TASK_STATE_ prefix
        if isinstance(v, str):
            normalized = v.replace("-", "_").lower()
            if normalized.startswith("task_state_"):
                normalized = normalized[len("task_state_") :]
            return normalized
        return v  # Let Pydantic handle invalid cases

    @property
    def a2a_state(self) -> int:
        """The protobuf ``TaskState`` enum value corresponding to ``task_state``."""
        return _TASK_STATE_BY_NAME[self.task_state]
