"""
Final response schema for agent completion status determination.

This schema is used as structured output by the model to explicitly determine
the A2A task status when the graph execution completes. The model considers:
1. Conversation history
2. Todo list state (pending, in-progress, completed tasks)
3. Whether user input or authentication is needed
"""

from typing import Literal, Optional

from a2a.types import TaskState
from pydantic import BaseModel, Field


class FinalResponseSchema(BaseModel):
    """Structured output schema for agent final response with explicit task status.

    The model uses this to determine the appropriate A2A task state based on:
    - Current todo list state (all completed, some pending, some in progress)
    - Conversation state (waiting for auth, waiting for user input, complete)
    - Whether the task has actually been accomplished or needs more work

    This replaces the hardcoded "completed" assumption and allows the agent
    to signal when tasks are still ongoing, need input, or have failed.
    """

    task_state: Literal[
        TaskState.completed,
        TaskState.working,
        TaskState.input_required,
        TaskState.failed,
    ] = Field(
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
            "- For 'failed': Explain what went wrong and suggest possible next steps or alternatives"
        )
    )

    reasoning: str = Field(
        description=(
            "Brief internal reasoning for why this task_state was chosen. "
            "Explain the key factors from todo list and conversation that led to this decision. "
            "This helps with debugging and transparency but is not shown to the user."
        )
    )

    todo_summary: Optional[str] = Field(
        default=None,
        description=(
            "Optional brief summary of todo list state (e.g., '3/5 tasks completed, 2 in progress'). "
            "Useful for 'working' state to give progress visibility."
        ),
    )
