"""Object storage abstraction layer for S3-compatible APIs and local storage.

This package provides:
- IObjectStorageService: Abstract interface for storage operations
- S3ObjectStorageService: AWS S3 / S3-compatible implementation
- LocalObjectStorageService: File system-based backend for development
- Factory functions: create_object_storage_service, get_object_storage_service

Configuration via environment variables:
- OBJECT_STORAGE_TYPE: 's3' (default), 's3-compatible', or 'local'
- S3_BUCKET_FILES: Default bucket name for file storage
- S3_REGION: AWS region (default: eu-central-1)
- S3_ENDPOINT_URL: Custom endpoint for S3-compatible APIs (MinIO, etc.)
- S3_ACCESS_KEY_ID: Access key for S3-compatible APIs
- S3_SECRET_ACCESS_KEY: Secret key for S3-compatible APIs
- LOCAL_STORAGE_PATH: Root path for local storage (default: ./local-storage)
"""

from object_storage.service import (
    IObjectStorageService,
    LocalObjectStorageService,
    S3ObjectStorageService,
    StoredObject,
    create_object_storage_service,
    get_object_storage_service,
    parse_storage_uri,
    reset_object_storage_service,
)

__all__ = [
    "IObjectStorageService",
    "LocalObjectStorageService",
    "S3ObjectStorageService",
    "StoredObject",
    "create_object_storage_service",
    "get_object_storage_service",
    "parse_storage_uri",
    "reset_object_storage_service",
]
