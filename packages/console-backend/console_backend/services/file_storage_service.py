"""Service for managing file uploads and generating presigned URLs.

Uses IObjectStorageService abstraction for backend flexibility (S3, S3-compatible, local).
"""

import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from object_storage import (
    IObjectStorageService,
    get_object_storage_service,
)
from console_backend.config import config
from fastapi import UploadFile

logger = logging.getLogger(__name__)

# Allowed MIME type prefixes for Office documents
_ALLOWED_OFFICE_MIME_PREFIXES = ("application/vnd.openxmlformats-officedocument",)

# Allowed Office document MIME types
_ALLOWED_OFFICE_MIME_TYPES = {
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.ms-outlook",
    "application/vnd.ms-project",
    "application/vnd.visio",
    "application/vnd.ms-works",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.apple.keynote",
    "application/vnd.apple.numbers",
    "application/vnd.apple.pages",
}

# Additional allowed MIME types (PDFs, etc.)
_ALLOWED_EXTRA_MIME_TYPES = {
    "application/pdf",
}

# Audio MIME types for audio recording support
_ALLOWED_AUDIO_MIME_TYPES = {
    "audio/webm",
    "audio/wav",
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/m4a",
    "audio/x-m4a",
    "audio/mp3",
    "audio/x-wav",
    "audio/wave",
}


@dataclass(slots=True)
class UploadedFile:
    """Represents a file uploaded to S3."""

    id: str
    bucket: str
    key: str
    name: str
    mime_type: str
    size: int
    uri: str
    download_uri: str


class FileStorageService:
    """Handles file uploads and presigned URL generation for user attachments including audio.

    Uses IObjectStorageService abstraction for backend flexibility:
    - S3: AWS S3 (production)
    - S3-compatible: MinIO, DigitalOcean Spaces, etc.
    - Local: File system (development)
    """

    def __init__(
        self,
        storage: Optional[IObjectStorageService] = None,
    ) -> None:
        self._storage = storage or get_object_storage_service()
        self._bucket = config.file_storage.bucket
        self._prefix = config.file_storage.prefix.strip("/ ") or "uploads"
        self._presigned_ttl = config.file_storage.presigned_ttl_seconds

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def presigned_ttl_seconds(self) -> int:
        return self._presigned_ttl

    @property
    def storage_type(self) -> str:
        """Return the underlying storage backend type."""
        return self._storage.storage_type

    def is_allowed_file(self, mime_type: str, filename: str | None = None) -> bool:
        """Validate that the provided file mime-type is supported."""

        # Allow text/plain (e.g. txt or csv files, tsv) - these are common for user uploads and generally safe
        if mime_type in ("text/plain", "text/csv", "text/tab-separated-values") or mime_type.startswith("text/"):
            return True

        # Allow all image types
        if mime_type.startswith("image/"):
            return True

        # Allow audio types (for recording support)
        if mime_type in _ALLOWED_AUDIO_MIME_TYPES or mime_type.startswith("audio/"):
            return True

        # Allow PDFs and other extra types
        if mime_type in _ALLOWED_EXTRA_MIME_TYPES:
            return True

        # Allow Office documents
        if mime_type in _ALLOWED_OFFICE_MIME_TYPES:
            return True

        if any(mime_type.startswith(prefix) for prefix in _ALLOWED_OFFICE_MIME_PREFIXES):
            return True

        # Fallback to filename extension heuristics when MIME type is missing or generic
        if mime_type in ("application/octet-stream", "", "binary/octet-stream") and filename:
            guessed = mimetypes.guess_type(filename)[0]
            if guessed:
                return self.is_allowed_file(guessed, None)

        return False

    def _sanitize_segment(self, value: str, *, default: str) -> str:
        """Sanitize a path segment by removing special characters."""
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
        return cleaned or default

    def _build_object_key(
        self,
        *,
        user_id: str,
        conversation_id: str,
        filename: str,
    ) -> str:
        """Build S3 object key with user/conversation scoping."""
        name, ext = os.path.splitext(filename)
        safe_name = self._sanitize_segment(name or "file", default="file")
        safe_ext = self._sanitize_segment(ext, default="").lstrip(".")

        random_part = uuid4().hex
        components = filter(
            None,
            (
                self._prefix,
                user_id,
                conversation_id,
                f"{safe_name}-{random_part}{('.' + safe_ext) if safe_ext else ''}",
            ),
        )
        return "/".join(components)

    async def upload_file(
        self,
        upload: UploadFile,
        *,
        user_id: str,
        conversation_id: str,
    ) -> UploadedFile:
        """Upload a single file and return metadata."""

        content_type = upload.content_type or "application/octet-stream"
        filename = upload.filename or "file"

        if not self.is_allowed_file(content_type, filename):
            logger.warning("Rejected upload with unsupported mime type: %s (file=%s)", content_type, filename)
            raise ValueError("Unsupported file type. Only images, audio files, PDFs, and Office documents are allowed.")

        key = self._build_object_key(
            user_id=user_id,
            conversation_id=conversation_id,
            filename=filename,
        )

        # Read content
        content = await upload.read()
        size = len(content)

        try:
            stored = await self._storage.upload(
                bucket=self._bucket,
                key=key,
                content=content,
                content_type=content_type,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed to upload file %s to bucket %s", filename, self._bucket)
            raise RuntimeError("File upload failed") from exc

        # Generate presigned URL for download
        uri = await self._storage.generate_presigned_url(stored.uri, self._presigned_ttl)

        return UploadedFile(
            id=uuid4().hex,
            bucket=self._bucket,
            key=key,
            name=filename,
            mime_type=content_type,
            size=size,
            uri=stored.uri,
            download_uri=uri,
        )

    async def generate_presigned_get_url(self, key: str, *, expires_in: Optional[int] = None) -> str:
        """Generate a presigned GET URL for the provided object key."""

        expires = expires_in or self._presigned_ttl
        uri = f"s3://{self._bucket}/{key}" if self._storage.storage_type != "local" else f"file://{self._bucket}/{key}"
        try:
            return await self._storage.generate_presigned_url(uri, expires)
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed to generate presigned URL for key %s", key)
            raise RuntimeError("Failed to generate download URL") from exc

    async def delete_file(self, key: str) -> None:
        """Delete a file. Intended for future lifecycle management."""

        uri = f"s3://{self._bucket}/{key}" if self._storage.storage_type != "local" else f"file://{self._bucket}/{key}"
        try:
            await self._storage.delete(uri)
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed to delete object %s", key)
            raise RuntimeError("Failed to delete file") from exc
