"""
Test the final response schema and status determination logic.
"""

import pytest
from a2a.types import TaskState

from app.models import FinalResponseSchema


def test_final_response_schema_completed():
    """Test creating a completed status response."""
    response = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_COMPLETED,
        message="All tasks completed successfully",
    )

    assert response.a2a_state == TaskState.TASK_STATE_COMPLETED
    assert response.message == "All tasks completed successfully"


def test_final_response_schema_working():
    """Test creating a working status response."""
    response = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_WORKING,
        message="Long-running task in progress",
    )

    assert response.a2a_state == TaskState.TASK_STATE_WORKING
    assert response.message == "Long-running task in progress"


def test_final_response_schema_input_required():
    """Test creating an input_required status response."""
    response = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_INPUT_REQUIRED,
        message="Which file should I update? I found multiple configuration files.",
    )

    assert response.a2a_state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert response.message == "Which file should I update? I found multiple configuration files."


def test_final_response_schema_failed():
    """Test creating a failed status response."""
    response = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_FAILED,
        message="Unable to complete the task",
    )

    assert response.a2a_state == TaskState.TASK_STATE_FAILED
    assert response.message == "Unable to complete the task"


def test_final_response_schema_validation():
    """Test that schema validation works correctly."""
    # Test each valid state individually
    response1 = FinalResponseSchema(task_state=TaskState.TASK_STATE_COMPLETED, message="Test message")
    assert response1.a2a_state == TaskState.TASK_STATE_COMPLETED

    response2 = FinalResponseSchema(task_state=TaskState.TASK_STATE_WORKING, message="Test message")
    assert response2.a2a_state == TaskState.TASK_STATE_WORKING

    response3 = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_INPUT_REQUIRED,
        message="Test message",
    )
    assert response3.a2a_state == TaskState.TASK_STATE_INPUT_REQUIRED

    response4 = FinalResponseSchema(task_state=TaskState.TASK_STATE_FAILED, message="Test message")
    assert response4.a2a_state == TaskState.TASK_STATE_FAILED

    # Invalid state should fail validation
    with pytest.raises(Exception):  # Pydantic will raise validation error
        FinalResponseSchema(
            task_state="invalid_state",  # type: ignore
            message="Test",
        )


def test_final_response_schema_optional_fields():
    """Test that optional fields work correctly."""
    # Minimal response (no optional fields)
    response = FinalResponseSchema(task_state=TaskState.TASK_STATE_COMPLETED, message="Done")

    assert response.include_subagent_output is False

    response_with_summary = FinalResponseSchema(task_state=TaskState.TASK_STATE_WORKING, message="In progress")

    assert response_with_summary.include_subagent_output is False


def test_final_response_schema_include_subagent_output():
    """Test the include_subagent_output field."""
    # Default value (False)
    response = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_COMPLETED,
        message="Here's the analysis:",
    )
    assert response.include_subagent_output is False

    # Explicit True - for pass-through with introduction
    response_with_intro = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_COMPLETED,
        message="The data analyst found the following:",
        include_subagent_output=True,
    )
    assert response_with_intro.include_subagent_output is True
    assert response_with_intro.message == "The data analyst found the following:"

    # Explicit True - for pure pass-through with empty message
    response_passthrough = FinalResponseSchema(
        task_state=TaskState.TASK_STATE_COMPLETED,
        message="",
        include_subagent_output=True,
    )
    assert response_passthrough.include_subagent_output is True
    assert response_passthrough.message == ""
