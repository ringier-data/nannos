"""Tests for preprocess_file_blocks_for_chat_completions (file URL → inline base64).

The gateway speaks the OpenAI protocol, so every request is assembled by
``langchain_openai`` against the Chat Completions spec — which has no URL source for
file blocks. Without inlining, ``langchain_core`` raises while building the payload:
``ValueError: OpenAI Chat Completions does not support file URLs``.
"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages.block_translators.openai import convert_to_openai_data_block

from ringier_a2a_sdk.utils.bedrock_image_processor import (
    _MAX_INLINE_BYTES,
    preprocess_file_blocks_for_chat_completions,
)


def _mock_httpx(payload: bytes):
    mock_response = MagicMock()
    mock_response.content = payload
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestChatCompletionsFileBlocks:
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
            result = await preprocess_file_blocks_for_chat_completions(blocks)

        # text + URL-reference text + inlined file
        assert len(result) == 3
        assert result[0] == {"type": "text", "text": "summarize this"}
        assert result[1]["type"] == "text"
        assert "leads.xlsx" in result[1]["text"]
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
            converted = await preprocess_file_blocks_for_chat_completions([block])

        formatted = convert_to_openai_data_block(converted[-1], api="chat/completions")
        assert formatted["type"] == "file"
        assert formatted["file"]["file_data"].startswith("data:application/pdf;base64,")
        assert formatted["file"]["filename"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_image_urls_are_left_alone(self):
        """Image URLs map to image_url and must not be inlined (or fetched)."""
        blocks = [{"type": "image", "url": "https://example.com/shot.png", "mime_type": "image/png"}]

        with patch("httpx.AsyncClient", side_effect=AssertionError("must not download images")):
            result = await preprocess_file_blocks_for_chat_completions(blocks)

        assert result == blocks

    @pytest.mark.asyncio
    async def test_already_inline_file_passes_through(self):
        blocks = [{"type": "file", "base64": "eA==", "mime_type": "application/pdf", "filename": "a.pdf"}]
        result = await preprocess_file_blocks_for_chat_completions(blocks)
        assert result == blocks

    @pytest.mark.asyncio
    async def test_download_failure_degrades_to_text(self):
        """A file that cannot be fetched must not fail the turn."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("boom"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        blocks = [{"type": "file", "url": "https://example.com/report.pdf", "mime_type": "application/pdf"}]
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await preprocess_file_blocks_for_chat_completions(blocks)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "could not load" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_unresolvable_mime_type_degrades_to_text(self):
        """No MIME type and no extension to guess from → text description, no crash."""
        blocks = [{"type": "file", "url": "https://example.com/attachment"}]

        with patch("httpx.AsyncClient", side_effect=AssertionError("must not download")):
            result = await preprocess_file_blocks_for_chat_completions(blocks)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "not loaded" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_oversized_file_degrades_to_text(self):
        """Past the inline ceiling the file is described, not embedded."""
        blocks = [{"type": "file", "url": "https://example.com/huge.pdf", "mime_type": "application/pdf"}]
        oversized = b"x" * (_MAX_INLINE_BYTES + 1)

        with patch("httpx.AsyncClient", return_value=_mock_httpx(oversized)):
            result = await preprocess_file_blocks_for_chat_completions(blocks)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "too large to attach" in result[0]["text"]
