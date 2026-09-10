"""Tests for preprocess_blocks_for_chat_completions (URL → inline base64).

The gateway speaks the OpenAI protocol, so every request is assembled by
``langchain_openai`` against the Chat Completions spec — which has no URL source for
file blocks. Without inlining, ``langchain_core`` raises while building the payload:
``ValueError: OpenAI Chat Completions does not support file URLs``.
"""

import base64
from unittest.mock import AsyncMock, patch

import pytest
from httpx_stubs import allow_all_urls, stub_httpx_client
from langchain_core.messages.block_translators.openai import convert_to_openai_data_block

from ringier_a2a_sdk.utils.bedrock_image_processor import (
    _MAX_INLINE_BYTES,
    preprocess_blocks_for_chat_completions,
)


def _mock_httpx(payload: bytes):
    return stub_httpx_client(payload)


class TestChatCompletionsFileBlocks:
    @pytest.fixture(autouse=True)
    def _stub_ssrf_guard(self):
        """The guard resolves hostnames for real; these tests stub the transport."""
        with allow_all_urls():
            yield

    @pytest.mark.asyncio
    async def test_url_file_block_is_inlined_with_filename(self):
        """A URL-sourced file block becomes base64 and keeps its extension."""
        blocks = [
            {"type": "text", "text": "summarize this"},
            {
                "type": "file",
                "url": "https://example.com/leads.xlsx?X-Amz-Signature=abc",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "filename": "leads.xlsx",
            },
        ]
        payload = b"PK\x03\x04 fake xlsx"

        with patch("httpx.AsyncClient", return_value=_mock_httpx(payload)):
            result = await preprocess_blocks_for_chat_completions(blocks)

        # text + URL-reference text + inlined file
        assert len(result) == 3
        assert result[0] == {"type": "text", "text": "summarize this"}
        assert result[1]["type"] == "text"
        assert "leads.xlsx" in result[1]["text"]
        # The signed URL must not be written back into the message: it would land in
        # the checkpoint and traces, and the model mangles long URLs when it echoes
        # them (the reason content_builder._describe_file omits raw URIs).
        assert "X-Amz-Signature" not in result[1]["text"]
        assert "https://" not in result[1]["text"]
        assert result[2]["type"] == "file"
        assert result[2]["base64"] == base64.b64encode(payload).decode("utf-8")
        # Unlike the Bedrock path, the extension is preserved — OpenAI identifies the
        # file type from the filename.
        assert result[2]["filename"] == "leads.xlsx"

    @pytest.mark.asyncio
    async def test_inlined_block_serializes_for_chat_completions(self):
        """The regression itself: the converted block must survive the OpenAI translator."""
        block = {"type": "file", "url": "https://example.com/report.pdf", "mime_type": "application/pdf"}

        with pytest.raises(ValueError, match="does not support file URLs"):
            convert_to_openai_data_block(block, api="chat/completions")

        with patch("httpx.AsyncClient", return_value=_mock_httpx(b"%PDF-1.4 fake")):
            converted = await preprocess_blocks_for_chat_completions([block])

        formatted = convert_to_openai_data_block(converted[-1], api="chat/completions")
        assert formatted["type"] == "file"
        assert formatted["file"]["file_data"].startswith("data:application/pdf;base64,")
        assert formatted["file"]["filename"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_image_urls_are_left_alone(self):
        """Image URLs map to image_url and must not be inlined (or fetched)."""
        blocks = [{"type": "image", "url": "https://example.com/shot.png", "mime_type": "image/png"}]

        with patch("httpx.AsyncClient", side_effect=AssertionError("must not download images")):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert result == blocks

    @pytest.mark.asyncio
    async def test_already_inline_file_passes_through(self):
        blocks = [{"type": "file", "base64": "eA==", "mime_type": "application/pdf", "filename": "a.pdf"}]
        result = await preprocess_blocks_for_chat_completions(blocks)
        assert result == blocks

    @pytest.mark.asyncio
    async def test_download_failure_degrades_to_text(self):
        """A file that cannot be fetched must not fail the turn."""
        blocks = [{"type": "file", "url": "https://example.com/report.pdf", "mime_type": "application/pdf"}]
        with patch("httpx.AsyncClient", return_value=stub_httpx_client(error=Exception("boom"))):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "could not load" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_unresolvable_mime_type_degrades_to_text(self):
        """No MIME type and no extension to guess from → text description, no crash."""
        blocks = [{"type": "file", "url": "https://example.com/attachment"}]

        with patch("httpx.AsyncClient", side_effect=AssertionError("must not download")):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "not loaded" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_oversized_file_degrades_to_text(self):
        """Past the inline ceiling the file is described, not embedded."""
        blocks = [{"type": "file", "url": "https://example.com/huge.pdf", "mime_type": "application/pdf"}]
        oversized = b"x" * (_MAX_INLINE_BYTES + 1)

        with patch("httpx.AsyncClient", return_value=_mock_httpx(oversized)):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "too large to attach" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_oversized_content_length_is_refused_before_reading_body(self):
        """The declared size short-circuits: no body bytes are pulled down."""
        blocks = [{"type": "file", "url": "https://example.com/huge.pdf", "mime_type": "application/pdf"}]
        client = stub_httpx_client(b"never read", headers={"content-length": str(_MAX_INLINE_BYTES + 1)})

        with patch("httpx.AsyncClient", return_value=client):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert result[0]["type"] == "text"
        assert "too large to attach" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_oversized_body_without_content_length_aborts_mid_stream(self):
        """A missing (or lying) Content-Length is caught by the running byte tally."""
        blocks = [{"type": "file", "url": "https://example.com/huge.pdf", "mime_type": "application/pdf"}]
        client = stub_httpx_client(b"x" * (_MAX_INLINE_BYTES + 1), headers={})

        with patch("httpx.AsyncClient", return_value=client):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert result[0]["type"] == "text"
        assert "too large to attach" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_extensionless_url_gets_extension_from_mime_type(self):
        """OpenAI types the file by extension, so an opaque object key must gain one."""
        blocks = [{"type": "file", "url": "https://example.com/objects/9f3a1c", "mime_type": "application/pdf"}]

        with patch("httpx.AsyncClient", return_value=_mock_httpx(b"%PDF-1.4 fake")):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert result[-1]["filename"] == "9f3a1c.pdf"

    @pytest.mark.asyncio
    async def test_trailing_slash_url_still_yields_a_filename(self):
        blocks = [{"type": "file", "url": "https://example.com/files/", "mime_type": "application/pdf"}]

        with patch("httpx.AsyncClient", return_value=_mock_httpx(b"%PDF-1.4 fake")):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert result[-1]["filename"] == "unknown.pdf"

    @pytest.mark.asyncio
    async def test_audio_url_is_inlined(self):
        """`input_audio` is base64-only: a URL raises "Key base64 is required for audio blocks"."""
        block = {"type": "audio", "url": "https://example.com/clip.mp3", "mime_type": "audio/mpeg"}

        with pytest.raises(ValueError, match="base64 is required for audio"):
            convert_to_openai_data_block(block, api="chat/completions")

        with patch("httpx.AsyncClient", return_value=_mock_httpx(b"ID3 fake mp3")):
            result = await preprocess_blocks_for_chat_completions([block])

        assert result[-1]["type"] == "audio"
        assert result[-1]["base64"] == base64.b64encode(b"ID3 fake mp3").decode("utf-8")
        assert convert_to_openai_data_block(result[-1], api="chat/completions")["type"] == "input_audio"

    @pytest.mark.asyncio
    async def test_video_degrades_to_text_without_downloading(self):
        """No video representation exists in Chat Completions, inline or by URL."""
        block = {"type": "video", "url": "https://example.com/clip.mp4", "mime_type": "video/mp4"}

        with pytest.raises(ValueError, match="video is not supported"):
            convert_to_openai_data_block(block, api="chat/completions")

        with patch("httpx.AsyncClient", side_effect=AssertionError("must not download video")):
            result = await preprocess_blocks_for_chat_completions([block])

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "clip.mp4" in result[0]["text"]
        assert "not supported" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_inline_video_also_degrades(self):
        """Even an already-inlined video has nowhere to go, so it must be described."""
        blocks = [{"type": "video", "base64": "eA==", "mime_type": "video/mp4", "filename": "clip.mp4"}]
        result = await preprocess_blocks_for_chat_completions(blocks)

        assert result[0]["type"] == "text"
        assert "clip.mp4" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_non_public_url_is_refused(self):
        """The SSRF guard runs before the fetch, and a refusal degrades to text."""
        from ringier_a2a_sdk.utils import bedrock_image_processor
        from ringier_a2a_sdk.utils.url_fetch import SSRFError

        blocks = [{"type": "file", "url": "http://169.254.169.254/latest/meta-data", "mime_type": "application/pdf"}]
        guard = AsyncMock(side_effect=SSRFError("non-public"))

        with patch.object(bedrock_image_processor, "assert_public_url", guard):
            with patch("httpx.AsyncClient", side_effect=AssertionError("must not fetch a blocked URL")):
                result = await preprocess_blocks_for_chat_completions(blocks)

        assert result[0]["type"] == "text"
        assert "blocked" in result[0]["text"]
        # The blocked URL must not be echoed back into the conversation.
        assert "169.254.169.254" not in result[0]["text"]

    @pytest.mark.asyncio
    async def test_degraded_blocks_do_not_leak_the_url_either(self):
        """Every degradation path names the file without echoing the signed URL."""
        blocks = [
            {
                "type": "file",
                "url": "https://example.com/report.pdf?X-Amz-Signature=abc",
                "mime_type": "application/pdf",
            }
        ]
        with patch("httpx.AsyncClient", return_value=stub_httpx_client(error=Exception("boom"))):
            result = await preprocess_blocks_for_chat_completions(blocks)

        assert "report.pdf" in result[0]["text"]
        assert "X-Amz-Signature" not in result[0]["text"]
