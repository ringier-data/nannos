"""Tests for copy_to_sandbox tool."""

from dataclasses import dataclass

import pytest

from agent_common.core.sandbox_tools import (
    _MAX_FILE_SIZE,
    _validate_virtual_path,
    create_copy_to_sandbox_tool,
)


@dataclass
class FakeReadResult:
    file_data: dict | None = None
    error: str | None = None


@dataclass
class FakeUploadResponse:
    path: str
    error: str | None = None


class MockCompositeBackend:
    """Mock backend that simulates CompositeBackend routing."""

    def __init__(self, files: dict[str, str] | None = None):
        self._files = files or {}

    async def aread(self, path: str, **kwargs):
        if path in self._files:
            return FakeReadResult(file_data={"content": self._files[path]})
        return FakeReadResult(file_data=None, error=f"File not found: {path}")


class MockSandboxBackend:
    """Mock sandbox backend that records uploads."""

    def __init__(self, fail_uploads: bool = False):
        self.uploaded_files: list[tuple[str, bytes]] = []
        self._fail_uploads = fail_uploads

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
        if self._fail_uploads:
            return [FakeUploadResponse(path=p, error="permission_denied") for p, _ in files]
        self.uploaded_files.extend(files)
        return [FakeUploadResponse(path=p) for p, _ in files]


# --- Path validation ---


def test_validate_rejects_relative_path():
    with pytest.raises(ValueError, match="must be absolute"):
        _validate_virtual_path("memories/foo.py")


def test_validate_rejects_path_traversal():
    with pytest.raises(ValueError, match="traversal"):
        _validate_virtual_path("/memories/../etc/passwd")


def test_validate_rejects_unknown_route():
    with pytest.raises(ValueError, match="not on a supported virtual route"):
        _validate_virtual_path("/tmp/foo.py")


def test_validate_accepts_valid_routes():
    assert _validate_virtual_path("/memories/foo.py") == "/memories/foo.py"
    assert _validate_virtual_path("/skills/analyze/main.py") == "/skills/analyze/main.py"
    assert _validate_virtual_path("/channel_memories/notes.md") == "/channel_memories/notes.md"
    assert _validate_virtual_path("/group_memories/playbook.md") == "/group_memories/playbook.md"
    assert _validate_virtual_path("/large_tool_results/data.json") == "/large_tool_results/data.json"


# --- Tool execution ---


@pytest.mark.asyncio
async def test_copy_to_sandbox_success():
    """Should read from virtual FS, upload to sandbox, return sandbox path."""
    backend = MockCompositeBackend({"/memories/script.py": "print('hello')"})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/memories/script.py"})

    assert "/home/ubuntu/memories/script.py" in result
    assert "File copied to sandbox" in result
    assert len(sandbox.uploaded_files) == 1
    path, content = sandbox.uploaded_files[0]
    assert path == "/home/ubuntu/memories/script.py"
    assert content == b"print('hello')"


@pytest.mark.asyncio
async def test_copy_to_sandbox_skills_route():
    """Should handle /skills/ route correctly."""
    backend = MockCompositeBackend({"/skills/analyze/main.py": "import pandas"})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/skills/analyze/main.py"})

    assert "/home/ubuntu/skills/analyze/main.py" in result
    assert len(sandbox.uploaded_files) == 1


@pytest.mark.asyncio
async def test_copy_to_sandbox_file_not_found():
    """Should return error when file doesn't exist."""
    backend = MockCompositeBackend({})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/memories/nonexistent.py"})

    assert "Error" in result
    assert "not found" in result
    assert len(sandbox.uploaded_files) == 0


@pytest.mark.asyncio
async def test_copy_to_sandbox_invalid_route():
    """Should reject paths not on a known virtual route."""
    backend = MockCompositeBackend({})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/tmp/foo.py"})

    assert "Error" in result
    assert "not on a supported virtual route" in result
    assert len(sandbox.uploaded_files) == 0


@pytest.mark.asyncio
async def test_copy_to_sandbox_size_guard():
    """Should reject files larger than 10MB."""
    large_content = "x" * (_MAX_FILE_SIZE + 1)
    backend = MockCompositeBackend({"/memories/huge.bin": large_content})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/memories/huge.bin"})

    assert "Error" in result
    assert "10 MB limit" in result
    assert len(sandbox.uploaded_files) == 0


@pytest.mark.asyncio
async def test_copy_to_sandbox_upload_failure():
    """Should return error when sandbox upload fails."""
    backend = MockCompositeBackend({"/memories/script.py": "print('hello')"})
    sandbox = MockSandboxBackend(fail_uploads=True)
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/memories/script.py"})

    assert "Error" in result
    assert "Failed to upload" in result


@pytest.mark.asyncio
async def test_copy_to_sandbox_always_copies():
    """Should re-upload even if called twice (no idempotency check)."""
    backend = MockCompositeBackend({"/memories/script.py": "print('hello')"})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    await tool.ainvoke({"virtual_path": "/memories/script.py"})
    await tool.ainvoke({"virtual_path": "/memories/script.py"})

    assert len(sandbox.uploaded_files) == 2


@pytest.mark.asyncio
async def test_copy_to_sandbox_path_traversal():
    """Should reject path traversal attempts."""
    backend = MockCompositeBackend({})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/memories/../etc/passwd"})

    assert "Error" in result
    assert "traversal" in result


@pytest.mark.asyncio
async def test_copy_to_sandbox_result_contains_usage_hint():
    """Return value should include execute() usage example."""
    backend = MockCompositeBackend({"/memories/script.py": "print('hello')"})
    sandbox = MockSandboxBackend()
    tool = create_copy_to_sandbox_tool(backend, sandbox, "/home/ubuntu")

    result = await tool.ainvoke({"virtual_path": "/memories/script.py"})

    assert "execute()" in result
    assert "working copy" in result
    assert "write_file()" in result
