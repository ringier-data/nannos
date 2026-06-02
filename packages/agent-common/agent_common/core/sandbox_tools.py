"""Copy-to-sandbox tool for materializing virtual filesystem files in the sandbox.

Agents with sandbox access have two filesystems:
- Virtual FS (CompositeBackend routes: /memories/, /skills/, etc.) — accessed via read_file/write_file
- Sandbox FS (real container filesystem) — accessed via execute()

This tool bridges them: it reads a file from the virtual FS and uploads it to the
sandbox, returning the sandbox-real path for use in execute() commands.

Design decisions:
- Always copies (no idempotency check) — simple and correct
- Whitelisted routes only — prevents copying ephemeral StateBackend files
- 10MB size guard — prevents sandbox storage/upload issues
- Per-invocation lifecycle — created after sandbox acquisition, never visible to non-sandbox agents
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol

logger = logging.getLogger(__name__)

# Virtual routes that can be copied to the sandbox.
_ALLOWED_ROUTES = (
    "/memories/",
    "/skills/",
    "/attachments/",
    "/channel_memories/",
    "/group_memories/",
    "/large_tool_results/",
)

# Maximum file size for sandbox copy (10MB).
_MAX_FILE_SIZE = 10 * 1024 * 1024


class CopyToSandboxInput(BaseModel):
    """Input schema for copy_to_sandbox tool."""

    virtual_path: str = Field(
        ...,
        description=(
            "Path on the virtual filesystem to copy to the sandbox. "
            "Must start with a known route: /memories/, /skills/, /attachments/, "
            "/channel_memories/, /group_memories/, or /large_tool_results/. "
            "Example: '/memories/script.py', '/skills/analyze/main.py', "
            "or '/attachments/report.pdf'"
        ),
    )


def _validate_virtual_path(path: str) -> str:
    """Validate that a virtual path is safe and on an allowed route."""
    if not path.startswith("/"):
        raise ValueError(f"Path must be absolute (start with /): {path}")
    if ".." in path:
        raise ValueError(f"Path traversal not allowed: {path}")
    if not any(path.startswith(route) for route in _ALLOWED_ROUTES):
        allowed = ", ".join(_ALLOWED_ROUTES)
        raise ValueError(
            f"Path '{path}' is not on a supported virtual route. "
            f"Only files from these persistent paths can be copied: {allowed}"
        )
    return path


def create_copy_to_sandbox_tool(
    composite_backend: "BackendProtocol",
    sandbox_backend: "SandboxBackendProtocol",
    sandbox_home: str,
) -> BaseTool:
    """Create a tool that copies files from the virtual filesystem to the sandbox.

    Args:
        composite_backend: The CompositeBackend that routes virtual paths to
            their respective backends (IndexingStoreBackend, SkillsStoreBackend, etc.)
        sandbox_backend: The sandbox backend for uploading files via aupload_files()
        sandbox_home: The sandbox home directory (e.g., "/home/ubuntu")

    Returns:
        BaseTool that accepts a virtual path and returns the sandbox-real path
    """

    async def copy_to_sandbox_handler(virtual_path: str) -> str:
        """Copy a file from the virtual filesystem to the sandbox for use in execute().

        Files on virtual paths (/memories/, /skills/, etc.) are not directly accessible
        in the sandbox. This tool reads the file from the virtual filesystem and uploads
        it to the sandbox, returning the real sandbox path you can use in execute() commands.

        Args:
            virtual_path: The virtual filesystem path (e.g., '/memories/script.py')

        Returns:
            The sandbox-real path to use in execute(), or an error message
        """
        try:
            virtual_path = _validate_virtual_path(virtual_path)
        except ValueError as e:
            return f"Error: {e}"

        # Read from virtual FS via CompositeBackend routing
        try:
            result = await composite_backend.aread(virtual_path)
        except Exception as e:
            logger.error("Failed to read '%s' from virtual FS: %s", virtual_path, e)
            return (
                f"Error: Could not read '{virtual_path}' from the virtual filesystem. "
                f"Make sure the file exists (use ls or read_file to verify). Details: {e}"
            )

        if not result or not getattr(result, "file_data", None):
            return (
                f"Error: File '{virtual_path}' not found on the virtual filesystem. "
                f"Use ls to check available files in that directory."
            )

        content = result.file_data.get("content", "")
        if not content:
            return f"Error: File '{virtual_path}' is empty."

        # Decode to raw bytes for upload. Binary files (e.g. PDFs, images served
        # from /attachments/) are returned base64-encoded; text is utf-8.
        encoding = result.file_data.get("encoding", "utf-8")
        if encoding == "base64" and isinstance(content, str):
            raw = base64.b64decode(content)
        else:
            raw = content.encode("utf-8") if isinstance(content, str) else content

        # Size guard
        if len(raw) > _MAX_FILE_SIZE:
            size_mb = len(raw) / (1024 * 1024)
            return (
                f"Error: File '{virtual_path}' is {size_mb:.1f} MB, which exceeds the "
                f"10 MB limit for sandbox copy. Consider processing it in chunks using "
                f"read_file instead, or filtering the data before copying."
            )

        # Compute sandbox-real path: /memories/foo.py → /home/ubuntu/memories/foo.py
        sandbox_path = f"{sandbox_home}/{virtual_path.lstrip('/')}"

        # Upload to sandbox
        try:
            responses = await sandbox_backend.aupload_files([(sandbox_path, raw)])
            failed = [r for r in responses if getattr(r, "error", None)]
            if failed:
                error_msg = failed[0].error
                logger.error("Failed to upload '%s' to sandbox: %s", sandbox_path, error_msg)
                return f"Error: Failed to upload file to sandbox at '{sandbox_path}'. Details: {error_msg}"
        except Exception as e:
            logger.error("Failed to upload '%s' to sandbox: %s", sandbox_path, e)
            return f"Error: Failed to upload file to sandbox. Details: {e}"

        # Format size for feedback
        if len(raw) < 1024:
            size_str = f"{len(raw)} bytes"
        elif len(raw) < 1024 * 1024:
            size_str = f"{len(raw) / 1024:.1f} KB"
        else:
            size_str = f"{len(raw) / (1024 * 1024):.1f} MB"

        logger.info("Copied '%s' → '%s' (%s)", virtual_path, sandbox_path, size_str)

        return (
            f"File copied to sandbox.\n\n"
            f"Sandbox path: {sandbox_path}\n"
            f"Size: {size_str}\n\n"
            f"Use this path in execute() commands, e.g.:\n"
            f'  execute("python {sandbox_path}")\n\n'
            f"Note: This is a working copy. Edits made via execute() are NOT "
            f"saved back to the virtual filesystem. To persist changes, use write_file()."
        )

    return StructuredTool.from_function(
        coroutine=copy_to_sandbox_handler,
        name="copy_to_sandbox",
        description=(
            "Copy a file from the virtual filesystem (/memories/, /skills/, /attachments/, etc.) to "
            "the sandbox for use in execute() commands. Virtual filesystem files — including files "
            "the user attached to the conversation (under /attachments/) — are NOT directly accessible "
            "in the sandbox; you must copy them first. Returns the sandbox-real path to use in "
            "execute(). Example: copy_to_sandbox('/attachments/report.pdf') → '/home/ubuntu/attachments/report.pdf'"
        ),
        args_schema=CopyToSandboxInput,
    )
