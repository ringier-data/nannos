"""File Analyzer Sub-Agent.

A local sub-agent for analyzing files (images, PDFs, text, audio) using multimodal capabilities.
This is a built-in capability, not an external A2A service, providing:

1. Clean LangSmith observability (separate agent trace)
2. A multimodal model (the fleet's cheap chat tier, resolved at runtime)
3. Consistent sub-agent interface with the rest of the system

The sub-agent accepts any HTTPS URL directly (public URLs work as-is).
For S3 URIs (s3://...), the orchestrator should first convert them to
presigned HTTPS URLs using the generate_presigned_url tool.

Supported file types:
- Images: PNG, JPG, JPEG, GIF, WebP, BMP, TIFF (sent as image_url)
- Documents: PDF (fetched and inlined as base64)
- Text: TXT, JSON, CSV, MD, XML, YAML, HTML (fetched inline)
- Audio: MP3, WAV, M4A, MPEG, OGG, WEBM (first-class chat input; inlined as base64 —
  requires an audio-capable resolved model, e.g. Gemini; Claude has no audio modality)

NOT supported (rejected with a clear message — see the video branch in _fetch_files):
- Video. Blocked by transport, not capability (the Gemini tier can do video): the ChatOpenAI
  wire format (ADR-0001) rejects a URL `file` block, and base64 doesn't scale to video. Real
  support is an upload pipeline — Vertex needs a gs:// (or Gemini File API) URI and won't fetch
  our S3 presigned URLs, so the file must be staged there first. See AGENTS.md ("the Video Gap").

This module uses LocalA2ARunnable to provide the same response format
as remote A2A agents, ensuring consistent middleware behavior.

Model: the fleet's cheap/fast chat tier, resolved at runtime from the gateway
(model_factory.get_default_fast_model). No env var or hardcoded alias: models are
registered at runtime.
"""

import asyncio
import base64
import ipaddress
import logging
import re
import socket
from typing import Any, Dict, List, Optional, cast
from urllib.parse import urlparse

import httpx
from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.stream_events import TaskResponseData
from agent_common.core.model_factory import (
    create_model,
    get_default_fast_model,
    get_model_input_capabilities,
    require_default_model,
)
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
    "Handles images, PDFs, text, and audio files. "
    "Video is NOT supported yet — do not route video files here. "
    "Attached files from the user's message are forwarded automatically — just describe what analysis you need. "
    "For HTTPS URLs in text, include the full URL. "
    "Do NOT assume the file type - let the analyzer determine it. "
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

# Maximum document (PDF) size to fetch and inline as base64. Documents are sent to the
# model as base64 (see _fetch_files) because the Chat Completions wire format the gateway
# speaks rejects file *URLs* — only base64/file_id file sources are accepted. Base64 inflates
# ~33%, and Anthropic/Bedrock cap the whole request near 32MB, so keep the raw file well under.
MAX_DOC_FETCH_BYTES = 20 * 1024 * 1024

# Maximum audio size to fetch and inline as base64. Audio is inlined for the same wire-format
# reason as PDFs (a file URL can't be expressed in Chat Completions), and Gemini's inline-data
# path caps the whole request near 20MB. Chat voice recordings are far smaller than this.
MAX_AUDIO_FETCH_BYTES = 20 * 1024 * 1024

# Content types the file-analyzer can process end-to-end, in advertised order. Used by
# get_supported_input_modes() to narrow the resolved model's declared input_modes. Excludes
# "video" (the gateway path can't carry it — see _fetch_files); "audio"/"file" pass through
# only when the model also declares them (audio needs an audio-capable model, e.g. Gemini).
_HANDLEABLE_MODES = ("text", "image", "file", "audio")

# Audio extension → MIME type for the base64 file block. Extensions not listed fall back to
# audio/mpeg (covers .mp3 and anything unrecognized). Detection accepts more (AUDIO_EXTENSIONS);
# this only needs the wire MIME for the formats we tag distinctly.
_AUDIO_EXT_TO_MIME = {
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".m4a": "audio/m4a",
    ".flac": "audio/flac",
}

