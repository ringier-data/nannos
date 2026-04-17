"""File Analyzer Sub-Agent.

A local sub-agent for analyzing files (images, PDFs, text, audio, video) using multimodal capabilities.
This is a built-in capability, not an external A2A service, providing:

1. Clean LangSmith observability (separate agent trace)
2. True multimodal model (default: gemini-3-flash-preview) supporting audio/video natively
3. Consistent sub-agent interface with the rest of the system

The sub-agent accepts any HTTPS URL directly (public URLs work as-is).
For S3 URIs (s3://...), the orchestrator should first convert them to
presigned HTTPS URLs using the generate_presigned_url tool.

Supported file types:
- Images: PNG, JPG, JPEG, GIF, WebP, BMP, TIFF
- Documents: PDF
- Text: TXT, JSON, CSV, MD, XML, YAML, HTML
- Audio: MP3, WAV, M4A, MPEG, OGG, WEBM (with transcription)
- Video: MP4, WEBM, AVI, MOV (with audio analysis)

This module uses LocalA2ARunnable to provide the same response format
as remote A2A agents, ensuring consistent middleware behavior.

Configuration:
- Model can be set via FILE_ANALYZER_MODEL environment variable
- Default: gemini-3-flash-preview (true multimodal, supports audio/video)
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, cast

import httpx
from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.stream_events import TaskResponseData
from agent_common.core.model_factory import create_model, is_valid_model
from agent_common.models.base import ModelType
from deepagents import CompiledSubAgent
from langchain_core.messages import (
    ContentBlock,
    FileContentBlock,
    HumanMessage,
    ImageContentBlock,
    TextContentBlock,
)
from langchain_core.runnables import RunnableConfig
from langsmith import traceable
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.utils.streaming import extract_text_from_content

logger = logging.getLogger(__name__)

# Sub-agent configuration
FILE_ANALYZER_NAME = "file-analyzer"
FILE_ANALYZER_DESCRIPTION = (
    "Analyzes the content of attached files or files at HTTPS URLs and answers questions about them. "
    "Automatically detects and handles: images, PDFs, text files, audio files, and videos. "
    "Attached files from the user's message are forwarded automatically — just describe what analysis you need. "
    "For HTTPS URLs in text, include the full URL. "
    "Do NOT assume the file type - let the analyzer determine it. "
    "Works with any HTTPS URL - no presigning needed. Only S3 URIs (s3://...) need presigning first."
)

# Default model for file analysis (true multimodal with audio/video support)
# Can be overridden via FILE_ANALYZER_MODEL environment variable
# Note: Must be a model supporting file_url content (gpt-4o, claude-3.5-sonnet, or Gemini models)
DEFAULT_FILE_ANALYZER_MODEL: ModelType = "gemini-3-flash-preview"

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

AUDIO_MIME_TYPES = {
    "audio/mpeg",
    "audio/wav",
    "audio/m4a",
    "audio/ogg",
    "audio/webm",
    "audio/flac",
    "audio/aiff",
}

VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/webm",
    "video/avi",
    "video/quicktime",
    "video/x-msvideo",
}

# Fallback: file extensions when Content-Type is not available or is generic
TEXT_EXTENSIONS = {".txt", ".json", ".csv", ".md", ".xml", ".yaml", ".yml", ".html", ".htm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
DOCUMENT_EXTENSIONS = {".pdf"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac", ".aif", ".aiff", ".mpeg"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".avi", ".mov", ".mkv", ".flv", ".wmv"}

# Maximum text file size to fetch (1MB)
MAX_TEXT_FETCH_BYTES = 1 * 1024 * 1024

# System prompt for the file analyzer
FILE_ANALYZER_SYSTEM_PROMPT = """You are a file analysis assistant. Your job is to analyze files and answer questions about their content.

When analyzing a file:
1. Describe what you see/read/hear clearly and accurately - ONLY what is actually present
2. Answer any specific questions the user asks
3. Extract relevant information as requested
4. Be concise but thorough

For images: Describe visual elements, text content, charts, diagrams, etc.
For PDFs: Extract and summarize text, describe layouts, identify key information.
For text files: Summarize content, answer questions, extract specific data.
For audio files (audio/webm, audio/wav, etc.): Transcribe speech EXACTLY as spoken - word for word. DO NOT add, expand, or invent content that wasn't said. DO NOT describe visual content - audio files have no video.
For video files (video/mp4, video/webm, etc.): Describe visual content AND transcribe audio EXACTLY as spoken.

