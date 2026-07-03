"""Tests for FileAnalyzerRunnable._fetch_files document handling.

Documents (PDFs) must be inlined as base64, not forwarded as a URL: the Chat Completions
wire format the gateway speaks rejects file *URLs* (only base64/file_id file sources are
accepted), so a url-carrying file block fails during payload construction. These tests lock
in that documents come back as base64 file blocks and that oversized PDFs are rejected.
"""

import base64

import pytest
from app.agents import file_analyzer
from app.agents.file_analyzer import MAX_DOC_FETCH_BYTES, FileAnalyzerRunnable


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeStreamResponse:
    """Mirrors the slice of httpx's streaming response that _fetch_bytes_capped uses."""

    def __init__(self, content: bytes, *, send_content_length: bool = True):
        self._content = content
        self.headers = {"content-length": str(len(content))} if send_content_length else {}

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


class _FakeStreamCtx:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeClient:
    def __init__(self, content: bytes, *, send_content_length: bool = True):
        self._content = content
        self._send_content_length = send_content_length

    async def get(self, url, **kwargs):
        return _FakeResponse(self._content)

    def stream(self, method, url, **kwargs):
        return _FakeStreamCtx(
            _FakeStreamResponse(self._content, send_content_length=self._send_content_length)
        )


@pytest.fixture
def _stub_detection(monkeypatch):
    """Force document detection and bypass the SSRF guard for these unit tests."""

    async def _noop_assert(_url):
        return None

    async def _detect_document(_url, _client):
        return "document"

    monkeypatch.setattr(file_analyzer, "_assert_public_url", _noop_assert)
    monkeypatch.setattr(file_analyzer, "_detect_file_type", _detect_document)


@pytest.mark.asyncio
async def test_document_is_inlined_as_base64_not_url(_stub_detection):
    pdf_bytes = b"%PDF-1.7\nfake pdf body\n%%EOF"
    runnable = FileAnalyzerRunnable()

    blocks = await runnable._fetch_files(
        ["https://example.com/uploads/Report%20Final.pdf?sig=abc"],
        _FakeClient(pdf_bytes),
    )

    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "file"
    assert "url" not in block  # the whole point: no URL survives to the wire payload
    assert block.get("mime_type") == "application/pdf"
    assert block.get("base64") == base64.b64encode(pdf_bytes).decode("ascii")
    assert block.get("filename") == "Report%20Final.pdf"


@pytest.mark.asyncio
async def test_oversized_document_is_rejected(_stub_detection):
    """Server declares Content-Length over the cap → rejected before the body is read."""
    runnable = FileAnalyzerRunnable()
    too_big = b"x" * (MAX_DOC_FETCH_BYTES + 1)

    with pytest.raises(ValueError, match="PDF too large"):
        await runnable._fetch_files(["https://example.com/huge.pdf"], _FakeClient(too_big))


@pytest.mark.asyncio
async def test_oversized_document_rejected_without_content_length(_stub_detection):
    """No/lying Content-Length → the incremental streaming read still caps memory and rejects
    once the accumulated body exceeds the limit (never buffering the whole thing first)."""
    runnable = FileAnalyzerRunnable()
    too_big = b"x" * (MAX_DOC_FETCH_BYTES + 1)

    with pytest.raises(ValueError, match="PDF too large"):
        await runnable._fetch_files(
            ["https://example.com/huge.pdf"],
            _FakeClient(too_big, send_content_length=False),
        )


@pytest.mark.asyncio
async def test_video_is_rejected_with_clear_message(monkeypatch):
    """Video isn't supported through the gateway path (no media-URL in Chat Completions,
    base64 doesn't scale). Reject with a clear, user-facing message rather than emitting a
    block that fails opaquely inside the model call. Audio, by contrast, is kept."""

    async def _noop_assert(_url):
        return None

    async def _detect_video(_url, _client):
        return "video"

    monkeypatch.setattr(file_analyzer, "_assert_public_url", _noop_assert)
    monkeypatch.setattr(file_analyzer, "_detect_file_type", _detect_video)

    runnable = FileAnalyzerRunnable()
    with pytest.raises(ValueError, match="Video files aren't supported"):
        await runnable._fetch_files(["https://example.com/clip.mp4"], _FakeClient(b""))


@pytest.mark.asyncio
async def test_audio_is_inlined_as_base64_not_url(monkeypatch):
    """Audio is a first-class chat input in this fleet — it must NOT be rejected. Like PDFs it
    is inlined as base64 (a URL file block is rejected by the Chat Completions translator);
    the resolved model must be audio-capable (e.g. Gemini)."""

    async def _noop_assert(_url):
        return None

    async def _detect_audio(_url, _client):
        return "audio"

    monkeypatch.setattr(file_analyzer, "_assert_public_url", _noop_assert)
    monkeypatch.setattr(file_analyzer, "_detect_file_type", _detect_audio)
    # Audio inlining requires an audio-capable resolved model (the _fetch_files capability gate).
    monkeypatch.setattr(file_analyzer, "get_default_fast_model", lambda: "gemini-x")
    monkeypatch.setattr(
        file_analyzer, "get_model_input_capabilities",
        lambda _m: ["text", "image", "audio", "file"],
    )

    audio_bytes = b"OggS\x00fake-opus-voice-recording"
    runnable = FileAnalyzerRunnable()
    blocks = await runnable._fetch_files(
        ["https://example.com/voice.ogg?sig=x"], _FakeClient(audio_bytes)
    )

    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "file"
    assert "url" not in block  # URL file blocks are rejected on the wire
    assert block.get("mime_type") == "audio/ogg"
    assert block.get("base64") == base64.b64encode(audio_bytes).decode("ascii")
    assert block.get("filename") == "voice.ogg"


