"""Tests for the copy_file tool."""

import pytest
from unittest.mock import AsyncMock, Mock

from deepagents.backends.protocol import WriteResult

from agent_common.core.copy_file_tool import create_copy_file_tool


@pytest.mark.asyncio
async def test_copy_file_success():
    """Test successful file copy to memories."""
    # Mock backend
    mock_backend = AsyncMock()
    mock_backend.aread.return_value = "Test content for large file"
    mock_backend.awrite.return_value = WriteResult(path="/memories/test.txt", error=None, files_update=None)

    # Mock backend factory
    def backend_factory(runtime):
        return mock_backend

    # Create tool
    tool = create_copy_file_tool(backend_factory)

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
    mock_backend.aread.assert_called_once_with("/temp/test.txt")
    mock_backend.awrite.assert_called_once_with("/memories/test.txt", "Test content for large file")


@pytest.mark.asyncio
async def test_copy_file_with_auto_destination():
    """Test copy with automatic destination name from source basename."""
    # Mock backend
    mock_backend = AsyncMock()
    mock_backend.aread.return_value = "Content"
    mock_backend.awrite.return_value = WriteResult(
        path="/memories/large_result.json", error=None, files_update=None
    )

    def backend_factory(runtime):
        return mock_backend

    tool = create_copy_file_tool(backend_factory)

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

    def backend_factory(runtime):
        return mock_backend

    tool = create_copy_file_tool(backend_factory)

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
    mock_backend.aread.return_value = "Content"
    mock_backend.awrite.return_value = WriteResult(path=None, error="Write failed: disk full", files_update=None)

    def backend_factory(runtime):
        return mock_backend

    tool = create_copy_file_tool(backend_factory)

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

    def backend_factory(runtime):
        return mock_backend

    tool = create_copy_file_tool(backend_factory)

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
    mock_backend.aread.return_value = ""  # Empty content

    def backend_factory(runtime):
        return mock_backend

    tool = create_copy_file_tool(backend_factory)

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
        mock_backend.aread.return_value = content
        mock_backend.awrite.return_value = WriteResult(path="/memories/test.txt", error=None, files_update=None)

        def backend_factory(runtime):
            return mock_backend

        tool = create_copy_file_tool(backend_factory)

        result = await tool.coroutine(
            source_path="/temp/test.txt",
            destination_name="test.txt",
            runtime=Mock(),
        )

        assert expected_unit in result
        assert "Size:" in result
