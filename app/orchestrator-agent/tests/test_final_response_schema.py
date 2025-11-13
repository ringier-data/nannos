"""
Test the final response schema and status determination logic.
"""

import pytest
from a2a.types import TaskState

from app.models import FinalResponseSchema


def test_final_response_schema_completed():
    """Test creating a completed status response."""
    response = FinalResponseSchema(
        task_state=TaskState.completed,
        message="All tasks completed successfully",
        reasoning="All 3 todos are completed and user request is satisfied",
    )

    assert response.task_state == TaskState.completed
    assert response.message == "All tasks completed successfully"
    assert response.reasoning == "All 3 todos are completed and user request is satisfied"
    assert response.todo_summary is None


def test_final_response_schema_working():
    """Test creating a working status response."""
    response = FinalResponseSchema(
        task_state=TaskState.working,
        message="Long-running task in progress",
        reasoning="2 out of 5 todos are completed, 3 still in progress",
        todo_summary="2/5 tasks completed",
    )

    assert response.task_state == TaskState.working
    assert response.message == "Long-running task in progress"
    assert response.reasoning == "2 out of 5 todos are completed, 3 still in progress"
    assert response.todo_summary == "2/5 tasks completed"


def test_final_response_schema_input_required():
    """Test creating an input_required status response."""
    response = FinalResponseSchema(
        task_state=TaskState.input_required,
        message="Which file should I update? I found multiple configuration files.",
        reasoning="Cannot determine which file to modify without user clarification",
    )

    assert response.task_state == TaskState.input_required
    assert response.message == "Which file should I update? I found multiple configuration files."
    assert response.reasoning == "Cannot determine which file to modify without user clarification"


def test_final_response_schema_failed():
    """Test creating a failed status response."""
    response = FinalResponseSchema(
        task_state=TaskState.failed,
        message="Unable to complete the task",
        reasoning="Sub-agent returned unrecoverable error",
    )

    assert response.task_state == TaskState.failed
    assert response.message == "Unable to complete the task"
    assert response.reasoning == "Sub-agent returned unrecoverable error"
    assert response.todo_summary is None


def test_final_response_schema_validation():
    """Test that schema validation works correctly."""
    # Test each valid state individually
    response1 = FinalResponseSchema(task_state=TaskState.completed, message="Test message", reasoning="Test reasoning")
    assert response1.task_state == TaskState.completed

    response2 = FinalResponseSchema(task_state=TaskState.working, message="Test message", reasoning="Test reasoning")
    assert response2.task_state == TaskState.working

    response3 = FinalResponseSchema(
        task_state=TaskState.input_required, message="Test message", reasoning="Test reasoning"
    )
    assert response3.task_state == TaskState.input_required

    response4 = FinalResponseSchema(task_state=TaskState.failed, message="Test message", reasoning="Test reasoning")
    assert response4.task_state == TaskState.failed

    # Invalid state should fail validation
    with pytest.raises(Exception):  # Pydantic will raise validation error
        FinalResponseSchema(
            task_state="invalid_state",  # type: ignore
            message="Test",
            reasoning="Test",
        )


def test_final_response_schema_optional_fields():
    """Test that optional fields work correctly."""
    # Minimal response (no optional fields)
    response = FinalResponseSchema(task_state=TaskState.completed, message="Done", reasoning="Task complete")

    assert response.todo_summary is None

    # Response with optional todo_summary
    response_with_summary = FinalResponseSchema(
        task_state=TaskState.working, message="In progress", reasoning="Working on it", todo_summary="1/3 done"
    )

    assert response_with_summary.todo_summary == "1/3 done"
