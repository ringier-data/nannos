"""File handling tools for the orchestrator agent.

These tools allow the LLM to:
1. Generate presigned URLs for files to pass to sub-agents

The orchestrator uses generate_presigned_url when it needs to pass files
to sub-agents that accept URLs.

For reading file content directly, see file_analyzer.py which provides
the unified read_file tool.
"""

import logging
from typing import Literal

from agent_common.core.object_storage import get_object_storage_service
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class GeneratePresignedUrlInput(BaseModel):
    """Input schema for generate_presigned_url tool."""

    s3_uri: str = Field(
        ...,
        description="S3 URI of the file (format: s3://bucket/key)",
    )
    expiration: Literal["1h", "24h"] = Field(
        default="1h",
        description="URL expiration time: '1h' for 1 hour (default), '24h' for 24 hours (use for sub-agent dispatch)",
    )


def _create_generate_presigned_url_tool() -> BaseTool:
    """Create tool for generating presigned URLs.

    Returns:
        StructuredTool for presigned URL generation
    """

    async def generate_presigned_url_handler(s3_uri: str, expiration: str = "1h") -> str:
        """Generate a presigned URL for a storage file.

        Use this tool when you need to:
        - Pass a file to a sub-agent that accepts URLs
        - Provide a downloadable link for the user
        - Share a file reference without exposing credentials

        Args:
            s3_uri: Storage URI of the file (format: s3://bucket/key or file://bucket/key)
            expiration: URL expiration time ('1h' or '24h')

        Returns:
            Presigned HTTPS URL that can be used to access the file
        """
        expiration_seconds = 86400 if expiration == "24h" else 3600
        storage = get_object_storage_service()

        try:
            url = await storage.generate_presigned_url(s3_uri, expiration_seconds)
            logger.info(f"Generated presigned URL for {s3_uri} (expires in {expiration})")
            return url
        except Exception as e:
            logger.error(f"Failed to generate presigned URL for {s3_uri}: {e}")
            return f"Error generating presigned URL: {str(e)}"

    return StructuredTool.from_function(
        coroutine=generate_presigned_url_handler,
        name="generate_presigned_url",
        description=(
            "Convert an S3 URI (s3://bucket/key) to a presigned HTTPS URL. "
            "ONLY use this tool for S3 URIs - do NOT use for URLs that are already HTTPS. "
            "Choose '24h' expiration when the URL will be used by sub-agents."
        ),
        args_schema=GeneratePresignedUrlInput,
    )


def create_presigned_url_tool() -> BaseTool:
    """Create the presigned URL tool.

    Returns:
        Tool for generating presigned URLs
    """
    return _create_generate_presigned_url_tool()
