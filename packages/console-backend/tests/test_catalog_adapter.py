"""Tests for GoogleDriveAdapter — file listing and page extraction with mocked Google APIs.

Tests cover:
- File listing with mocked Drive API responses
- Folder path resolution
- Page extraction for Google Slides (mocked Slides API)
- Text extraction helpers
- Shared drive listing
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from console_backend.catalog.adapters.base import SourceFile
from console_backend.catalog.adapters.google_drive import (
    GoogleDriveAdapter,
    _build_folder_path,
    _parse_drive_time,
)


class TestParseHelpers:
    """Test utility functions."""

    def test_parse_drive_time_utc(self):
        result = _parse_drive_time("2026-01-15T10:30:00.000Z")
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15
        assert result.tzinfo is not None

    def test_build_folder_path_simple(self):
        parents_map = {
            "folder-a": {"name": "Sales", "parent_id": "folder-root"},
            "folder-root": {"name": "Root", "parent_id": None},
        }
        path = _build_folder_path("folder-a", parents_map)
        assert path == "Sales"

    def test_build_folder_path_nested(self):
        parents_map = {
            "folder-c": {"name": "Q1", "parent_id": "folder-b"},
            "folder-b": {"name": "Sales", "parent_id": "folder-a"},
            "folder-a": {"name": "Presentations", "parent_id": None},
        }
        path = _build_folder_path("folder-c", parents_map)
        # _build_folder_path stops before the root (parent_id=None)
        assert path == "Sales/Q1"

    def test_build_folder_path_empty(self):
        path = _build_folder_path("unknown-id", {})
        assert path == ""


class TestGoogleDriveAdapterListFiles:
    """Test file listing with mocked Drive API."""

    @pytest.mark.asyncio
    async def test_list_files_returns_supported_files(self):
        adapter = GoogleDriveAdapter()

        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_service.files.return_value = mock_files

        # Mock Drive API response
        mock_list_request = MagicMock()
        mock_list_request.execute.return_value = {
            "files": [
                {
                    "id": "file-1",
                    "name": "Quarterly Report.pptx",
                    "mimeType": "application/vnd.google-apps.presentation",
                    "modifiedTime": "2026-03-15T14:30:00.000Z",
                    "parents": ["folder-1"],
                    "owners": [{"displayName": "John Doe"}],
                    "webViewLink": "https://docs.google.com/presentation/d/file-1",
                },
            ],
            "nextPageToken": None,
        }
        mock_files.list.return_value = mock_list_request

        # Mock folder map building
        mock_folder_list = MagicMock()
        mock_folder_list.execute.return_value = {
            "files": [
                {"id": "folder-1", "name": "Marketing", "parents": ["drive-123"]},
            ],
            "nextPageToken": None,
        }
        # files().list() is called twice: once for folders, once for files
        mock_files.list.side_effect = [mock_folder_list, mock_list_request]

        mock_credentials = MagicMock()

        with (
            patch(
                "console_backend.catalog.adapters.google_drive._build_drive_service",
                return_value=mock_service,
            ),
            patch(
                "console_backend.catalog.adapters.google_drive._run_in_executor",
                side_effect=lambda func, *args: func(*args) if not args else func(),
            ),
        ):
            files = await adapter.list_files(
                {
                    "credentials": mock_credentials,
                    "shared_drive_id": "drive-123",
                }
            )

        assert len(files) == 1
        assert files[0].id == "file-1"
        assert files[0].name == "Quarterly Report.pptx"
        assert files[0].mime_type == "application/vnd.google-apps.presentation"


class TestGoogleDriveAdapterExtractPages:
    """Test page extraction with mocked Slides API."""

    @pytest.mark.asyncio
    async def test_extract_google_slides_pages(self):
        adapter = GoogleDriveAdapter()

        mock_slides_service = MagicMock()
        mock_presentations = MagicMock()
        mock_slides_service.presentations.return_value = mock_presentations

        # Mock Slides API response with two slides
        mock_get_request = MagicMock()
        mock_get_request.execute.return_value = {
            "slides": [
                {
                    "objectId": "slide-1",
                    "pageElements": [
                        {
                            "shape": {
                                "placeholder": {"type": "TITLE"},
                                "text": {
                                    "textElements": [
                                        {"textRun": {"content": "Revenue Overview\n"}},
                                    ]
                                },
                            }
                        },
                        {
                            "shape": {
                                "text": {
                                    "textElements": [
                                        {"textRun": {"content": "Q1 revenue was $10M\n"}},
                                    ]
                                },
                            }
                        },
                    ],
                    "slideProperties": {
                        "notesPage": {
                            "pageElements": [
                                {
                                    "shape": {
                                        "text": {
                                            "textElements": [
                                                {"textRun": {"content": "Discuss growth trends\n"}},
                                            ]
                                        },
                                        "placeholder": {"type": "BODY"},
                                    }
                                }
                            ]
                        }
                    },
                },
                {
                    "objectId": "slide-2",
                    "pageElements": [
                        {
                            "shape": {
                                "placeholder": {"type": "TITLE"},
                                "text": {
                                    "textElements": [
                                        {"textRun": {"content": "Regional Breakdown\n"}},
                                    ]
                                },
                            }
                        },
                    ],
                },
            ]
        }
        mock_presentations.get.return_value = mock_get_request

        source_file = SourceFile(
            id="file-1",
            name="Quarterly Review.pptx",
            mime_type="application/vnd.google-apps.presentation",
            modified_at=datetime.now(timezone.utc),
        )

        mock_credentials = MagicMock()

        with (
            patch(
                "console_backend.catalog.adapters.google_drive._build_slides_service",
                return_value=mock_slides_service,
            ),
            patch(
                "console_backend.catalog.adapters.google_drive._run_in_executor",
                side_effect=lambda func, *args: func(*args) if not args else func(),
            ),
        ):
            pages = await adapter.extract_pages(source_file, mock_credentials)

        assert len(pages) == 2
        assert pages[0].page_number == 1
        assert "Revenue Overview" in pages[0].title
        assert "Q1 revenue" in pages[0].text_content
        assert "growth trends" in pages[0].speaker_notes
        assert pages[0].source_ref["type"] == "google_slides"
        assert pages[0].source_ref["page_object_id"] == "slide-1"

        assert pages[1].page_number == 2
        assert "Regional Breakdown" in pages[1].title


class TestGoogleDriveAdapterSharedDrives:
    """Test shared drive listing."""

    @pytest.mark.asyncio
    async def test_list_shared_drives(self):
        adapter = GoogleDriveAdapter()

        mock_service = MagicMock()
        mock_drives = MagicMock()
        mock_service.drives.return_value = mock_drives

        mock_list_request = MagicMock()
        mock_list_request.execute.return_value = {
            "drives": [
                {"id": "drive-1", "name": "Marketing"},
                {"id": "drive-2", "name": "Sales"},
            ],
            "nextPageToken": None,
        }
        mock_drives.list.return_value = mock_list_request

        mock_credentials = MagicMock()

        with (
            patch(
                "console_backend.catalog.adapters.google_drive._build_drive_service",
                return_value=mock_service,
            ),
            patch(
                "console_backend.catalog.adapters.google_drive._run_in_executor",
                side_effect=lambda func, *args: func(*args) if not args else func(),
            ),
        ):
            drives = await adapter.list_shared_drives(mock_credentials)

        assert len(drives) == 2
        assert drives[0] == {"id": "drive-1", "name": "Marketing"}
        assert drives[1] == {"id": "drive-2", "name": "Sales"}
