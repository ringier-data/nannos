"""File Analyzer Sub-Agent.

A local sub-agent for analyzing files (images, PDFs, text) using multimodal capabilities.
This is a built-in capability, not an external A2A service, providing:

1. Clean LangSmith observability (separate agent trace)
2. Ability to use a cheaper/specialized vision model
3. Consistent sub-agent interface with the rest of the system

The sub-agent accepts any HTTPS URL directly (public URLs work as-is).
For S3 URIs (s3://...), the orchestrator should first convert them to
presigned HTTPS URLs using the generate_presigned_url tool.

For text files, the sub-agent fetches and includes the content.
For images/PDFs, it passes URLs directly to the vision model.

This module uses LocalA2ARunnable to provide the same response format
as remote A2A agents, ensuring consistent middleware behavior.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from deepagents import CompiledSubAgent
from langchain_core.messages import (
    FileContentBlock,
    HumanMessage,
    ImageContentBlock,
    TextContentBlock,
)
from langsmith import traceable

from app.core.model_factory import create_model

from ..a2a.base import LocalA2ARunnable, SubAgentInput

logger = logging.getLogger(__name__)

# Sub-agent configuration
FILE_ANALYZER_NAME = "file-analyzer"
FILE_ANALYZER_DESCRIPTION = (
    "Analyzes files (images, PDFs, text) via vision and answers questions about their content. "
    "IMPORTANT: You MUST include the full URL in your description! "
    "Example: 'What is shown in https://example.com/image.png?' "
    "Works with any HTTPS URL - no presigning needed. Only S3 URIs (s3://...) need presigning first."
)

# Regex to extract URLs from text
# Uses negative lookbehind to exclude trailing punctuation (periods, commas, etc.)
# that often follow URLs in natural text
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+(?<![.,;!?\)\]])")
S3_URI_PATTERN = re.compile(r"s3://[^\s<>\"']+(?<![.,;!?\)\]])")

# MIME type categories for file handling
TEXT_MIME_TYPES = {
    "text/plain",
    "text/csv",
    "text/html",
    "text/css",
    "text/javascript",
    "text/markdown",
    "text/xml",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
}

IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/tiff",
}

DOCUMENT_MIME_TYPES = {
    "application/pdf",
}

# Fallback: file extensions when Content-Type is not available or is generic
TEXT_EXTENSIONS = {".txt", ".json", ".csv", ".md", ".xml", ".yaml", ".yml", ".html", ".htm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
DOCUMENT_EXTENSIONS = {".pdf"}

# Maximum text file size to fetch (1MB)
MAX_TEXT_FETCH_BYTES = 1 * 1024 * 1024

# System prompt for the file analyzer
FILE_ANALYZER_SYSTEM_PROMPT = """You are a file analysis assistant. Your job is to analyze files and answer questions about their content.

When analyzing a file:
1. Describe what you see/read clearly and accurately
2. Answer any specific questions the user asks
3. Extract relevant information as requested
4. Be concise but thorough

For images: Describe visual elements, text content, charts, diagrams, etc.
For PDFs: Extract and summarize text, describe layouts, identify key information.
For text files: Summarize content, answer questions, extract specific data.

