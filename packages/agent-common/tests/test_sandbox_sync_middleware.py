"""Tests for SkillSandboxSyncMiddleware."""

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from agent_common.backends.skills_store import SkillsStoreBackend
from agent_common.middleware.skill_sandbox_sync import SkillSandboxSyncMiddleware
from agent_common.models.skill import ResolvedSkill, SkillFile


@dataclass
class FakeUploadResponse:
    path: str
    error: str | None = None


@dataclass
class FakeExecuteResponse:
    output: str = ""
    exit_code: int | None = 0


class MockSandboxHandle:
    def __init__(self):
        self.uploaded_files: list[tuple[str, bytes]] = []
        self.executed_commands: list[str] = []

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
        self.uploaded_files.extend(files)
        return [FakeUploadResponse(path=p) for p, _ in files]

    async def aexecute(self, command: str, **kwargs) -> FakeExecuteResponse:
        self.executed_commands.append(command)
        return FakeExecuteResponse()


def _make_backend():
    skills = {
        "my-skill": ResolvedSkill(
            name="my-skill",
            description="Test skill.",
            body="# Instructions\nDo things.",
            scope="standard",
            files=[SkillFile(path="scripts/run.py", content="print('hello')")],
        ),
    }
    return SkillsStoreBackend(skills)


@pytest.mark.asyncio
async def test_uploads_files_on_first_call():
    """First call should upload all skill files to sandbox_home/skills/."""
    handle = MockSandboxHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    await middleware.abefore_agent({}, None)

    # Should have uploaded SKILL.md + scripts/run.py under sandbox_home
    assert len(handle.uploaded_files) == 2
    paths = [f[0] for f in handle.uploaded_files]
    assert "/home/ubuntu/skills/my-skill/SKILL.md" in paths
    assert "/home/ubuntu/skills/my-skill/scripts/run.py" in paths
    # Hash should be set
    assert hash_ref["hash"] is not None


@pytest.mark.asyncio
async def test_skips_upload_on_same_hash():
    """Second call with same skills should skip upload."""
    handle = MockSandboxHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    await middleware.abefore_agent({}, None)
    first_count = len(handle.uploaded_files)

    # Second call — should skip
    await middleware.abefore_agent({}, None)
    assert len(handle.uploaded_files) == first_count  # no new uploads


@pytest.mark.asyncio
async def test_reuploads_on_hash_change():
    """If hash_ref is cleared, should re-upload."""
    handle = MockSandboxHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    await middleware.abefore_agent({}, None)
    first_count = len(handle.uploaded_files)

    # Clear hash to force re-upload
    hash_ref["hash"] = None
    await middleware.abefore_agent({}, None)
    assert len(handle.uploaded_files) == first_count * 2  # uploaded again


@pytest.mark.asyncio
async def test_empty_backend_no_upload():
    """Empty backend should not trigger upload."""
    handle = MockSandboxHandle()
    backend = SkillsStoreBackend({})
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    await middleware.abefore_agent({}, None)
    assert len(handle.uploaded_files) == 0
    assert hash_ref["hash"] is None


@pytest.mark.asyncio
async def test_retries_on_upload_exception():
    """Should retry when aupload_files raises an exception."""
    call_count = 0

    class FailThenSucceedHandle:
        def __init__(self):
            self.uploaded_files: list[tuple[str, bytes]] = []

        async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("Sandbox is not ready")
            self.uploaded_files.extend(files)
            return [FakeUploadResponse(path=p) for p, _ in files]

    handle = FailThenSucceedHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    with patch("agent_common.middleware.skill_sandbox_sync.asyncio.sleep"):
        await middleware.abefore_agent({}, None)

    assert call_count == 3
    assert len(handle.uploaded_files) == 2
    assert hash_ref["hash"] is not None


@pytest.mark.asyncio
async def test_retries_on_upload_response_errors():
    """Should retry when aupload_files returns responses with errors."""
    call_count = 0

    class ErrorResponseHandle:
        def __init__(self):
            self.uploaded_files: list[tuple[str, bytes]] = []

        async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return [FakeUploadResponse(path=p, error="permission_denied") for p, _ in files]
            self.uploaded_files.extend(files)
            return [FakeUploadResponse(path=p) for p, _ in files]

    handle = ErrorResponseHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    with patch("agent_common.middleware.skill_sandbox_sync.asyncio.sleep"):
        await middleware.abefore_agent({}, None)

    assert call_count == 2
    assert len(handle.uploaded_files) == 2
    assert hash_ref["hash"] is not None


@pytest.mark.asyncio
async def test_gives_up_after_max_retries():
    """Should give up and log error after max retries exhausted."""

    class AlwaysFailHandle:
        async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
            raise RuntimeError("Sandbox is not ready")

    handle = AlwaysFailHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    with patch("agent_common.middleware.skill_sandbox_sync.asyncio.sleep"):
        # Should not raise — logs error instead
        await middleware.abefore_agent({}, None)

    # Hash should NOT be set on failure
    assert hash_ref["hash"] is None


@pytest.mark.asyncio
async def test_custom_sandbox_home_remaps_paths():
    """With custom sandbox_home, paths should be remapped."""
    handle = MockSandboxHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
        sandbox_home="/home/ubuntu",
    )

    await middleware.abefore_agent({}, None)

    # Paths should be remapped to /home/ubuntu/skills/...
    paths = [f[0] for f in handle.uploaded_files]
    assert "/home/ubuntu/skills/my-skill/SKILL.md" in paths
    assert "/home/ubuntu/skills/my-skill/scripts/run.py" in paths


@pytest.mark.asyncio
async def test_default_sandbox_home_uploads_to_skills():
    """With default sandbox_home (None), uploads go to /skills/ directly."""
    handle = MockSandboxHandle()
    backend = _make_backend()
    hash_ref: dict[str, str | None] = {"hash": None}

    middleware = SkillSandboxSyncMiddleware(
        sandbox_backend=handle,
        skills_backend=backend,
        skills_hash_ref=hash_ref,
    )

    await middleware.abefore_agent({}, None)

    # Paths remain at /skills/...
    paths = [f[0] for f in handle.uploaded_files]
    assert "/skills/my-skill/SKILL.md" in paths
    assert "/skills/my-skill/scripts/run.py" in paths
