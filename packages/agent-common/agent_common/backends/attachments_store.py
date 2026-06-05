"""Read-only, in-memory backend serving the current conversation's attachments.

Files attached by the user to a conversation are normally passed to the LLM as
multi-modal content blocks. That is convenient for direct analysis but is not
enough when a skill or a sandboxed command needs to *access the file on disk*.

This backend mounts those attachments at ``/attachments/{filename}`` so agents
can ``read_file`` / ``grep`` them and ``copy_to_sandbox`` them. It is:

- **Ephemeral**: built per A2A turn from the message's file blocks — nothing is
  persisted to PostgreSQL.
- **Lazy**: file bytes are fetched from the (presigned) URL only on first read
  and cached in memory for the lifetime of the backend instance.
- **Read-only**: writes/edits are rejected; attachments come from the user.
"""

from __future__ import annotations

import base64
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass

import httpx
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import _get_file_type, create_file_data

from agent_common.backends.skills_store import SkillsStoreBackend

logger = logging.getLogger(__name__)

_READ_ONLY_MSG = (
    "/attachments/ is read-only. It contains files attached to the current "
    "conversation. To use one in the sandbox, copy it with copy_to_sandbox."
)

# Network/size guards for lazy attachment fetches.
_DOWNLOAD_TIMEOUT = 30.0
_MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB


@dataclass
class Attachment:
    """An attachment available to the current conversation.

    Exactly one of ``url`` or ``inline_bytes`` should be set as the content
    source. ``inline_bytes`` takes precedence when both are present.
    """

    filename: str
    mime_type: str | None = None
    url: str | None = None
    inline_bytes: bytes | None = None


class AttachmentsStoreBackend(BackendProtocol):
    """Read-only backend serving conversation attachments at /attachments/{name}.

    Instantiated once per agent invocation with the attachments extracted from
    the incoming message. File bytes are fetched lazily and cached in memory.
    """

    def __init__(self, attachments: list[Attachment]):
        # Keep insertion order while de-duplicating by filename.
        self._attachments: dict[str, Attachment] = {}
        for att in attachments:
            self._attachments[att.filename] = att
        self._content_cache: dict[str, bytes] = {}

    # ---- content resolution ----

    async def _get_bytes(self, name: str, att: Attachment) -> bytes:
        """Return the raw bytes for an attachment, fetching/caching on demand."""
        if name in self._content_cache:
            return self._content_cache[name]

        if att.inline_bytes is not None:
            raw = att.inline_bytes
        elif att.url:
            async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
                resp = await client.get(att.url)
                resp.raise_for_status()
                raw = resp.content
        else:
            raise ValueError(f"Attachment '{name}' has no content source")

        if len(raw) > _MAX_ATTACHMENT_SIZE:
            raise ValueError(
                f"Attachment '{name}' is {len(raw) / (1024 * 1024):.1f} MB, "
                f"which exceeds the {_MAX_ATTACHMENT_SIZE // (1024 * 1024)} MB limit"
            )

        self._content_cache[name] = raw
        return raw

    # ---- read operations ----

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        name = self._parse_path(file_path)
        if not name:
            return ReadResult(error=f"Invalid path: {file_path}")

        att = self._attachments.get(name)
        if not att:
            return ReadResult(error=f"Attachment not found: {name}")

        try:
            raw = await self._get_bytes(name, att)
        except Exception as e:
            logger.warning("Failed to fetch attachment '%s': %s", name, e)
            return ReadResult(error=f"Could not read attachment '{name}': {e}")

        # Match the deepagents read tool's extension-based classification so the
        # tool renders text vs. binary (base64) content blocks consistently.
        if _get_file_type(name) != "text":
            return ReadResult(file_data=create_file_data(base64.b64encode(raw).decode("ascii"), encoding="base64"))

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ReadResult(file_data=create_file_data(base64.b64encode(raw).decode("ascii"), encoding="base64"))

        lines = text.splitlines(keepends=True)
        sliced = lines[offset : offset + limit]
        return ReadResult(file_data=create_file_data("".join(sliced), encoding="utf-8"))

    async def als(self, path: str) -> LsResult:
        normalized = path.rstrip("/") + "/"
        if normalized in ("/attachments/", "/"):
            entries = [FileInfo(path=f"/{name}") for name in self._attachments]
            return LsResult(entries=entries)
        return LsResult(error=f"Directory not found: {path}")

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        pattern_lower = pattern.lower()
        glob_re = SkillsStoreBackend._glob_to_regex(glob) if glob else None
        matches: list[GrepMatch] = []

        for name, att in self._attachments.items():
            fpath = f"/attachments/{name}"
            if glob_re and not glob_re.search(fpath):
                continue
            if path and not fpath.startswith(path):
                continue
            # Only search text attachments — avoid downloading large binaries.
            if _get_file_type(name) != "text":
                continue
            try:
                raw = await self._get_bytes(name, att)
                content = raw.decode("utf-8")
            except Exception:
                continue
            for i, line in enumerate(content.splitlines()):
                if pattern_lower in line.lower():
                    matches.append(GrepMatch(path=fpath, line=i + 1, text=line))

        return GrepResult(matches=matches)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        glob_re = SkillsStoreBackend._glob_to_regex(pattern)
        entries: list[FileInfo] = []
        for name in self._attachments:
            fpath = f"/attachments/{name}"
            if fpath.startswith(path) and glob_re.search(fpath):
                entries.append(FileInfo(path=fpath))
        return GlobResult(matches=entries)

    # ---- write operations (blocked) ----

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY_MSG)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=_READ_ONLY_MSG)

    # ---- sync stubs (required by protocol, delegate to async) ----

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        raise NotImplementedError("Use aread()")

    def ls(self, path: str) -> LsResult:
        raise NotImplementedError("Use als()")

    def write(self, file_path: str, content: str) -> WriteResult:
        raise NotImplementedError("Use awrite()")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        raise NotImplementedError("Use aedit()")

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        raise NotImplementedError("Use agrep()")

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        raise NotImplementedError("Use aglob()")

    # ---- helpers ----

    async def alist_recursive(self, prefix: str = "/attachments/") -> list[str]:
        """List all attachment paths (used by sandbox sync enumeration)."""
        return [f"/attachments/{name}" for name in self._attachments]

    async def aread_text(self, file_path: str) -> str | None:
        """Return the full UTF-8 text of an attachment, or ``None``.

        Used by ``semantic_search_file`` to obtain the raw, unsliced content of
        a ``/attachments/`` file for on-demand indexing. Returns ``None`` when
        the path is unknown or the bytes cannot be decoded as text.
        """
        name = self._parse_path(file_path)
        if not name:
            return None
        att = self._attachments.get(name)
        if not att:
            return None
        try:
            raw = await self._get_bytes(name, att)
        except Exception as e:
            logger.warning("Failed to fetch attachment '%s' for indexing: %s", name, e)
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _parse_path(path: str) -> str | None:
        """Parse /attachments/{name} into name. Returns None if it doesn't match."""
        cleaned = path.strip("/")
        if cleaned.startswith("attachments/"):
            cleaned = cleaned[len("attachments/") :]
        elif cleaned == "attachments":
            return None
        # Attachments are flat — reject nested paths.
        if not cleaned or "/" in cleaned:
            return None
        return cleaned


