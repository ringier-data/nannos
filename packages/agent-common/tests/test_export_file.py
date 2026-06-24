"""Tests for _export_file_impl (the docstore_export tool implementation).

Regression coverage for the bug where exporting a file whose stored `content`
is a deepagents v2 plain `str` (rather than the legacy v1 `list[str]`) produced
a corrupted upload — `"\n".join(some_string)` splits the string per-character,
inserting a newline between every character.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_common.core.document_store_tools import _export_file_impl


def _make_store_item(content):
    item = MagicMock()
    item.value = {"content": content}
    return item


async def _run_export(content):
    """Export a /memories/ file with the given stored content; return uploaded bytes."""
    store = AsyncMock()
    store.aget.return_value = _make_store_item(content)

    storage = AsyncMock()
    storage.generate_presigned_url.return_value = "https://example.com/presigned"

    result = await _export_file_impl(
        file_path="/memories/notes.md",
        user_id="user-1",
        store=store,
        storage=storage,
        s3_bucket="bucket",
    )

    assert "Download link" in result
    storage.upload.assert_awaited_once()
    uploaded = storage.upload.await_args.kwargs["content"]
    return uploaded.decode("utf-8")


class TestExportFileContentFormats:
    @pytest.mark.asyncio
    async def test_v2_string_content_is_not_split_per_character(self):
        # deepagents v2 (default) stores content as a plain str.
        uploaded = await _run_export("# Title\nHello world\nSecond line")
        assert uploaded == "# Title\nHello world\nSecond line"
        # Guard against the regression: no newline injected between every char.
        assert "\nH\ne\nl\nl\no" not in uploaded

    @pytest.mark.asyncio
    async def test_v1_list_content_is_joined_with_newlines(self):
        # Legacy v1 format stores content as list[str] (lines).
        uploaded = await _run_export(["# Title", "Hello world", "Second line"])
        assert uploaded == "# Title\nHello world\nSecond line"
