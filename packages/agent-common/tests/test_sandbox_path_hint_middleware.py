"""Tests for SandboxPathHintMiddleware."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage

from agent_common.middleware.sandbox_path_hint import (
    SandboxPathHintMiddleware,
    _find_virtual_paths,
)


class FakeToolCallRequest:
    """Minimal mock for ToolCallRequest."""

    def __init__(self, tool_name: str, args: dict):
        self.tool_call = {"name": tool_name, "args": args}


# --- Path detection ---


def test_find_virtual_paths_in_command():
    paths = _find_virtual_paths("python /memories/script.py --output /tmp/out")
    assert paths == ["/memories/script.py"]


def test_find_multiple_virtual_paths():
    paths = _find_virtual_paths("cp /memories/a.py /skills/b.py")
    assert "/memories/a.py" in paths
    assert "/skills/b.py" in paths


def test_find_virtual_paths_in_error_output():
    error = "FileNotFoundError: [Errno 2] No such file or directory: '/memories/data.csv'"
    paths = _find_virtual_paths(error)
    assert paths == ["/memories/data.csv"]


def test_find_virtual_paths_strips_trailing_colons():
    """Regression: shell errors like 'cat: /memories/foo.txt: No such file' must not
    include the trailing colon in the extracted path."""
    error = "cat: /memories/password.txt: No such file or directory"
    paths = _find_virtual_paths(error)
    assert paths == ["/memories/password.txt"]


def test_find_no_virtual_paths():
    paths = _find_virtual_paths("python /home/ubuntu/script.py")
    assert paths == []


def test_find_virtual_paths_deduplicates():
    paths = _find_virtual_paths("cat /memories/foo /memories/foo")
    assert len(paths) == 1


def test_find_all_route_types():
    text = "/memories/a /skills/b /channel_memories/c /group_memories/d /large_tool_results/e"
    paths = _find_virtual_paths(text)
    assert len(paths) == 5


# --- Middleware behavior ---


@pytest.mark.asyncio
async def test_passthrough_non_execute_tools():
    """Non-execute tools should pass through unmodified."""
    mw = SandboxPathHintMiddleware(sandbox_home="/home/ubuntu")
    request = FakeToolCallRequest("read_file", {"file_path": "/memories/foo.py"})
    expected = ToolMessage(content="file content", tool_call_id="tc1", name="read_file")
    handler = AsyncMock(return_value=expected)

    result = await mw.awrap_tool_call(request, handler)

    assert result is expected
    handler.assert_called_once_with(request)


@pytest.mark.asyncio
async def test_error_with_virtual_path_in_output():
    """Failed execute with virtual path in error → append remediation hint."""
    mw = SandboxPathHintMiddleware(sandbox_home="/home/ubuntu")
    request = FakeToolCallRequest("execute", {"command": "python script.py"})
    error_result = ToolMessage(
        content="FileNotFoundError: No such file or directory: '/memories/data.csv'",
        tool_call_id="tc1",
        name="execute",
    )
    handler = AsyncMock(return_value=error_result)

    result = await mw.awrap_tool_call(request, handler)

    assert "copy_to_sandbox" in result.content
    assert "/memories/data.csv" in result.content
    assert "virtual filesystem" in result.content


@pytest.mark.asyncio
async def test_success_with_virtual_path_in_command():
    """Successful execute with virtual path in command → append warning."""
    mw = SandboxPathHintMiddleware(sandbox_home="/home/ubuntu")
    request = FakeToolCallRequest("execute", {"command": "python /skills/analyze.py"})
    success_result = ToolMessage(
        content="Analysis complete. Results saved.",
        tool_call_id="tc1",
        name="execute",
    )
    handler = AsyncMock(return_value=success_result)

    result = await mw.awrap_tool_call(request, handler)

    assert "⚠️" in result.content
    assert "/skills/analyze.py" in result.content
    assert "pre-synced" in result.content


@pytest.mark.asyncio
async def test_error_with_virtual_path_in_command():
    """Failed execute with virtual path in command → append remediation hint."""
    mw = SandboxPathHintMiddleware(sandbox_home="/home/ubuntu")
    request = FakeToolCallRequest("execute", {"command": "python /memories/script.py"})
    error_result = ToolMessage(
        content="bash: /memories/script.py: No such file or directory",
        tool_call_id="tc1",
        name="execute",
    )
    handler = AsyncMock(return_value=error_result)

    result = await mw.awrap_tool_call(request, handler)

    assert "copy_to_sandbox" in result.content
    assert "virtual filesystem" in result.content


@pytest.mark.asyncio
async def test_no_modification_when_clean():
    """Execute with no virtual paths → no modification."""
    mw = SandboxPathHintMiddleware(sandbox_home="/home/ubuntu")
    request = FakeToolCallRequest("execute", {"command": "python /home/ubuntu/script.py"})
    clean_result = ToolMessage(
        content="Script executed successfully.",
        tool_call_id="tc1",
        name="execute",
    )
    handler = AsyncMock(return_value=clean_result)

    result = await mw.awrap_tool_call(request, handler)

    assert result is clean_result  # Unchanged, same object


@pytest.mark.asyncio
async def test_hint_includes_sandbox_home():
    """Remediation hints should reference the actual sandbox home."""
    mw = SandboxPathHintMiddleware(sandbox_home="/home/custom")
    request = FakeToolCallRequest("execute", {"command": "cat /memories/foo"})
    error_result = ToolMessage(
        content="cat: /memories/foo: No such file or directory",
        tool_call_id="tc1",
        name="execute",
    )
    handler = AsyncMock(return_value=error_result)

    result = await mw.awrap_tool_call(request, handler)

    assert "/home/custom/" in result.content