# ---------------------------------------------------------------------------
# Building attachments backends from multi-modal content blocks
# ---------------------------------------------------------------------------


def derive_attachment_filename(
    block: dict, url: str | None, mime_type: str | None, idx: int, used_names: set[str]
) -> str:
    """Derive a stable, unique, flat filename for an attachment block.

    Both the orchestrator and sub-agents use this so a given attachment resolves
    to the same ``/attachments/{filename}`` path everywhere.
    """
    import mimetypes
    import os
    from urllib.parse import unquote, urlparse

    name = block.get("filename") or block.get("name")
    if not name and url:
        path = urlparse(url).path
        name = os.path.basename(unquote(path)) or None
    if not name:
        ext = mimetypes.guess_extension(mime_type or "") or ""
        name = f"attachment_{idx}{ext}"

    # Flatten any path separators and de-duplicate.
    name = name.replace("/", "_").replace("\\", "_").strip() or f"attachment_{idx}"
    if name in used_names:
        stem, ext = os.path.splitext(name)
        name = f"{stem}_{idx}{ext}"
    return name


def build_attachments_backend_from_blocks(blocks: list) -> "AttachmentsStoreBackend | None":
    """Build an ephemeral ``AttachmentsStoreBackend`` from content blocks.

    Each block is a multi-modal content block (dict / TypedDict) carrying either
    a presigned ``url`` or inline ``base64`` data. Blocks that cannot be served
    from disk (no url and no decodable inline data) are skipped.

    Returns ``None`` when there are no servable attachments.
    """
    if not isinstance(blocks, list):
        return None

    attachments: list[Attachment] = []
    used_names: set[str] = set()
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        if block.get("type") not in ("image", "audio", "video", "file"):
            continue

        url = block.get("url")
        b64 = block.get("base64")
        mime_type = block.get("mime_type")
        inline_bytes: bytes | None = None
        if not url and b64:
            try:
                inline_bytes = base64.b64decode(b64)
            except Exception:
                inline_bytes = None
        if not url and inline_bytes is None:
            continue

        filename = derive_attachment_filename(block, url, mime_type, idx, used_names)
        used_names.add(filename)
        attachments.append(Attachment(filename=filename, mime_type=mime_type, url=url, inline_bytes=inline_bytes))

    if not attachments:
        return None

    return AttachmentsStoreBackend(attachments)


