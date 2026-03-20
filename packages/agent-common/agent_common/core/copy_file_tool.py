"""Copy to memories tool for efficient file copying without LLM context loading.

This tool allows the agent to copy ephemeral files (like large tool results) to
long-term memory (/memories/) without reading them into the LLM context. This is
particularly useful for large files that shouldn't be processed by the model.
"""

import logging
from typing import Any, Callable

from deepagents.backends.protocol import BackendProtocol
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent_common.core.document_store_tools import FilesystemState

logger = logging.getLogger(__name__)


class CopyFileInput(BaseModel):
    """Input schema for copy_file tool."""

    source_path: str = Field(
        ...,
        description="Absolute path to the source file to copy (e.g., '/large_tool_results/tool_xyz', '/temp/file.txt')",
    )
    destination_name: str | None = Field(
        default=None,
        description=(
            "Optional destination filename in /memories/. "
            "If not provided, uses the basename of source_path. "
            "Example: 'analysis_result.txt'"
        ),
    )


def _validate_path(path: str) -> str:
    """Validate that path is absolute and doesn't contain traversal patterns.

    Args:
        path: File path to validate

    Returns:
        Validated path

    Raises:
        ValueError: If path is invalid
    """
    if not path.startswith("/"):
        raise ValueError(f"Path must be absolute: {path}")

    if ".." in path:
        raise ValueError(f"Path traversal not allowed: {path}")

    return path


def create_copy_file_tool(backend_factory: Callable[[Any], BackendProtocol]) -> BaseTool:
    """Create tool for copying files to long-term memory without LLM context loading.

    The tool uses the CompositeBackend's routing to automatically:
    1. Read from the appropriate source backend (ephemeral/persistent)
    2. Write to the /memories/ backend (IndexingStoreBackend with semantic indexing)

    This is efficient because:
    - Content is transferred directly between backends
    - No LLM tokenization or context window usage
    - Automatic semantic indexing on write to /memories/

    Args:
        backend_factory: Callable that takes ToolRuntime and returns CompositeBackend

    Returns:
        BaseTool for copying files to memories
    """

    async def copy_file_handler(
        source_path: str,
        destination_name: str | None,
        runtime: ToolRuntime[None, FilesystemState],
    ) -> str:
        """Copy a file to long-term memory without loading into LLM context.

        Use this when you have a large file (e.g., tool result, analysis output) that should
        be preserved in long-term memory but doesn't need to be read by the LLM.

        The file will be automatically indexed for semantic search via docstore_search tool.

        Args:
            source_path: Absolute path to source file
            destination_name: Optional destination filename (uses source basename if not provided)
            runtime: Tool runtime providing access to backends

        Returns:
            Success message with destination path, or error message
        """
        try:
            # Validate paths
            source_path = _validate_path(source_path)

            # Determine destination path
            if destination_name:
                # Ensure destination_name doesn't start with /
                destination_name = destination_name.lstrip("/")
                destination_path = f"/memories/{destination_name}"
            else:
                # Extract basename from source path
                basename = source_path.split("/")[-1]
                if not basename:
                    return "Error: Could not determine destination filename. Please provide destination_name."
                destination_path = f"/memories/{basename}"

            destination_path = _validate_path(destination_path)

            # Get backend from factory
            backend: BackendProtocol = backend_factory(runtime)

            logger.info(f"Copying file from '{source_path}' to '{destination_path}'")

            # Read from source using backend (routes to correct backend via CompositeBackend)
            try:
                source_content = await backend.aread(source_path)
            except Exception as e:
                logger.error(f"Failed to read source file '{source_path}': {e}")
                return f"Error: Could not read source file '{source_path}'. Make sure it exists and is accessible. Details: {str(e)}"

            if not source_content:
                return f"Error: Source file '{source_path}' is empty or not found."

            # Write to destination (will route to IndexingStoreBackend for /memories/)
            # This triggers automatic semantic indexing
            try:
                write_result = await backend.awrite(destination_path, source_content)

                if write_result.error:
                    logger.error(f"Failed to write to '{destination_path}': {write_result.error}")
                    return f"Error: Could not write to '{destination_path}'. Details: {write_result.error}"

            except Exception as e:
                logger.error(f"Failed to write to destination '{destination_path}': {e}")
                return f"Error: Could not write to '{destination_path}'. Details: {str(e)}"

            logger.info(f"Successfully copied '{source_path}' to '{destination_path}'")

            # Calculate file size for feedback
            file_size = len(source_content)
            if file_size < 1024:
                size_str = f"{file_size} bytes"
            elif file_size < 1024 * 1024:
                size_str = f"{file_size / 1024:.1f} KB"
            else:
                size_str = f"{file_size / (1024 * 1024):.1f} MB"

            return (
                f"Successfully copied file to long-term memory.\n\n"
                f"Destination: {destination_path}\n"
                f"Size: {size_str}\n\n"
                f"The file has been automatically indexed for semantic search. "
                f"Use docstore_search tool to find relevant sections."
            )

        except ValueError as e:
            logger.warning(f"Invalid path in copy_file: {e}")
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"Unexpected error in copy_file: {e}", exc_info=True)
            return f"Error: Unexpected error during copy: {str(e)}"

    return StructuredTool.from_function(
        coroutine=copy_file_handler,
        name="copy_file",
        description=(
            "Copy a file to long-term memory WITHOUT loading it into your context window. "
            "Use this for large files (tool results, analysis outputs) that should be "
            "preserved but don't need to be read by you. The file will be automatically "
            "indexed for semantic search via docstore_search tool."
        ),
        args_schema=CopyFileInput,
    )