@pytest.mark.asyncio
async def test_audio_via_fetch_files_rejected_on_non_audio_model(monkeypatch):
    """The capability gate must hold in _fetch_files too, not only in the preflight: audio can be
    discovered here for the first time (a user-pasted URL has no typed block for the preflight to
    inspect). On a text/vision-only tier it must raise the clear message rather than inline the
    audio and fail opaquely deep in the model call."""

    async def _noop_assert(_url):
        return None

    async def _detect_audio(_url, _client):
        return "audio"

    monkeypatch.setattr(file_analyzer, "_assert_public_url", _noop_assert)
    monkeypatch.setattr(file_analyzer, "_detect_file_type", _detect_audio)
    monkeypatch.setattr(file_analyzer, "get_default_fast_model", lambda: "claude-x")
    monkeypatch.setattr(
        file_analyzer, "get_model_input_capabilities", lambda _m: ["text", "image", "file"]
    )

    runnable = FileAnalyzerRunnable()
    with pytest.raises(ValueError, match="Audio transcription isn't available"):
        await runnable._fetch_files(
            ["https://example.com/voice.ogg?sig=x"], _FakeClient(b"OggS\x00fake")
        )


# --- Capability gate: honest advertised modes + preflight rejection ---
# get_supported_input_modes narrows the model's declared modes to what the file-analyzer can
# handle: video is always dropped; audio/file only when the model declares them. Unsupported
# media is rejected up front with a clear message (not the generic "No processable files").

from types import SimpleNamespace  # noqa: E402


def _input_with_blocks(*blocks):
    return SimpleNamespace(messages=[SimpleNamespace(content=list(blocks))])


def test_supported_modes_drops_video_keeps_declared_audio(monkeypatch):
    monkeypatch.setattr(file_analyzer, "get_default_fast_model", lambda: "gemini-x")
    monkeypatch.setattr(
        file_analyzer, "get_model_input_capabilities",
        lambda _m: ["text", "image", "audio", "video", "file"],
    )
    modes = FileAnalyzerRunnable().get_supported_input_modes()
    assert "video" not in modes  # gateway can't carry video
    assert modes == ["text", "image", "file", "audio"]


def test_supported_modes_excludes_audio_when_model_lacks_it(monkeypatch):
    monkeypatch.setattr(file_analyzer, "get_default_fast_model", lambda: "claude-x")
    monkeypatch.setattr(
        file_analyzer, "get_model_input_capabilities", lambda _m: ["text", "image", "file"]
    )
    modes = FileAnalyzerRunnable().get_supported_input_modes()
    assert modes == ["text", "image", "file"]
    assert not FileAnalyzerRunnable()._supports_audio()


def test_preflight_rejects_video(monkeypatch):
    monkeypatch.setattr(file_analyzer, "get_default_fast_model", lambda: "gemini-x")
    monkeypatch.setattr(
        file_analyzer, "get_model_input_capabilities",
        lambda _m: ["text", "image", "audio", "file"],
    )
    runnable = FileAnalyzerRunnable()
    with pytest.raises(ValueError, match="Video files aren't supported"):
        runnable._reject_unsupported_media(
            _input_with_blocks({"type": "video", "url": "https://x/clip.mp4"})
        )


def test_preflight_rejects_audio_when_model_not_audio_capable(monkeypatch):
    monkeypatch.setattr(file_analyzer, "get_default_fast_model", lambda: "claude-x")
    monkeypatch.setattr(
        file_analyzer, "get_model_input_capabilities", lambda _m: ["text", "image", "file"]
    )
    runnable = FileAnalyzerRunnable()
    with pytest.raises(ValueError, match="Audio transcription isn't available"):
        runnable._reject_unsupported_media(
            _input_with_blocks({"type": "audio", "url": "https://x/voice.webm"})
        )


def test_preflight_allows_audio_when_model_is_audio_capable(monkeypatch):
    monkeypatch.setattr(file_analyzer, "get_default_fast_model", lambda: "gemini-x")
    monkeypatch.setattr(
        file_analyzer, "get_model_input_capabilities",
        lambda _m: ["text", "image", "audio", "file"],
    )
    runnable = FileAnalyzerRunnable()
    # No raise: audio is allowed through to fetching when the model supports it.
    runnable._reject_unsupported_media(
        _input_with_blocks({"type": "audio", "url": "https://x/voice.webm"})
    )
