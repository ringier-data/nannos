"""Local filesystem file storage — drop-in replacement for S3-backed FileStorageService.

Used when FILES_S3_BUCKET is not set (local development without AWS credentials).
Files are stored under a configurable local directory.
"""

import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from fastapi import UploadFile

logger = logging.getLogger(__name__)

# Re-use the UploadedFile dataclass from the S3 service
from .file_storage_service import UploadedFile, _ALLOWED_AUDIO_MIME_TYPES, _ALLOWED_EXTRA_MIME_TYPES, _ALLOWED_OFFICE_MIME_PREFIXES, _ALLOWED_OFFICE_MIME_TYPES  # noqa: E402


class LocalFileStorageService:
    """Handles file uploads to local filesystem.

    Provides the same public API as FileStorageService but stores files on disk
    instead of S3. Presigned URLs are replaced with local API endpoint URLs.
    """

    def __init__(self, base_path: str | None = None) -> None:
        self._base_path = base_path or os.getenv("LOCAL_FILE_STORAGE_PATH", "./local-uploads")
        self._prefix = "uploads"
        self._presigned_ttl = 3600
        os.makedirs(self._base_path, exist_ok=True)
        logger.warning("Using local filesystem for file storage at: %s", os.path.abspath(self._base_path))

    @property
    def bucket(self) -> str:
        return "local"

    @property
    def presigned_ttl_seconds(self) -> int:
        return self._presigned_ttl

    def is_allowed_file(self, mime_type: str, filename: str | None = None) -> bool:
        """Validate that the provided file mime-type is supported."""
        import mimetypes as mt

        if mime_type in ("text/plain", "text/csv", "text/tab-separated-values") or mime_type.startswith("text/"):
            return True
        if mime_type.startswith("image/"):
            return True
        if mime_type in _ALLOWED_AUDIO_MIME_TYPES or mime_type.startswith("audio/"):
            return True
        if mime_type in _ALLOWED_EXTRA_MIME_TYPES:
            return True
        if mime_type in _ALLOWED_OFFICE_MIME_TYPES:
            return True
        if any(mime_type.startswith(prefix) for prefix in _ALLOWED_OFFICE_MIME_PREFIXES):
            return True
        if mime_type in ("application/octet-stream", "", "binary/octet-stream") and filename:
            guessed = mt.guess_type(filename)[0]
            if guessed:
                return self.is_allowed_file(guessed, None)
        return False

    def _sanitize_segment(self, value: str, *, default: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
        return cleaned or default

    def _build_object_key(self, *, user_id: str, conversation_id: str, filename: str) -> str:
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
        """Upload a file to local filesystem."""
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

        file_path = os.path.join(self._base_path, key)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Determine size
        upload.file.seek(0, os.SEEK_END)
        size = upload.file.tell()
        upload.file.seek(0)

        try:
            with open(file_path, "wb") as f:
                shutil.copyfileobj(upload.file, f)
        except Exception as exc:
            logger.exception("Failed to write file %s", file_path)
            raise RuntimeError("File upload failed") from exc
        finally:
            try:
                upload.file.seek(0)
            except (ValueError, OSError):
                pass

        uri = f"/api/v1/files/local/{key}"

        return UploadedFile(
            id=uuid4().hex,
            bucket="local",
            key=key,
            name=filename,
            mime_type=content_type,
            size=size,
            uri=uri,
            download_uri=uri,
        )

    async def generate_presigned_get_url(self, key: str, *, expires_in: Optional[int] = None) -> str:
        """Return a local API URL for the file."""
        return f"/api/v1/files/local/{key}"

    async def delete_file(self, key: str) -> None:
        """Delete a file from local filesystem."""
        file_path = os.path.join(self._base_path, key)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as exc:
            logger.exception("Failed to delete file %s", file_path)
            raise RuntimeError("Failed to delete file") from exc
