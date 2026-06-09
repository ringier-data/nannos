"""Tests for AttachmentsStoreBackend."""

from unittest.mock import patch

import pytest

from agent_common.backends.attachments_store import Attachment, AttachmentsStoreBackend


def _text_backend() -> AttachmentsStoreBackend:
    return AttachmentsStoreBackend(
        [
            Attachment(filename="notes.txt", mime_type="text/plain", inline_bytes=b"line one\nline two\n"),
            Attachment(filename="data.md", mime_type="text/markdown", inline_bytes=b"# Title\nbody text\n"),
        ]
    )


# ---- aread: text ----


@pytest.mark.asyncio
async def test_read_text_attachment():
    backend = _text_backend()
    result = await backend.aread("/attachments/notes.txt")
    assert result.error is None
    assert result.file_data["encoding"] == "utf-8"
    assert result.file_data["content"] == "line one\nline two\n"


@pytest.mark.asyncio
async def test_read_text_without_prefix():
    backend = _text_backend()
    result = await backend.aread("notes.txt")
    assert result.error is None
    assert "line one" in result.file_data["content"]


@pytest.mark.asyncio
async def test_read_text_offset_limit():
    backend = _text_backend()
    result = await backend.aread("/attachments/notes.txt", offset=1, limit=1)
    assert result.file_data["content"] == "line two\n"


# ---- aread: binary ----


@pytest.mark.asyncio
async def test_read_binary_pdf_returns_base64():
    import base64

    pdf_bytes = b"%PDF-1.4 binary\x00\x01\x02content"
    backend = AttachmentsStoreBackend(
        [Attachment(filename="report.pdf", mime_type="application/pdf", inline_bytes=pdf_bytes)]
    )
    result = await backend.aread("/attachments/report.pdf")
    assert result.error is None
    assert result.file_data["encoding"] == "base64"
    assert base64.b64decode(result.file_data["content"]) == pdf_bytes


@pytest.mark.asyncio
async def test_read_undecodable_text_falls_back_to_base64():
    import base64

    raw = b"\xff\xfe\x00garbage"
    backend = AttachmentsStoreBackend([Attachment(filename="weird.txt", mime_type="text/plain", inline_bytes=raw)])
    result = await backend.aread("/attachments/weird.txt")
    assert result.file_data["encoding"] == "base64"
    assert base64.b64decode(result.file_data["content"]) == raw


# ---- aread: errors ----


@pytest.mark.asyncio
async def test_read_missing_attachment():
    backend = _text_backend()
    result = await backend.aread("/attachments/missing.txt")
    assert result.error is not None
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_read_directory_path_is_invalid():
    backend = _text_backend()
    result = await backend.aread("/attachments/")
    assert result.error is not None


# ---- URL fetching ----


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self, content: bytes, *args, **kwargs):
        self._content = content
        self.requested_url: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        self.requested_url = url
        return _FakeResponse(self._content)


@pytest.mark.asyncio
async def test_read_fetches_from_url_and_caches():
    fetched = b"remote text content\n"
    backend = AttachmentsStoreBackend(
        [Attachment(filename="remote.txt", mime_type="text/plain", url="https://s3.example/remote.txt?sig=x")]
    )

    calls = {"count": 0}

    def _factory(*args, **kwargs):
        calls["count"] += 1
        return _FakeClient(fetched)

    with patch("agent_common.backends.attachments_store.httpx.AsyncClient", _factory):
        first = await backend.aread("/attachments/remote.txt")
        second = await backend.aread("/attachments/remote.txt")

    assert first.file_data["content"] == "remote text content\n"
    assert second.file_data["content"] == "remote text content\n"
    # Second read served from cache → only one network fetch.
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_read_url_fetch_error_surfaces():
    backend = AttachmentsStoreBackend(
        [Attachment(filename="remote.txt", mime_type="text/plain", url="https://s3.example/remote.txt")]
    )

    def _factory(*args, **kwargs):
        raise RuntimeError("boom")

    with patch("agent_common.backends.attachments_store.httpx.AsyncClient", _factory):
        result = await backend.aread("/attachments/remote.txt")

    assert result.error is not None
    assert "could not read" in result.error.lower()


@pytest.mark.asyncio
async def test_size_guard():
    big = b"x" * (26 * 1024 * 1024)
    backend = AttachmentsStoreBackend(
        [Attachment(filename="big.bin", mime_type="application/octet-stream", inline_bytes=big)]
    )
    result = await backend.aread("/attachments/big.bin")
    assert result.error is not None
    assert "exceeds" in result.error.lower()


# ---- als / aglob / agrep ----


@pytest.mark.asyncio
async def test_ls_lists_attachments():
    backend = _text_backend()
    result = await backend.als("/attachments/")
    paths = {e["path"] for e in result.entries}
    assert paths == {"/notes.txt", "/data.md"}


@pytest.mark.asyncio
async def test_glob_matches():
    backend = _text_backend()
    result = await backend.aglob("*.md")
    paths = {e["path"] for e in result.matches}
    assert paths == {"/attachments/data.md"}


@pytest.mark.asyncio
async def test_grep_searches_text_only():
    pdf = b"%PDF needle here"
    backend = AttachmentsStoreBackend(
        [
            Attachment(filename="notes.txt", mime_type="text/plain", inline_bytes=b"a needle in text\n"),
            Attachment(filename="report.pdf", mime_type="application/pdf", inline_bytes=pdf),
        ]
    )
    result = await backend.agrep("needle")
    matched_paths = {m["path"] for m in result.matches}
    # Only the text attachment is searched; the PDF is skipped.
    assert matched_paths == {"/attachments/notes.txt"}


