"""Wrapper that detects sandbox-not-ready responses and raises a retryable error.

When the sandbox infrastructure hasn't finished initializing, commands execute
but return non-JSON output like "Sandbox is not ready". Without this wrapper,
that response flows through as a silent error ToolMessage. By detecting it at
the execute() level and raising, we allow ToolRetryMiddleware to automatically
retry the operation with exponential backoff.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deepagents.backends.protocol import SandboxBackendProtocol

if TYPE_CHECKING:
    from deepagents.backends.protocol import ExecuteResponse

logger = logging.getLogger(__name__)

# Patterns in execute output that indicate the sandbox isn't ready yet
_NOT_READY_PATTERNS = (
    "sandbox is not ready",
    "container is not running",
    "container is starting",
)


class SandboxNotReadyError(RuntimeError):
    """Raised when the sandbox hasn't finished initializing.

    This exception is caught by ToolRetryMiddleware and retried with
    exponential backoff.
    """


class ReadySandboxWrapper:
    """Transparent wrapper around a SandboxBackendProtocol that raises on not-ready responses.

    Delegates all attribute access to the wrapped backend. Intercepts execute()
    and aexecute() to check for "not ready" indicators in the output and raise
    SandboxNotReadyError instead of returning a confusing error to the LLM.
    """

    def __init__(self, backend: "SandboxBackendProtocol") -> None:
        self._backend = backend

    def __getattr__(self, name: str):
        """Delegate all attribute access to the wrapped backend."""
        return getattr(self._backend, name)

    @property
    def id(self) -> str:
        """Unique identifier for the sandbox backend instance."""
        return self._backend.id

    def execute(self, command: str, *, timeout: int | None = None) -> "ExecuteResponse":
        """Execute command, raising SandboxNotReadyError if sandbox isn't ready."""
        result = self._backend.execute(command, timeout=timeout)
        self._check_not_ready(result)
        return result

    async def aexecute(self, command: str, *, timeout: int | None = None) -> "ExecuteResponse":
        """Async execute, raising SandboxNotReadyError if sandbox isn't ready."""
        result = await self._backend.aexecute(command, timeout=timeout)
        self._check_not_ready(result)
        return result

    def _check_not_ready(self, result: "ExecuteResponse") -> None:
        """Raise SandboxNotReadyError if the output indicates sandbox isn't ready."""
        if not result.output:
            return
        output_lower = result.output.strip().lower()
        for pattern in _NOT_READY_PATTERNS:
            if pattern in output_lower:
                logger.warning(
                    "Sandbox not ready (output: %s), raising retryable error",
                    result.output.strip()[:100],
                )
                raise SandboxNotReadyError(f"Sandbox is not ready: {result.output.strip()[:200]}")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list:
        """Upload files, raising SandboxNotReadyError on transient failures."""
        try:
            return self._backend.upload_files(files)
        except RuntimeError as e:
            if self._is_not_ready_error(e):
                raise SandboxNotReadyError(str(e)) from e
            raise

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
        """Async upload files, raising SandboxNotReadyError on transient failures."""
        try:
            return await self._backend.aupload_files(files)
        except RuntimeError as e:
            if self._is_not_ready_error(e):
                raise SandboxNotReadyError(str(e)) from e
            raise

    @staticmethod
    def _is_not_ready_error(exc: Exception) -> bool:
        """Check if an exception indicates the sandbox isn't ready."""
        msg = str(exc).lower()
        return any(p in msg for p in _NOT_READY_PATTERNS)


# Register as a virtual subclass so isinstance() checks in FilesystemMiddleware
# (which gates the `execute` tool) pass through the wrapper transparently.
SandboxBackendProtocol.register(ReadySandboxWrapper)
