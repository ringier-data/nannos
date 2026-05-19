"""Middleware that syncs skill files into the sandbox before agent execution.

Provider-agnostic: works with any SandboxBackendProtocol instance.
Skips re-upload on warm sandbox reuse when the skills hash hasn't changed.

Implements the AgentMiddleware protocol so it can be added to the standard
middleware stack. The sync runs in ``abefore_agent`` — once per graph invocation.

Upload strategy:
    Files are uploaded to {sandbox_home}/skills/ (user-writable). The agent's
    read_file/ls tool calls for /skills/ are routed through CompositeBackend to
    the virtual SkillsStoreBackend, so no symlink is needed. The sandbox upload
    is only for execute() commands that need file access.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from base64 import b64decode
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware

from agent_common.backends.skills_store import SkillsStoreBackend

if TYPE_CHECKING:
    from deepagents.backends.protocol import SandboxBackendProtocol

logger = logging.getLogger(__name__)

# Retry configuration for skill uploads.
# Sandbox containers can take 15-20s to become ready after provisioning.
# Total retry window: 1+2+4+8+15 = ~30s of sleep between 6 attempts.
_MAX_RETRIES = 5
_INITIAL_DELAY = 1.0
_BACKOFF_FACTOR = 2.0
_MAX_DELAY = 15.0


class SkillSandboxSyncMiddleware(AgentMiddleware):
    """Upload /skills/** into the sandbox before each agent run.

    Reads all skill files from the SkillsStoreBackend and uploads them
    to the sandbox via aupload_files(). Hash-caches the upload so warm
    sandbox reuse skips the transfer when skills haven't changed.

    The agent's read_file/ls calls for /skills/ go through CompositeBackend's
    virtual routing (SkillsStoreBackend). The sandbox upload makes files
    available to execute() commands at ``{sandbox_home}/skills/``.

    This is a proper AgentMiddleware — add it to the middleware stack and
    it will run automatically via ``abefore_agent``.
    """

    tools = ()  # No additional tools

    def __init__(
        self,
        sandbox_backend: "SandboxBackendProtocol",
        skills_backend: SkillsStoreBackend,
        skills_hash_ref: dict[str, str | None],
        *,
        sandbox_home: str | None = None,
    ) -> None:
        self._sandbox = sandbox_backend
        self._skills_backend = skills_backend
        self._hash_ref = skills_hash_ref
        self._skills_upload_dir = f"{sandbox_home}/skills" if sandbox_home else "/skills"

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Sync skill files to sandbox. Skip if hash matches (warm reuse)."""
        paths = await self._skills_backend.alist_recursive("/skills/")
        if not paths:
            return None

        files: list[tuple[str, bytes]] = []
        for path in paths:
            result = await self._skills_backend.aread(path)
            if result.file_data:
                content = result.file_data["content"]
                encoding = result.file_data.get("encoding", "utf-8")
                # Decode base64 binary files, encode text as UTF-8
                if encoding == "base64":
                    raw = b64decode(content)
                else:
                    raw = content.encode() if isinstance(content, str) else content
                # Remap /skills/... → upload dir
                sandbox_path = self._skills_upload_dir + path[len("/skills") :]
                files.append((sandbox_path, raw))

        if not files:
            return None

        # Compute content hash
        h = hashlib.sha256()
        for path, content in sorted(files):
            h.update(path.encode())
            h.update(content)
        new_hash = h.hexdigest()

        # Skip upload if sandbox already has these exact files
        if self._hash_ref.get("hash") == new_hash:
            logger.debug("Skills hash unchanged, skipping sandbox upload")
            return None

        logger.info("Uploading %d skill files to sandbox", len(files))
        success = await self._upload_with_retry(files)
        if success:
            self._hash_ref["hash"] = new_hash
        return None

    async def _upload_with_retry(self, files: list[tuple[str, bytes]]) -> bool:
        """Upload files to sandbox with exponential backoff retry.

        Retries on any exception (sandbox not ready, network errors, etc.)
        and also on upload responses that indicate failures.

        Returns:
            True if upload succeeded, False if all retries exhausted.
        """
        delay = _INITIAL_DELAY
        last_error: str | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                responses = await self._sandbox.aupload_files(files)
                # Check if any uploads failed (e.g. permission_denied when not ready)
                failed = [r for r in responses if getattr(r, "error", None)]
                if not failed:
                    if attempt > 0:
                        logger.info("Skill upload succeeded on attempt %d", attempt + 1)
                    return True
                # Some files failed — treat as retryable
                last_error = f"{len(failed)}/{len(files)} files failed: {failed[0].error}"
            except Exception as e:
                last_error = str(e)

            if attempt < _MAX_RETRIES:
                logger.warning(
                    "Skill upload attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * _BACKOFF_FACTOR, _MAX_DELAY)

        logger.error("Skill upload failed after %d attempts: %s", _MAX_RETRIES + 1, last_error)
        return False