# ---------------------------------------------------------------------------
# Per-turn attachments context
# ---------------------------------------------------------------------------
#
# The orchestrator graph is compiled once per model and shared across all users,
# so it cannot bake a per-conversation ``AttachmentsStoreBackend`` into its
# filesystem. Instead it mounts a single ``ContextScopedAttachmentsBackend`` at
# ``/attachments/`` that delegates to whichever backend is registered in this
# context variable for the current A2A turn. Sub-agents register their own
# per-invocation backend here too so that ``semantic_search_file`` (which reads
# content outside the FilesystemMiddleware backend) can reach the attachments.

_current_attachments_backend: ContextVar[AttachmentsStoreBackend | None] = ContextVar(
    "current_attachments_backend", default=None
)


def set_current_attachments_backend(backend: AttachmentsStoreBackend | None) -> Token:
    """Register the attachments backend for the current turn. Returns a reset token."""
    return _current_attachments_backend.set(backend)


def reset_current_attachments_backend(token: Token) -> None:
    """Restore the previous attachments backend using a token from set()."""
    _current_attachments_backend.reset(token)


def get_current_attachments_backend() -> AttachmentsStoreBackend | None:
    """Return the attachments backend registered for the current turn, if any."""
    return _current_attachments_backend.get()


# ---------------------------------------------------------------------------
# Checkpoint-based attachment reconstruction
# ---------------------------------------------------------------------------

_MAX_ATTACHMENT_HISTORY_MESSAGES = 20


def collect_attachment_blocks_from_messages(
    messages: list,
    max_messages: int = _MAX_ATTACHMENT_HISTORY_MESSAGES,
) -> list[dict]:
    """Collect file blocks from the most recent *max_messages* checkpoint messages.

    Walks messages newest-first and accumulates blocks from every turn, stopping
    after *max_messages* have been scanned.  Filenames are de-duplicated — the
    most recent occurrence wins.

    Two storage formats are handled transparently:

    - ``additional_kwargs["file_blocks"]`` — orchestrator style (blocks are
      excluded from the LLM-visible text but kept in the checkpoint).
    - Multimodal ``content`` list — sub-agent style (blocks are passed directly
      to the model as content blocks).
    """
    all_blocks: list[dict] = []
    seen: set[str] = set()

    for i, msg in enumerate(reversed(messages)):
        if i >= max_messages:
            break

        # Orchestrator style: blocks stored outside the LLM-visible content.
        kwargs = getattr(msg, "additional_kwargs", {}) or {}
        prior_blocks: list[dict] | None = kwargs.get("file_blocks")

        # Sub-agent style: blocks embedded in a multimodal content list.
        if not prior_blocks:
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                prior_blocks = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") in ("image", "audio", "video", "file")
                ] or None

        if not prior_blocks:
            continue

        for block in prior_blocks:
            key = block.get("filename") or block.get("name") or block.get("url", "")
            if key not in seen:
                all_blocks.append(block)
                seen.add(key)

    return all_blocks


class ContextScopedAttachmentsBackend(BackendProtocol):
    """Stateless ``/attachments/`` backend that delegates to the current turn's backend.

    A single instance is mounted on the orchestrator's shared ``CompositeBackend``.
    Each operation resolves the live ``AttachmentsStoreBackend`` from the
    ``_current_attachments_backend`` context variable, which is set per A2A turn.
    When no backend is registered, read operations report an empty filesystem.
    """

    def _delegate(self) -> AttachmentsStoreBackend | None:
        return _current_attachments_backend.get()

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        backend = self._delegate()
        if backend is None:
            return ReadResult(error=f"Attachment not found: {file_path}")
        return await backend.aread(file_path, offset=offset, limit=limit)

    async def als(self, path: str) -> LsResult:
        backend = self._delegate()
        if backend is None:
            return LsResult(entries=[])
        return await backend.als(path)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        backend = self._delegate()
        if backend is None:
            return GrepResult(matches=[])
        return await backend.agrep(pattern, path=path, glob=glob)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        backend = self._delegate()
        if backend is None:
            return GlobResult(matches=[])
        return await backend.aglob(pattern, path=path)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY_MSG)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=_READ_ONLY_MSG)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        raise NotImplementedError("Use aread()")

    def ls(self, path: str) -> LsResult:
        raise NotImplementedError("Use als()")

    def write(self, file_path: str, content: str) -> WriteResult:
        raise NotImplementedError("Use awrite()")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        raise NotImplementedError("Use aedit()")

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        raise NotImplementedError("Use agrep()")

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        raise NotImplementedError("Use aglob()")

    async def alist_recursive(self, prefix: str = "/attachments/") -> list[str]:
        backend = self._delegate()
        if backend is None:
            return []
        return await backend.alist_recursive(prefix)