# User-facing rejection messages. Shared between the preflight (_reject_unsupported_media,
# which routes on the declared block type) and _fetch_files (which routes on the type detected
# from the actual content) so both entry points emit the same message — audio/video can be
# discovered at either stage (e.g. a user-pasted URL only surfaces its type in _fetch_files).
_VIDEO_UNSUPPORTED_MSG = (
    "Video files aren't supported for analysis yet — "
    "I can analyze images, PDFs, audio, and text files."
)
_AUDIO_UNAVAILABLE_MSG = (
    "Audio transcription isn't available: the configured file-analyzer model doesn't "
    "accept audio input. An admin must set an audio-capable model (e.g. a Gemini model "
    "with 'audio' input mode) as the Low chat tier or the Chat default in "
    "Admin → Model Gateway."
)

# System prompt for the file analyzer
FILE_ANALYZER_SYSTEM_PROMPT = """<role>
You are a file analysis assistant. Your job is to analyze files and answer questions about their content.
</role>

<instructions>
When analyzing a file:
1. Describe what you see/read/hear clearly and accurately — ONLY what is actually present.
2. Answer any specific questions the user asks.
3. Extract relevant information as requested.
4. Be concise but thorough.
</instructions>

<file_type_guidelines>
- Images: Describe visual elements, text content, charts, diagrams, etc.
- PDFs: Extract and summarize text, describe layouts, identify key information.
- Text files: Summarize content, answer questions, extract specific data.
- Audio files (audio/webm, audio/wav, etc.): Transcribe speech EXACTLY as spoken, word for word. Do not describe visual content — audio files have no video.
</file_type_guidelines>

<audio_transcription_rules>
- Transcribe ONLY the exact words that were actually spoken.
- Do not add context, explanations, or elaborate on what was said.
- Do not invent follow-up sentences or additional dialogue.
- Do not expand short messages into longer ones.
- If the file is audio-only (MIME type starts with "audio/"), do not make up or describe any visual content.
</audio_transcription_rules>

Always provide actionable, useful information based on what is actually in the file — not what you imagine might be there."""


class SSRFError(ValueError):
    """Raised when a URL targets a non-public address (SSRF guard)."""


def _is_blocked_address(ip: str) -> bool:
    """True if an IP is loopback/private/link-local/reserved (i.e. not publicly routable)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local  # blocks 169.254.0.0/16, incl. the cloud metadata endpoint
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def _assert_public_url(url: str) -> None:
    """Reject URLs whose host resolves to a non-public address.

    The file-analyzer fetches attacker-supplied URLs server-side (from inside the
    orchestrator pod) and returns their body, so an unrestricted fetch is an SSRF that
    can reach cluster-internal services and cloud-metadata endpoints. Resolve the host
    and refuse any URL that maps to a loopback/private/link-local/reserved address.

    Note: this validates at resolution time and does not pin the connection to the
    resolved IP, so it does not defend against active DNS rebinding; combine with an
    egress NetworkPolicy for defense-in-depth.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise SSRFError("URL has no host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFError(f"Could not resolve host {host!r}: {e}")

    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise SSRFError(f"Host {host!r} did not resolve")
    for ip in resolved:
        if _is_blocked_address(ip):
            raise SSRFError(f"Refusing to fetch a URL that resolves to a non-public address ({ip})")


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
        # Use Range GET instead of HEAD - S3 presigned URLs are signed for GET only.
        # follow_redirects stays off so a redirect can't bounce the SSRF-guarded URL
        # into an internal/metadata address after validation.
        response = await client.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=False)
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


async def _fetch_bytes_capped(
    client: httpx.AsyncClient, url: str, max_bytes: int, too_large_msg: str
) -> bytes:
    """GET ``url`` and return its body, aborting as soon as it exceeds ``max_bytes``.

    Streams the response so an oversized (or malicious) URL is never fully buffered before the
    limit is enforced: a declared ``Content-Length`` over the cap is rejected before reading the
    body, and the incremental read still caps memory at ~``max_bytes`` when the header is absent
    or lies. Raises ``ValueError(too_large_msg)`` when the cap is exceeded.
    """
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        cl = response.headers.get("content-length")
        if cl and cl.strip().isdigit() and int(cl) > max_bytes:
            raise ValueError(too_large_msg)
        buf = bytearray()
        async for chunk in response.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError(too_large_msg)
        return bytes(buf)