CRITICAL FOR AUDIO TRANSCRIPTION:
- Transcribe ONLY the exact words that were actually spoken
- DO NOT add context, explanations, or elaborate on what was said
- DO NOT invent follow-up sentences or additional dialogue
- DO NOT expand short messages into longer ones
- If the file is audio-only (MIME type starts with "audio/"), do NOT make up or describe any visual content

Always provide actionable, useful information based on what is actually in the file - not what you imagine might be there."""


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

    Returns one of: 'text', 'image', 'document', 'audio', 'video', 'unknown'
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
            if content_type in AUDIO_MIME_TYPES or content_type.startswith("audio/"):
                return "audio"
            if content_type in VIDEO_MIME_TYPES or content_type.startswith("video/"):
                return "video"
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
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"

    return "unknown"


def _create_file_analyzer_model(callbacks: Optional[List] = None):
    """Create the model for file analysis.

    Uses gpt-4o-mini by default for cost optimization on vision tasks.
    Can be overridden via FILE_ANALYZER_MODEL environment variable.

    Args:
        callbacks: Optional list of callbacks to attach to the model

    Returns:
        BaseChatModel: The configured model for file analysis.
    """
    # Get model from environment or use default
    model_name = os.getenv("FILE_ANALYZER_MODEL", DEFAULT_FILE_ANALYZER_MODEL)

    # Validate model if overridden
    if model_name != DEFAULT_FILE_ANALYZER_MODEL:
        if not is_valid_model(model_name):
            logger.warning(
                f"Invalid FILE_ANALYZER_MODEL '{model_name}'. Falling back to default: {DEFAULT_FILE_ANALYZER_MODEL}"
            )
            model_name = DEFAULT_FILE_ANALYZER_MODEL

    logger.info(f"Creating file analyzer model: {model_name} with callbacks={callbacks}")
    return create_model(model_name, callbacks=callbacks, streaming=False)  # type: ignore


class FileAnalyzerRunnable(LocalA2ARunnable):
    """Local sub-agent for analyzing files (images, PDFs, text, audio, video).

    Uses multimodal LLM capabilities (default: Gemini) to analyze file content.
    Natively supports audio transcription and video analysis without separate services.
    Uses CostTrackingCallback for automatic cost tracking through LangChain.
    Extends LocalA2ARunnable to ensure consistent response format with remote A2A agents.

    Cost Attribution:
        File-analyzer costs are attributed to the orchestrator (sub_agent_id=None) since
        it's a built-in system capability, not a user-created sub-agent. This treats
        file analysis as part of the orchestrator's operational overhead.
    """

    def __init__(
        self,
        cost_logger: Optional[CostLogger] = None,
        sub_agent_id: Optional[int] = None,
        user_sub: Optional[str] = None,
    ):
        """Initialize file analyzer with optional cost tracking.

        Args:
            cost_logger: Shared CostLogger instance from GraphFactory (optional)
            sub_agent_id: Sub-agent ID for cost attribution (optional)
            user_sub: User subscription ID for cost attribution (optional)
        """
        super().__init__()

        self.sub_agent_id = sub_agent_id  # Store for tag construction
        self._cost_logger = cost_logger  # Use shared instance
        self._user_sub = user_sub  # Store user_sub for potential use in tags or callbacks

        logger.info(
            f"Initializing FileAnalyzerRunnable with cost_logger={cost_logger is not None}, sub_agent_id={sub_agent_id}, user_sub={user_sub}"
        )

    @property
    def name(self) -> str:
        """Return the agent name."""
        return FILE_ANALYZER_NAME

    def get_supported_input_modes(self) -> List[str]:
        """Get list of input modes supported by the file analyzer.

        Gemini 3 Flash (default model) natively supports all major file types:
        text, images, PDFs/documents, audio, and video.

        Returns:
            List of supported content types
        """
        return ["text", "image", "file", "audio", "video"]

    def get_model_type(self) -> str | None:
        """Return the file analyzer model type for provider-specific transforms."""
        return os.getenv("FILE_ANALYZER_MODEL", DEFAULT_FILE_ANALYZER_MODEL)

    @property
    def description(self) -> str:
        """Return the agent description."""
        return FILE_ANALYZER_DESCRIPTION

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        """Return checkpoint namespace for this agent.

        Args:
            input_data: Validated input data

        Returns:
            Checkpoint namespace (e.g., "file-analyzer")
        """
        return "file-analyzer"

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        """Return identifier for cost tracking.

        Args:
            input_data: Validated input data

        Returns:
            Sub-agent identifier (\"file-analyzer\" since it's a built-in system capability)
        """
        # File-analyzer costs are attributed to the orchestrator (built-in system capability)
        if self.sub_agent_id is not None:
            return str(self.sub_agent_id)
        return "file-analyzer"

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

            elif file_type == "audio":
                logger.info("Detected audio file, adding to multimodal request...")
                # Audio files are passed as file blocks with MIME type detection
                # Gemini models will transcribe and analyze the audio content
                content_type = "audio/mpeg"  # Default; can be refined based on extension
                ext = _get_file_extension(file_url)
                if ext == ".wav":
                    content_type = "audio/wav"
                elif ext == ".ogg":
                    content_type = "audio/ogg"
                elif ext == ".webm":
                    content_type = "audio/webm"
                elif ext == ".m4a":
                    content_type = "audio/m4a"
                elif ext == ".flac":
                    content_type = "audio/flac"

                file_block: FileContentBlock = {
                    "type": "file",
                    "url": file_url,
                    "mime_type": content_type,
                }
                content_blocks.append(file_block)

            elif file_type == "video":
                logger.info("Detected video file, adding to multimodal request...")
                # Video files are passed as file blocks
                # Gemini models will analyze both video and audio content
                content_type = "video/mp4"  # Default; can be refined based on extension
                ext = _get_file_extension(file_url)
                if ext == ".webm":
                    content_type = "video/webm"
                elif ext == ".avi":
                    content_type = "video/avi"
                elif ext == ".mov":
                    content_type = "video/quicktime"

                file_block: FileContentBlock = {
                    "type": "file",
                    "url": file_url,
                    "mime_type": content_type,
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
        content_blocks: list,
        conversation_id: Optional[str] = None,
    ) -> str:
        """Synthesize analysis from prepared content blocks.

        Prepends a system prompt with file-type context, then invokes the
        multimodal model on the full content block list.

        Args:
            content_blocks: Prepared content blocks (text + file blocks)
            conversation_id: Conversation ID for cost attribution

        Returns:
            Analysis result as string
        """
        from ringier_a2a_sdk.cost_tracking import CostTrackingCallback

        callbacks = []
        if self._cost_logger:
            callbacks.append(CostTrackingCallback(self._cost_logger, sub_agent_id=self.sub_agent_id))
            logger.info("Cost tracking enabled for file-analyzer with shared CostLogger")

        model = _create_file_analyzer_model(callbacks=callbacks if callbacks else None)

        # Extract file type info and user request text from content blocks
        file_types = []
        user_text_parts = []
        for block in content_blocks:
            if isinstance(block, dict):
                if block.get("type") == "file" and "mime_type" in block:
                    file_types.append(block["mime_type"])
                elif block.get("type") == "text" and "text" in block:
                    user_text_parts.append(block["text"])

        user_request = "\n".join(user_text_parts) if user_text_parts else "Analyze the attached file(s)."

        # Build file type context for system prompt
        file_type_context = ""
        if file_types:
            file_type_context = f"\n\nFile types being analyzed: {', '.join(file_types)}"
            if any(ft.startswith("audio/") for ft in file_types):
                file_type_context += "\nNote: Audio files contain NO visual content - only analyze the audio."

        # Prepend system prompt block
        prompt_block: TextContentBlock = {
            "type": "text",
            "text": f"{FILE_ANALYZER_SYSTEM_PROMPT}{file_type_context}\n\nUser request: {user_request}",
        }
        # Replace text blocks with the combined prompt, keep file blocks
        file_only_blocks = [b for b in content_blocks if isinstance(b, dict) and b.get("type") != "text"]
        all_blocks = [prompt_block] + file_only_blocks

        analysis_message = HumanMessage(content_blocks=cast(list[ContentBlock], all_blocks))

        # Cost tracking config
        if self._user_sub and conversation_id:
            tags = [
                f"user_sub:{self._user_sub}",
                f"conversation:{conversation_id}",
            ]
            if self.sub_agent_id:
                tags.append(f"sub_agent:{self.sub_agent_id}")
            config = RunnableConfig(tags=tags)  # type: ignore
            logger.info(f"[COST TRACKING] Invoking model with tags: {tags}")
            response = await model.ainvoke([analysis_message], config=config)
        else:
            logger.warning("[COST TRACKING] No user_sub/conversation_id available, cost tracking may be incomplete")
            response = await model.ainvoke([analysis_message])

        return extract_text_from_content(response.content)[0]

    async def _prepare_human_message_input(self, input_data: SubAgentInput) -> HumanMessage:
        """Extend base with file-type detection, MIME correction, and URL extraction.

        Calls _extract_and_validate_blocks() directly (instead of super()) to avoid
        an unnecessary decompose/recompose roundtrip, then applies file-analyzer-
        specific processing:
        1. Image blocks passed through directly (natively supported by multimodal models)
        2. Other file blocks (audio, video, documents) routed through _fetch_files
           for proper MIME type detection — critical for audio/webm correction
           where browsers send video/webm but Gemini requires audio/webm
        3. Regex URL fallback when no file blocks are present (user-typed URLs)

        S3 URI validation is handled by the base _extract_and_validate_blocks().

        Raises:
            ValueError: For user-facing input issues (S3 URIs, no URLs, no processable files)
            httpx.HTTPStatusError: For HTTP errors during file fetching
            httpx.TimeoutException: For timeout during file fetching
        """
        text_content, file_blocks = await self._extract_and_validate_blocks(input_data)

        if file_blocks:
            # Route: images pass through directly, everything else through _fetch_files
            # for proper file-type detection and MIME handling
            processed_blocks: List[ContentBlock] = []
            urls_needing_fetch: List[str] = []

            for block in file_blocks:
                if not isinstance(block, dict):
                    continue
                url = block.get("url", "")
                block_type = block.get("type", "")

                if block_type == "image" and url:
                    processed_blocks.append(block)
                elif url:
                    urls_needing_fetch.append(url)

            if urls_needing_fetch:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    fetched = await self._fetch_files(urls_needing_fetch, client)
                    processed_blocks.extend(fetched)

            if not processed_blocks:
                raise ValueError("No processable files found in the attached content blocks.")

            logger.info(f"Prepared {len(processed_blocks)} file content block(s) for analysis")

        else:
            # Regex fallback: extract URLs from text content
            s3_uris = S3_URI_PATTERN.findall(text_content)
            if s3_uris:
                raise ValueError(f"I cannot directly access S3 URIs. Please provide a presigned URL for: {s3_uris[0]}")

            urls = URL_PATTERN.findall(text_content)
            if not urls:
                raise ValueError(
                    "No URL found in the request. Please provide a presigned HTTPS URL to analyze, "
                    "e.g., 'What is shown in https://...?'"
                )

            logger.info(f"Extracting {len(urls)} URL(s) from text (regex fallback)")

            async with httpx.AsyncClient(timeout=30.0) as client:
                processed_blocks = await self._fetch_files(urls, client)

        # Build HumanMessage with text + processed file blocks
        content_blocks: List[ContentBlock] = []
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})  # type: ignore[arg-type]
        content_blocks.extend(processed_blocks)
        return HumanMessage(content=content_blocks)  # type: ignore[arg-type]

    async def _process(
        self,
        input_data: SubAgentInput,
        config: Optional[Dict[str, Any]] = None,
    ) -> TaskResponseData:
        """Analyze file(s) using the prepared multimodal HumanMessage.

        Uses the overridden _prepare_human_message_input which handles:
        - S3 URI validation
        - File-type detection and MIME correction via _fetch_files
        - Regex URL extraction fallback for user-typed URLs

        Args:
            input_data: Validated input with messages and tracking IDs
            config: Optional parent config from orchestrator
        """
        context_id, task_id = self._extract_tracking_ids(input_data)
        conversation_id = input_data.orchestrator_conversation_id or context_id

        try:
            human_message = await self._prepare_human_message_input(input_data)

            analysis_result = await self._synthesize_analysis(
                human_message.content if isinstance(human_message.content, list) else [],
                conversation_id=conversation_id,
            )
            return self._build_success_response(analysis_result, context_id=context_id, task_id=task_id)

        except ValueError as e:
            return self._build_input_required_response(str(e), context_id=context_id, task_id=task_id)
        except httpx.HTTPStatusError as e:
            return self._handle_http_error(e, context_id, task_id)
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

    def _handle_http_error(
        self,
        e: httpx.HTTPStatusError,
        context_id: Optional[str],
        task_id: Optional[str],
    ) -> TaskResponseData:
        """Build appropriate response for HTTP errors."""
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


def create_file_analyzer_subagent(
    cost_logger: Optional[CostLogger] = None,
    sub_agent_id: Optional[int] = None,
    user_sub: Optional[str] = None,
) -> CompiledSubAgent:
    """Create the file analyzer sub-agent.

    Args:
        cost_logger: Shared CostLogger instance from GraphFactory (optional)
        sub_agent_id: Optional sub-agent ID for cost attribution

    Returns:
        CompiledSubAgent that can be registered with the orchestrator
    """
    runnable = FileAnalyzerRunnable(
        cost_logger=cost_logger,
        sub_agent_id=sub_agent_id,
        user_sub=user_sub,  # Pass user_sub for cost attribution in the runnable
    )

    # Cast to Any for CompiledSubAgent compatibility (duck typing)
    return CompiledSubAgent(
        name=runnable.name,
        description=runnable.description,
        runnable=runnable,  # type: ignore[arg-type]
    )
