"""Tests for the copy_file tool."""

import pytest
from unittest.mock import AsyncMock, Mock

from deepagents.backends.protocol import ReadResult, WriteResult
from deepagents.backends.utils import create_file_data

from agent_common.core.copy_file_tool import create_copy_file_tool


def _read(content: str) -> ReadResult:
    """What a backend's aread actually returns: a ReadResult, not the text."""
    return ReadResult(file_data=create_file_data(content))


@pytest.mark.asyncio
async def test_copy_file_success():
    """Test successful file copy to memories."""
    # Mock backend
    mock_backend = AsyncMock()
    mock_backend.aread.return_value = _read("Test content for large file")
    mock_backend.awrite.return_value = WriteResult(path="/memories/test.txt", error=None)

    tool = create_copy_file_tool(mock_backend)

    # Invoke tool directly with function parameters (bypassing LangChain's parameter injection)
    result = await tool.coroutine(
        source_path="/temp/test.txt",
        destination_name="test.txt",
        runtime=Mock(),  # Mock runtime
    )

    # Verify result
    assert "Successfully copied" in result
    assert "/memories/test.txt" in result
    assert "indexed for semantic search" in result

    # Verify backend calls
    mock_backend.aread.assert_called_once_with("/temp/test.txt", offset=0)
    mock_backend.awrite.assert_called_once_with("/memories/test.txt", "Test content for large file")


@pytest.mark.asyncio
async def test_copy_file_with_auto_destination():
    """Test copy with automatic destination name from source basename."""
    # Mock backend
    mock_backend = AsyncMock()
    mock_backend.aread.return_value = _read("Content")
    mock_backend.awrite.return_value = WriteResult(
        path="/memories/large_result.json", error=None
    )

    tool = create_copy_file_tool(mock_backend)

    # Invoke without destination_name
    result = await tool.coroutine(
        source_path="/large_tool_results/tool_abc/large_result.json",
        destination_name=None,
        runtime=Mock(),
    )

    assert "Successfully copied" in result
    assert "/memories/large_result.json" in result

    # Verify it used the source basename
    mock_backend.awrite.assert_called_once_with("/memories/large_result.json", "Content")


@pytest.mark.asyncio
async def test_copy_file_source_not_found():
    """Test error handling when source file doesn't exist."""
    mock_backend = AsyncMock()
    mock_backend.aread.side_effect = FileNotFoundError("File not found")

    tool = create_copy_file_tool(mock_backend)

    result = await tool.coroutine(
        source_path="/temp/nonexistent.txt",
        destination_name="test.txt",
        runtime=Mock(),
    )

    assert "Error:" in result
    assert "Could not read source file" in result


@pytest.mark.asyncio
async def test_copy_file_write_error():
    """Test error handling when write operation fails."""
    mock_backend = AsyncMock()
    mock_backend.aread.return_value = _read("Content")
    mock_backend.awrite.return_value = WriteResult(path=None, error="Write failed: disk full")

    tool = create_copy_file_tool(mock_backend)

    result = await tool.coroutine(
        source_path="/temp/test.txt",
        destination_name="test.txt",
        runtime=Mock(),
    )

    assert "Error:" in result
    assert "Could not write" in result
    assert "disk full" in result


@pytest.mark.asyncio
async def test_copy_file_invalid_path():
    """Test validation of invalid paths."""
    mock_backend = AsyncMock()

    tool = create_copy_file_tool(mock_backend)

    # Test relative path (should fail validation)
    result = await tool.coroutine(
        source_path="relative/path.txt",
        destination_name="test.txt",
        runtime=Mock(),
    )

    assert "Error:" in result
    assert "must be absolute" in result

    # Test path traversal (should fail validation)
    result = await tool.coroutine(
        source_path="/temp/../etc/passwd",
        destination_name="test.txt",
        runtime=Mock(),
    )

    assert "Error:" in result
    assert "traversal not allowed" in result


@pytest.mark.asyncio
async def test_copy_file_empty_file():
    """Test handling of empty source files."""
    mock_backend = AsyncMock()
    mock_backend.aread.return_value = _read("")  # Empty content

    tool = create_copy_file_tool(mock_backend)

    result = await tool.coroutine(
        source_path="/temp/empty.txt",
        destination_name="test.txt",
        runtime=Mock(),
    )

    assert "Error:" in result
    assert "empty or not found" in result


@pytest.mark.asyncio
async def test_copy_file_file_size_display():
    """Test file size display in success message."""
    # Test different file sizes
    test_cases = [
        ("Small content", "bytes"),
        ("A" * 2048, "KB"),  # 2KB
        ("B" * (2 * 1024 * 1024), "MB"),  # 2MB
    ]

    for content, expected_unit in test_cases:
        mock_backend = AsyncMock()
        mock_backend.aread.return_value = _read(content)
        mock_backend.awrite.return_value = WriteResult(path="/memories/test.txt", error=None)

        tool = create_copy_file_tool(mock_backend)

        result = await tool.coroutine(
            source_path="/temp/test.txt",
            destination_name="test.txt",
            runtime=Mock(),
        )

        assert expected_unit in result
        assert "Size:" in result


@pytest.mark.asyncio
async def test_a_file_longer_than_one_read_window_is_copied_whole():
    """aread returns one window of lines; the copy must follow next_offset to the end."""
    mock_backend = AsyncMock()
    mock_backend.aread.side_effect = [
        ReadResult(file_data=create_file_data("line 1\nline 2\n"), total_lines=3, start_line=1, end_line=2, next_offset=2),
        ReadResult(file_data=create_file_data("line 3\n"), total_lines=3, start_line=3, end_line=3),
    ]
    mock_backend.awrite.return_value = WriteResult(path="/memories/big.txt", error=None)

    tool = create_copy_file_tool(mock_backend)
    result = await tool.coroutine(source_path="/temp/big.txt", destination_name="big.txt", runtime=Mock())

    assert "Successfully copied" in result
    mock_backend.awrite.assert_called_once_with("/memories/big.txt", "line 1\nline 2\nline 3\n")


@pytest.mark.asyncio
async def test_a_binary_source_is_refused():
    mock_backend = AsyncMock()
    mock_backend.aread.return_value = ReadResult(file_data=create_file_data("UEsDBA==", encoding="base64"))

    tool = create_copy_file_tool(mock_backend)
    result = await tool.coroutine(source_path="/attachments/deck.pptx", destination_name=None, runtime=Mock())

    assert "binary file" in result
    mock_backend.awrite.assert_not_called()
