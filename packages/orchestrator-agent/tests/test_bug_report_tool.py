"""Tests for report_bug_tool (Phase 2)."""

from unittest.mock import patch

import pytest

from app.core.bug_report_tool import report_bug_tool


@pytest.mark.asyncio
async def test_bug_report_confirmed():
    """When the user confirms, the tool returns a success message."""
    user_response = {"confirmed": True, "description": "The search tool crashed"}

    with patch("app.core.bug_report_tool.interrupt", return_value=user_response) as mock_interrupt:
        result = report_bug_tool.invoke({"reason": "Unrecoverable search failure"})

    mock_interrupt.assert_called_once()
    interrupt_value = mock_interrupt.call_args[1].get("value") or mock_interrupt.call_args[0][0]
    assert interrupt_value["type"] == "bug_report"
    assert interrupt_value["reason"] == "Unrecoverable search failure"
    assert "Bug report submitted" in result
    assert "The search tool crashed" in result


@pytest.mark.asyncio
async def test_bug_report_declined():
    """When the user declines, the tool returns a decline message."""
    user_response = {"confirmed": False}

    with patch("app.core.bug_report_tool.interrupt", return_value=user_response):
        result = report_bug_tool.invoke({"reason": "Something went wrong"})

    assert "declined" in result.lower()


@pytest.mark.asyncio
async def test_bug_report_confirmed_uses_reason_as_fallback():
    """If the user confirms without a description, the reason is used."""
    user_response = {"confirmed": True}

    with patch("app.core.bug_report_tool.interrupt", return_value=user_response):
        result = report_bug_tool.invoke({"reason": "Original reason"})

    assert "Original reason" in result


@pytest.mark.asyncio
async def test_bug_report_non_dict_response():
    """If the resume value is not a dict (edge case), treat as declined."""
    with patch("app.core.bug_report_tool.interrupt", return_value="unexpected string"):
        result = report_bug_tool.invoke({"reason": "Test"})

    assert "declined" in result.lower()


def test_tool_schema():
    """Verify the tool has the expected schema."""
    assert report_bug_tool.name == "report_bug_tool"
    schema = report_bug_tool.args_schema.model_json_schema() if report_bug_tool.args_schema else {}
    assert "reason" in schema.get("properties", {})
