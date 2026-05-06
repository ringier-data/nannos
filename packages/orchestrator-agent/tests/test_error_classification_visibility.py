"""Tests for error_classification visibility in ToolMessage content.

Verifies that ErrorClassificationMiddleware prepends the classification
to ToolMessage.content so that the model (which strips additional_kwargs)
can see and act on the error type.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage

from app.middleware.error_classification_middleware import ErrorClassificationMiddleware


@pytest.fixture
def middleware():
    return ErrorClassificationMiddleware()


def _make_request(tool_name: str = "some_tool"):
    req = MagicMock()
    req.tool_call = {"name": tool_name, "args": {}}
    return req


@pytest.mark.asyncio
async def test_classification_prepended_to_content(middleware):
    """Error classification is prepended to content for model visibility."""
    request = _make_request()
    msg = ToolMessage(
        content="Error: Connection timed out after 30s",
        tool_call_id="tc-1",
        status="error",
    )
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)

    assert result.content.startswith("[ERROR_TYPE: transient]")
    assert "Connection timed out after 30s" in result.content


@pytest.mark.asyncio
async def test_system_error_prepended_to_content(middleware):
    """system_error classification is visible in content."""
    request = _make_request()
    msg = ToolMessage(
        content="Traceback (most recent call last):\nRuntimeError: kaboom",
        tool_call_id="tc-1",
        status="error",
    )
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)

    assert result.content.startswith("[ERROR_TYPE: system_error]")
    assert "kaboom" in result.content


@pytest.mark.asyncio
async def test_non_error_content_unchanged(middleware):
    """Non-error responses have their content unchanged."""
    request = _make_request()
    original_content = "Success: operation completed"
    msg = ToolMessage(content=original_content, tool_call_id="tc-1")
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)

    assert result.content == original_content


@pytest.mark.asyncio
async def test_metadata_and_content_both_set(middleware):
    """Both additional_kwargs and content are set for classified errors."""
    request = _make_request()
    msg = ToolMessage(
        content="Error: missing required field: email",
        tool_call_id="tc-1",
        status="error",
    )
    handler = AsyncMock(return_value=msg)

    result = await middleware.awrap_tool_call(request, handler)

    # Metadata is set (for programmatic use)
    assert result.additional_kwargs["error_classification"] == "user_fixable"
    # Content is prefixed (for model visibility)
    assert "[ERROR_TYPE: user_fixable]" in result.content
