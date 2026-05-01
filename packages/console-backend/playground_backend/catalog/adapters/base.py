"""Base interface for catalog source adapters."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class ExtractionResourceError(Exception):
    """Raised when an extraction subprocess is killed due to memory limits.

    The caller should treat the file as skipped rather than retrying, since
    retrying will hit the same limit.
    """


@dataclass(slots=True)
class SourceFile:
    """A file discovered from the source."""

    id: str  # source-specific file ID (e.g. Google Drive file ID)
    name: str
    mime_type: str
    modified_at: datetime
    folder_path: str = ""  # path within the source (e.g. "Sales/Q1")
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractedPage:
    """A single page/slide extracted from a file."""

    page_number: int  # 1-based
    title: str
    text_content: str
    speaker_notes: str = ""
    source_ref: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChangeSet:
    """Delta changes from the source since last sync."""

    added: list[SourceFile] = field(default_factory=list)
    modified: list[SourceFile] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)
    new_page_token: str | None = None  # for incremental change tracking


class CatalogSourceAdapter(ABC):
    """Abstract interface for catalog source backends (Google Drive, etc.)."""

    @abstractmethod
    async def list_files(
        self,
        config: dict[str, Any],
        progress_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> list[SourceFile]:
        """List all eligible files from the source.

        Args:
            config: Source-specific configuration (e.g. shared_drive_id, folder_id).
            progress_callback: Optional async callback invoked with a human-readable
                progress message during long-running scans.

        Returns:
            List of source files.
        """

    @abstractmethod
    async def extract_pages(self, file: SourceFile, credentials: Any) -> list[ExtractedPage]:
        """Extract all pages/slides from a file.

        Args:
            file: The source file to extract from.
            credentials: Source-specific credentials for API access.

        Returns:
            List of extracted pages with text content and metadata.
        """

    @abstractmethod
    async def get_thumbnail(self, file: SourceFile, page: ExtractedPage, credentials: Any) -> bytes | None:
        """Get a thumbnail image (PNG) for a specific page.

        Args:
            file: The source file.
            page: The page to get thumbnail for.
            credentials: Source-specific credentials.

        Returns:
            PNG image bytes, or None if thumbnail unavailable.
        """

    async def get_all_thumbnails(
        self,
        file: SourceFile,
        pages: list[ExtractedPage],
        credentials: Any,
    ) -> dict[int, bytes]:
        """Get thumbnails for all pages in a file at once.

        Default implementation calls get_thumbnail() per page.
        Subclasses can override for efficiency (e.g. download PDF once, render all).
        Individual page failures are logged and skipped — one bad page does not
        prevent the rest from getting thumbnails.

        Returns:
            Dict mapping page_number → PNG bytes.
        """
        result: dict[int, bytes] = {}
        for page in pages:
            try:
                thumb = await self.get_thumbnail(file, page, credentials)
                if thumb:
                    result[page.page_number] = thumb
            except Exception:
                logger.warning(
                    "Failed to get thumbnail for page %d of %s, skipping",
                    page.page_number,
                    file.name,
                    exc_info=True,
                )
        return result

    @abstractmethod
    async def detect_changes(self, config: dict[str, Any], since_token: str | None = None) -> ChangeSet:
        """Detect file changes since last sync.

        Args:
            config: Source-specific configuration.
            since_token: Opaque token from previous sync (e.g. Drive changes startPageToken).

        Returns:
            ChangeSet with added, modified, and deleted files.
        """

    @abstractmethod
    async def list_shared_drives(self, credentials: Any) -> list[dict[str, str]]:
        """List shared drives accessible to the user.

        Args:
            credentials: User's OAuth credentials.

        Returns:
            List of dicts with 'id' and 'name' keys.
        """
