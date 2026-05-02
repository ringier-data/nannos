"""Routes for managing file uploads for chat messages."""

import os
from typing import Annotated

from console_backend.dependencies import require_auth
from console_backend.models.user import User
from console_backend.services.file_storage_service import FileStorageService
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/files", tags=["files"])


class RegenerateUrlRequest(BaseModel):
    """Request to regenerate presigned URLs for file attachments."""

    files: list[dict[str, str]]  # Each dict should have 's3Url' and optionally 'name'


class UploadedFileInfo(BaseModel):
    """Response model for uploaded file metadata."""

    id: str
    bucket: str
    key: str
    name: str
    mimeType: str
    size: int
    uri: str
    downloadUri: str
    s3Url: str


class UploadedFileResponse(BaseModel):
    files: list[UploadedFileInfo]


class RegeneratedFileInfo(BaseModel):
    key: str
    name: str
    url: str


class RegeneratedFileResponse(BaseModel):
    files: list[RegeneratedFileInfo]


@router.post("/upload")
async def upload_files(
    request: Request,
    files: Annotated[list[UploadFile], File(...)],
    conversation_id: Annotated[str | None, Form()] = None,
    user: User = Depends(require_auth),
) -> UploadedFileResponse:
    """Upload one or more files (including audio recordings) for a chat message.

    Args:
        conversation_id: Optional conversation ID. If not provided, a new conversation will be created.
        files: List of files to upload
        user: Authenticated user

    Returns metadata for each uploaded file, including a presigned URL that the
    frontend can use immediately for preview and download. The metadata also
    includes the S3 URL so that future responses can rehydrate the URL when the
    presigned link expires.
    """

    if not files:
        raise HTTPException(status_code=400, detail="At least one file must be provided")

    # Create a new conversation if conversation_id is not provided
    if not conversation_id:
        conversation_id = str(uuid4())
        try:
            await conversation_service.get_or_create_conversation(
                conversation_id=conversation_id,
                user_id=user.id,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to create conversation: {str(exc)}") from exc

    storage: FileStorageService | None = getattr(request.app.state, "file_storage_service", None)
    if storage is None:
        raise HTTPException(status_code=500, detail="File storage service is not available")

    uploaded_files: list[UploadedFileInfo] = []

    for upload in files:
        try:
            stored = await storage.upload_file(
                upload,
                user_id=user.id,
                conversation_id=conversation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        uploaded_files.append(
            UploadedFileInfo(
                id=stored.id,
                bucket=stored.bucket,
                key=stored.key,
                name=stored.name,
                mimeType=stored.mime_type,
                size=stored.size,
                uri=stored.uri,
                downloadUri=stored.download_uri,
                s3Url=f"s3://{stored.bucket}/{stored.key}",
            )
        )

    return UploadedFileResponse(files=uploaded_files)


@router.post("/regenerate-urls")
async def regenerate_presigned_urls(
    request: Request,
    body: RegenerateUrlRequest,
    user: User = Depends(require_auth),
) -> RegeneratedFileResponse:
    """Regenerate presigned URLs for file attachments.

    This endpoint allows clients to refresh expired presigned URLs using the
    stored S3 URLs from conversation history. Files must belong to the
    authenticated user (verified via key prefix).

    Args:
        body: Request containing list of files with their S3 URLs
        user: Authenticated user

    Returns:
        Dictionary with refreshed file URLs

    Raises:
        HTTPException: If storage service unavailable or access denied
    """
    storage: FileStorageService | None = getattr(request.app.state, "file_storage_service", None)
    if storage is None:
        raise HTTPException(status_code=500, detail="File storage service is not available")

    if not body.files:
        raise HTTPException(status_code=400, detail="At least one file key must be provided")

    regenerated_files: list[RegeneratedFileInfo] = []

    for file_info in body.files:
        key = file_info.get("key")
        if not key:
            raise HTTPException(status_code=400, detail="Each file must have a 'key' field")

        # Security check: Verify the file belongs to the user
        # Keys are formatted as: uploads/{user_id}/{conversation_id}/...
        # Extract user_id from key and compare
        key_parts = key.split("/")
        if len(key_parts) < 2 or key_parts[0] != (storage._prefix or "uploads"):
            raise HTTPException(status_code=403, detail="Invalid file key format")

        # For security, verify user_id in the path matches authenticated user
        if len(key_parts) >= 2 and key_parts[1] != user.id:
            raise HTTPException(status_code=403, detail="Access denied to this file")

        try:
            new_url = await storage.generate_presigned_get_url(key)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        regenerated_files.append(
            RegeneratedFileInfo(
                key=key,
                name=file_info.get("name", ""),
                url=new_url,
            )
        )

    return RegeneratedFileResponse(files=regenerated_files)


@router.get("/local/{file_path:path}")
async def serve_local_file(
    request: Request,
    file_path: str,
    user: User = Depends(require_auth),
) -> FileResponse:
    """Serve a file from local filesystem storage.

    Only active when LocalFileStorageService is in use (no S3 bucket configured).
    """
    from console_backend.services.local_file_storage_service import LocalFileStorageService

    storage = getattr(request.app.state, "file_storage_service", None)
    if not isinstance(storage, LocalFileStorageService):
        raise HTTPException(status_code=404, detail="Local file serving is not enabled")

    # Security: verify the user owns the file (key format: uploads/{user_id}/...)
    parts = file_path.split("/")
    if len(parts) < 2 or parts[0] != storage._prefix or parts[1] != user.id:
        raise HTTPException(status_code=403, detail="Access denied to this file")

    abs_path = os.path.join(storage._base_path, file_path)
    # Prevent path traversal
    abs_path = os.path.realpath(abs_path)
    base_real = os.path.realpath(storage._base_path)
    if not abs_path.startswith(base_real + os.sep) and abs_path != base_real:
        raise HTTPException(status_code=403, detail="Invalid file path")

    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(abs_path)