def _create_file_analyzer_model(callbacks: Optional[List] = None):
    """Create the model for file analysis.

    Runs on the fleet's cheap/fast chat tier, resolved at runtime from the gateway
    (model_factory.get_default_fast_model). File analysis needs a multimodal-capable model
    (image/PDF/audio), so the admin's chat / chat:low default must support file content. Audio
    additionally requires an audio-capable tier (e.g. Gemini); video is not supported (see
    _fetch_files / AGENTS.md "the Video Gap").

    Args:
        callbacks: Optional list of callbacks to attach to the model

    Returns:
        BaseChatModel: The configured model for file analysis.
    """
    model_name = get_default_fast_model() or require_default_model()
    logger.info(f"Creating file analyzer model: {model_name} with callbacks={callbacks}")
    return create_model(model_name, callbacks=callbacks, streaming=False)


class FileAnalyzerRunnable(LocalA2ARunnable):
    """Local sub-agent for analyzing files (images, PDFs, text, audio).

    Uses multimodal LLM capabilities (default: Gemini) to analyze file content.
    Supports audio transcription (on an audio-capable tier) without a separate service; video is
    not supported through the gateway path (see _fetch_files / AGENTS.md "the Video Gap").
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
        """Content types the file-analyzer actually handles end-to-end.

        Starts from the resolved model's gateway-declared input_modes (set at registration) and
        narrows to what this agent can genuinely process (`_HANDLEABLE_MODES`):
        - **Video is always dropped** — the gateway path can't carry it (see `_fetch_files`),
          regardless of what the model declares.
        - **Audio / file (PDF) are offered only when the model declares them** — audio needs an
          audio-capable model (e.g. Gemini); a text/vision-only tier (e.g. Claude/Bedrock) can't
          transcribe audio, so we neither advertise nor attempt it.

        This is both the agent-card capability (so the orchestrator routes honestly) and the
        input to the base block-strip. Falls back to text+image when no default is configured
        or the gateway snapshot is unavailable.
        """
        model_type = self.get_model_type()
        model_modes: List[str] = ["text", "image"]
        if model_type:
            try:
                model_modes = list(get_model_input_capabilities(model_type))  # type: ignore[arg-type]
            except Exception:
                pass
        return [m for m in _HANDLEABLE_MODES if m in model_modes]

    def _supports_audio(self) -> bool:
        """Whether the resolved file-analyzer model accepts audio input.

        True only when the model declares `audio` in its gateway input_modes — in practice an
        audio-capable model such as Gemini. On a text/vision-only tier (e.g. Claude on Bedrock)
        audio transcription is unavailable and audio attachments are rejected with a clear
        message (see `_reject_unsupported_media`) rather than attempted and silently failed.
        """
        return "audio" in self.get_supported_input_modes()

    def _reject_unsupported_media(self, input_data: SubAgentInput) -> None:
        """Fail fast with a clear, user-facing message for media this agent can't process.

        Runs before the base block-strip so unsupported media doesn't get silently rewritten to
        a text description — which surfaces as the generic "No processable files" error and, worse,
        prompts the orchestrator to pointlessly re-delegate to general-purpose.

        - **Video** is never supported (the gateway path can't carry it — see `_fetch_files`).
        - **Audio** requires an audio-capable resolved model (e.g. Gemini). On a text/vision-only
          tier there's no point attempting it.

        Gated on the block's declared type (the same signal the base strip routes on), so no fetch
        happens first.
        """
        if not input_data.messages:
            return
        content = input_data.messages[-1].content
        if not isinstance(content, list):
            return
        types = {b.get("type") for b in content if isinstance(b, dict)}

        if "video" in types:
            raise ValueError(_VIDEO_UNSUPPORTED_MSG)
        if "audio" in types and not self._supports_audio():
            raise ValueError(_AUDIO_UNAVAILABLE_MSG)

    def get_model_type(self) -> str | None:
        """Return the file analyzer model type for provider-specific transforms.

        Resolves the same runtime alias _create_file_analyzer_model uses; None when no
        default is configured (no transform applied)."""
        return get_default_fast_model()

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
            # SSRF guard: refuse URLs pointing at internal/metadata addresses before any fetch.
            await _assert_public_url(file_url)
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
                # Documents must be sent as base64, NOT as a URL — and this is NOT a
                # Bedrock-specific choice, so don't gate it on the provider. Every model goes
                # through the gateway as a langchain ChatOpenAI client speaking OpenAI Chat
                # Completions (ADR-0001), whose file content part has no URL form: a file block
                # carrying a `url` is rejected at payload build ("file URLs ... with Chat
                # Completions"), for every provider. base64 is the one source that works across
                # all of them (Bedrock/Vertex additionally accept base64 only at the provider
                # layer). Images differ — image_url accepts a URL — which is why only documents
                # need this. Revisit only if we ever route documents through a native
                # (non-ChatOpenAI) client or the Responses API.
                logger.info("Detected document file, fetching bytes to inline as base64...")
                content = await _fetch_bytes_capped(
                    client,
                    file_url,
                    MAX_DOC_FETCH_BYTES,
                    f"PDF too large. Maximum is {MAX_DOC_FETCH_BYTES:,} bytes.",
                )

                filename = file_url.split("?")[0].rsplit("/", 1)[-1] or "document.pdf"
                file_block: FileContentBlock = {
                    "type": "file",
                    "base64": base64.b64encode(content).decode("ascii"),
                    "mime_type": "application/pdf",
                    "filename": filename,
                }
                content_blocks.append(file_block)

            elif file_type == "audio":
                # Audio must be inlined as base64, NOT sent as a URL — same wire-format reason as
                # documents: a `file` block carrying a `url` is rejected at payload build ("file
                # URLs ... with Chat Completions"). base64 is viable here because chat voice
                # recordings are small; the resolved model must be audio-capable (e.g. Gemini) —
                # Claude has no audio modality. LiteLLM's Vertex path accepts base64 `file` blocks.
                # Capability gate — also enforced here, not only in the preflight. The preflight
                # (_reject_unsupported_media) routes on the declared block type, but audio can be
                # discovered here for the first time when it wasn't typed "audio" upstream — e.g. a
                # user-pasted audio URL (regex fallback, no block) or a file block whose mime isn't
                # audio/*. Without this check such audio would be inlined and fail opaquely deep in
                # the model call on a text/vision-only tier, instead of the clear message below.
                if not self._supports_audio():
                    raise ValueError(_AUDIO_UNAVAILABLE_MSG)
                logger.info("Detected audio file, fetching bytes to inline as base64...")
                content_type = _AUDIO_EXT_TO_MIME.get(_get_file_extension(file_url), "audio/mpeg")

                content = await _fetch_bytes_capped(
                    client,
                    file_url,
                    MAX_AUDIO_FETCH_BYTES,
                    f"Audio file too large. Maximum is {MAX_AUDIO_FETCH_BYTES:,} bytes.",
                )

                filename = file_url.split("?")[0].rsplit("/", 1)[-1] or "audio"
                file_block: FileContentBlock = {
                    "type": "file",
                    "base64": base64.b64encode(content).decode("ascii"),
                    "mime_type": content_type,
                    "filename": filename,
                }
                content_blocks.append(file_block)

            elif file_type == "video":
                # KNOWN GAP: video is not supported through the current gateway path. Reject with
                # a clear message rather than emitting a block that fails opaquely deep in the
                # model call. The blocker is TRANSPORT, not model capability (the Gemini tier can
                # do video): a URL `file` block is rejected at payload build ("file URLs ... with
                # Chat Completions", ADR-0001's ChatOpenAI wire format), and base64 doesn't scale
                # to video (Gemini inline ~20MB, request cap 32MB) — so neither form we can send
                # works. Real support is an upload pipeline: Vertex requires a gs:// (or Gemini
                # File API) URI — it won't fetch our S3 presigned URLs — so the file must be
                # staged into a Gemini-reachable location first. See AGENTS.md ("the Video Gap").
                # (Audio is kept above: small enough to base64, and the Gemini tier handles it.)
                logger.info("Rejecting unsupported video file: %s", file_url[:80])
                raise ValueError(_VIDEO_UNSUPPORTED_MSG)

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
            config = RunnableConfig(tags=tags)
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
        2. Other file blocks (audio, documents) routed through _fetch_files for content-type
           detection and base64 inlining; video blocks reach _fetch_files only to be rejected
           (unsupported through the gateway path)
        3. Regex URL fallback when no file blocks are present (user-typed URLs)

        S3 URI validation is handled by the base _extract_and_validate_blocks().

        Raises:
            ValueError: For user-facing input issues (S3 URIs, no URLs, no processable files,
                unsupported media — see _reject_unsupported_media)
            httpx.HTTPStatusError: For HTTP errors during file fetching
            httpx.TimeoutException: For timeout during file fetching
        """
        # Fail fast on media this agent can't process, with a clear message — before the base
        # block-strip silently converts it to a text description ("No processable files").
        self._reject_unsupported_media(input_data)

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