Always provide actionable, useful information."""


def _get_file_extension(url: str) -> str:
    """Extract file extension from URL, ignoring query parameters."""
    path = url.split("?")[0]
    filename = path.rsplit("/", 1)[-1]
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ""


async def _detect_file_type(url: str, client: httpx.AsyncClient) -> str:
    """Detect file type using HTTP Range GET request with extension fallback.

    Uses Range header to fetch minimal data while getting Content-Type.
    This is compatible with S3 presigned URLs (which only support GET, not HEAD).
    """
    try:
        # Use Range GET instead of HEAD - S3 presigned URLs are signed for GET only
        response = await client.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True)
        # Accept both 200 (full content) and 206 (partial content) as success
        content_type = response.headers.get("content-type", "").lower().split(";")[0].strip()

        if content_type:
            if content_type in TEXT_MIME_TYPES or content_type.startswith("text/"):
                return "text"
            if content_type in IMAGE_MIME_TYPES:
                return "image"
            if content_type in DOCUMENT_MIME_TYPES:
                return "document"
            if content_type != "application/octet-stream" and content_type != "binary/octet-stream":
                logger.debug(f"Unknown content-type '{content_type}', treating as document")
                return "document"

    except httpx.HTTPError as e:
        logger.debug(f"HEAD request failed for {url[:60]}...: {e}")

    # Fallback to extension-based detection
    ext = _get_file_extension(url)
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"

    return "unknown"


def _create_file_analyzer_model():
    """Create the model for file analysis."""

    return create_model("gpt-4o-mini", config=None, thinking=False)


class FileAnalyzerRunnable(LocalA2ARunnable):
    """Local sub-agent for analyzing files (images, PDFs, text).

    Uses multimodal LLM capabilities to analyze file content.
    Extends LocalA2ARunnable to ensure consistent response format
    with remote A2A agents.
    """

    @property
    def name(self) -> str:
        """Return the agent name."""
        return FILE_ANALYZER_NAME

    @property
    def description(self) -> str:
        """Return the agent description."""
        return FILE_ANALYZER_DESCRIPTION

    @traceable(name="fetch_files")
    async def _fetch_files(
        self,
        urls: List[str],
        client: httpx.AsyncClient,
    ) -> List[TextContentBlock | ImageContentBlock | FileContentBlock]:
        """Fetch and prepare content blocks from URLs.

        Args:
            urls: List of HTTPS URLs to fetch
            client: HTTP client for making requests

        Returns:
            List of content blocks ready for model input
        """
        content_blocks: List[TextContentBlock | ImageContentBlock | FileContentBlock] = []

        for file_url in urls:
            logger.info(f"Processing: {file_url[:80]}...")
            file_type = await _detect_file_type(file_url, client)

            if file_type == "text":
                logger.info("Detected text file, fetching content...")
                response = await client.get(file_url)
                response.raise_for_status()

                if len(response.content) > MAX_TEXT_FETCH_BYTES:
                    raise ValueError(
                        f"File too large ({len(response.content):,} bytes). Maximum is {MAX_TEXT_FETCH_BYTES:,} bytes."
                    )

                text_block: TextContentBlock = {
                    "type": "text",
                    "text": f"\n--- File: {file_url.split('?')[0].rsplit('/', 1)[-1]} ---\n{response.text}\n",
                }
                content_blocks.append(text_block)

            elif file_type == "image":
                logger.info("Detected image file, adding to vision request...")
                image_block: ImageContentBlock = {"type": "image", "url": file_url}
                content_blocks.append(image_block)

            elif file_type == "document":
                logger.info("Detected document file, adding to request...")
                file_block: FileContentBlock = {
                    "type": "file",
                    "url": file_url,
                    "mime_type": "application/pdf",
                }
                content_blocks.append(file_block)

            else:
                logger.info("Unknown file type, adding as generic file...")
                file_block: FileContentBlock = {"type": "file", "url": file_url}
                content_blocks.append(file_block)

        return content_blocks

    @traceable(name="synthesize_analysis")
    async def _synthesize_analysis(
        self,
        content: str,
        file_blocks: List[TextContentBlock | ImageContentBlock | FileContentBlock],
    ) -> str:
        """Synthesize analysis from fetched file content.

        Args:
            content: Original user request
            file_blocks: Content blocks from fetched files

        Returns:
            Analysis result as string
        """
        model = _create_file_analyzer_model()

        # Build complete content blocks with prompt
        prompt_block: TextContentBlock = {
            "type": "text",
            "text": f"{FILE_ANALYZER_SYSTEM_PROMPT}\n\nUser request: {content}",
        }

        all_content_blocks = [prompt_block] + file_blocks

        analysis_message = HumanMessage(content_blocks=all_content_blocks)
        response = await model.ainvoke([analysis_message])
        return str(response.content)

    async def _process(
        self,
        input_data: SubAgentInput,
    ) -> Dict[str, Any]:
        """Analyze file(s) from URLs in the content."""
        # Check for S3 URIs first
        # Extract content and IDs from input_data
        content = self._extract_message_content(input_data)
        context_id, task_id = self._extract_tracking_ids(input_data)

        s3_uris = S3_URI_PATTERN.findall(content)
        if s3_uris:
            return self._build_input_required_response(
                f"I cannot directly access S3 URIs. Please provide a presigned URL for: {s3_uris[0]}",
                context_id=context_id,
                task_id=task_id,
            )

        # Extract HTTPS URLs
        urls = URL_PATTERN.findall(content)
        if not urls:
            return self._build_input_required_response(
                "No URL found in the request. Please provide a presigned HTTPS URL to analyze, "
                "e.g., 'What is shown in https://...?'",
                context_id=context_id,
                task_id=task_id,
            )

        logger.info(f"Analyzing {len(urls)} file(s) from URLs...: {urls}")

        try:
            # Fetch files and prepare content blocks
            async with httpx.AsyncClient(timeout=30.0) as client:
                file_blocks = await self._fetch_files(urls, client)

            # Synthesize analysis from fetched content
            analysis_result = await self._synthesize_analysis(content, file_blocks)

            return self._build_success_response(analysis_result, context_id=context_id, task_id=task_id)

        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return self._build_input_required_response(
                    f"Access denied (HTTP {e.response.status_code}). "
                    "The URL may have expired or be invalid. Please provide a valid presigned URL.",
                    context_id=context_id,
                    task_id=task_id,
                )
            elif e.response.status_code == 404:
                return self._build_input_required_response(
                    "File not found (HTTP 404). Please check the URL and try again.",
                    context_id=context_id,
                    task_id=task_id,
                )
            else:
                logger.error(f"HTTP error fetching file: {e}")
                return self._build_error_response(
                    f"HTTP error {e.response.status_code} accessing the file.",
                    context_id=context_id,
                    task_id=task_id,
                )

        except httpx.TimeoutException:
            return self._build_input_required_response(
                "Request timed out. The file may be too large or the URL may be slow. "
                "Please try again or provide a different URL.",
                context_id=context_id,
                task_id=task_id,
            )

        except Exception as e:
            logger.error(f"Failed to analyze file: {e}")
            error_str = str(e).lower()
            if "url" in error_str or "access" in error_str or "permission" in error_str:
                return self._build_input_required_response(
                    f"Could not access the file: {e}. Please ensure the URL is valid.",
                    context_id=context_id,
                    task_id=task_id,
                )
            return self._build_error_response(f"Error analyzing file: {str(e)}", context_id=context_id, task_id=task_id)


# Singleton instance
_file_analyzer_instance: Optional[FileAnalyzerRunnable] = None


def _get_file_analyzer() -> FileAnalyzerRunnable:
    """Get or create the file analyzer runnable instance."""
    global _file_analyzer_instance
    if _file_analyzer_instance is None:
        _file_analyzer_instance = FileAnalyzerRunnable()
    return _file_analyzer_instance


def create_file_analyzer_subagent() -> CompiledSubAgent:
    """Create the file analyzer sub-agent.

    Returns:
        CompiledSubAgent that can be registered with the orchestrator
    """
    runnable = _get_file_analyzer()

    # Cast to Any for CompiledSubAgent compatibility (duck typing)
    return CompiledSubAgent(
        name=runnable.name,
        description=runnable.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