# ---- write/edit blocked ----


@pytest.mark.asyncio
async def test_write_blocked():
    backend = _text_backend()
    result = await backend.awrite("/attachments/x.txt", "data")
    assert result.error is not None
    assert "read-only" in result.error.lower()


@pytest.mark.asyncio
async def test_edit_blocked():
    backend = _text_backend()
    result = await backend.aedit("/attachments/notes.txt", "line one", "LINE ONE")
    assert result.error is not None
    assert "read-only" in result.error.lower()


# ---- helpers ----


@pytest.mark.asyncio
async def test_list_recursive():
    backend = _text_backend()
    paths = await backend.alist_recursive()
    assert set(paths) == {"/attachments/notes.txt", "/attachments/data.md"}


def test_parse_path_rejects_nested():
    assert AttachmentsStoreBackend._parse_path("/attachments/sub/file.txt") is None
    assert AttachmentsStoreBackend._parse_path("/attachments/") is None
    assert AttachmentsStoreBackend._parse_path("/attachments/file.txt") == "file.txt"


def test_duplicate_filename_last_wins():
    backend = AttachmentsStoreBackend(
        [
            Attachment(filename="a.txt", inline_bytes=b"first"),
            Attachment(filename="a.txt", inline_bytes=b"second"),
        ]
    )
    assert len(backend._attachments) == 1


# ---- aread_text ----


@pytest.mark.asyncio
async def test_aread_text_returns_full_text():
    backend = _text_backend()
    assert await backend.aread_text("/attachments/notes.txt") == "line one\nline two\n"


@pytest.mark.asyncio
async def test_aread_text_unknown_returns_none():
    backend = _text_backend()
    assert await backend.aread_text("/attachments/missing.txt") is None


@pytest.mark.asyncio
async def test_aread_text_undecodable_returns_none():
    backend = AttachmentsStoreBackend([Attachment(filename="img.bin", inline_bytes=b"\xff\xfe\x00\x01")])
    assert await backend.aread_text("/attachments/img.bin") is None


# ---- build_attachments_backend_from_blocks ----


def test_build_from_blocks_uses_url_basename():
    from agent_common.backends.attachments_store import build_attachments_backend_from_blocks

    blocks = [
        {"type": "file", "url": "https://example.com/files/pg1012-779bbd53.txt?sig=abc", "mime_type": "text/plain"},
        {"type": "image", "url": "https://example.com/x/photo.png", "mime_type": "image/png"},
    ]
    backend = build_attachments_backend_from_blocks(blocks)
    assert backend is not None
    assert set(backend._attachments.keys()) == {"pg1012-779bbd53.txt", "photo.png"}


def test_build_from_blocks_decodes_inline_base64():
    import base64

    from agent_common.backends.attachments_store import build_attachments_backend_from_blocks

    b64 = base64.b64encode(b"hello").decode()
    blocks = [{"type": "file", "base64": b64, "mime_type": "text/plain", "filename": "inline.txt"}]
    backend = build_attachments_backend_from_blocks(blocks)
    assert backend is not None
    att = backend._attachments["inline.txt"]
    assert att.inline_bytes == b"hello"


def test_build_from_blocks_skips_non_servable_and_returns_none():
    from agent_common.backends.attachments_store import build_attachments_backend_from_blocks

    # No url, no base64 → nothing servable
    assert build_attachments_backend_from_blocks([{"type": "file", "mime_type": "text/plain"}]) is None
    assert build_attachments_backend_from_blocks([]) is None
    assert build_attachments_backend_from_blocks("not a list") is None


def test_build_from_blocks_deduplicates_filenames():
    from agent_common.backends.attachments_store import build_attachments_backend_from_blocks

    blocks = [
        {"type": "file", "url": "https://example.com/a/doc.txt", "mime_type": "text/plain"},
        {"type": "file", "url": "https://example.com/b/doc.txt", "mime_type": "text/plain"},
    ]
    backend = build_attachments_backend_from_blocks(blocks)
    assert backend is not None
    assert len(backend._attachments) == 2


# ---- ContextScopedAttachmentsBackend + contextvar ----


@pytest.mark.asyncio
async def test_context_scoped_backend_delegates_to_registered():
    from agent_common.backends.attachments_store import (
        ContextScopedAttachmentsBackend,
        reset_current_attachments_backend,
        set_current_attachments_backend,
    )

    proxy = ContextScopedAttachmentsBackend()
    live = _text_backend()
    token = set_current_attachments_backend(live)
    try:
        result = await proxy.aread("/attachments/notes.txt")
        assert "line one" in result.file_data["content"]
        ls = await proxy.als("/attachments/")
        assert any("notes.txt" in e["path"] for e in ls.entries)
    finally:
        reset_current_attachments_backend(token)


@pytest.mark.asyncio
async def test_context_scoped_backend_empty_when_unset():
    from agent_common.backends.attachments_store import ContextScopedAttachmentsBackend

    proxy = ContextScopedAttachmentsBackend()
    ls = await proxy.als("/attachments/")
    assert ls.entries == []


@pytest.mark.asyncio
async def test_context_scoped_backend_write_blocked():
    from agent_common.backends.attachments_store import ContextScopedAttachmentsBackend

    proxy = ContextScopedAttachmentsBackend()
    result = await proxy.awrite("/attachments/x.txt", "data")
    assert result.error is not None
