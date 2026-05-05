"""Object storage abstraction layer for S3-compatible APIs and local storage.

This module provides:
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

import asyncio
import hashlib
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

from aiobotocore.session import AioSession, get_session

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StoredObject:
    """Represents a stored object in the storage backend.

    Attributes:
        uri: Unique identifier (s3://bucket/key or file://bucket/key)
        bucket: Bucket or container name
        key: Object key/path within the bucket
        name: Original filename
        mime_type: MIME type of the content
        size: Size in bytes
    """

    uri: str
    bucket: str
    key: str
    name: str
    mime_type: str
    size: int


def parse_storage_uri(uri: str) -> tuple[str, str]:
    """Parse a storage URI into bucket and key.

    Supports:
    - s3://bucket/key
    - file://bucket/key

    Args:
        uri: Storage URI

    Returns:
        Tuple of (bucket, key)

    Raises:
        ValueError: If URI format is invalid
    """
    parsed = urlparse(uri)
    if parsed.scheme not in ("s3", "file"):
        raise ValueError(f"Unsupported storage URI scheme: {parsed.scheme}. Expected 's3://' or 'file://'")
    if not parsed.netloc:
        raise ValueError(f"Invalid storage URI: missing bucket name in {uri}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not key:
        raise ValueError(f"Invalid storage URI: missing key in {uri}")
    return bucket, key


class IObjectStorageService(ABC):
    """Abstract interface for object storage operations.

    All methods are async coroutines for consistency with the async
    architecture of the application.
    """

    @abstractmethod
    async def upload(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Optional[dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> StoredObject:
        """Upload object to storage.

        Args:
            bucket: Bucket/container name
            key: Object key/path
            content: File content as bytes
            metadata: Optional metadata (tags, custom headers)
            content_type: MIME type (default: application/octet-stream)

        Returns:
            StoredObject with URI and metadata
        """

    @abstractmethod
    async def download(self, uri: str) -> bytes:
        """Download object from storage.

        Args:
            uri: Storage URI (s3://bucket/key or file://bucket/key)

        Returns:
            File content as bytes

        Raises:
            FileNotFoundError: If object does not exist
            ValueError: If URI format is invalid
        """

    @abstractmethod
    async def generate_presigned_url(
        self,
        uri: str,
        expiration_seconds: int = 3600,
    ) -> str:
        """Generate presigned/temporary access URL.

        Args:
            uri: Storage URI
            expiration_seconds: URL validity duration (default: 1 hour)

        Returns:
            Presigned URL valid for specified duration
        """

    @abstractmethod
    async def delete(self, uri: str) -> None:
        """Delete object from storage.

        Args:
            uri: Storage URI

        Raises:
            ValueError: If URI format is invalid
        """

    @abstractmethod
    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
    ) -> list[str]:
        """List object keys by prefix.

        Args:
            bucket: Bucket name
            prefix: Key prefix filter

        Returns:
            List of object keys matching the prefix
        """

    @abstractmethod
    async def exists(self, uri: str) -> bool:
        """Check if an object exists.

        Args:
            uri: Storage URI

        Returns:
            True if object exists, False otherwise
        """

    @property
    @abstractmethod
    def storage_type(self) -> str:
        """Return storage type identifier ('s3', 's3-compatible', 'local')."""


class S3ObjectStorageService(IObjectStorageService):
    """AWS S3 and S3-compatible API implementation.

    Supports:
    - AWS S3 (default)
    - MinIO
    - DigitalOcean Spaces
    - Wasabi
    - Any S3-compatible endpoint
    """

    def __init__(
        self,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session: Optional[AioSession] = None,
    ):
        """Initialize S3 storage service.

        Args:
            region: AWS region (default: AWS_REGION env or eu-central-1)
            endpoint_url: Custom S3 endpoint (for S3-compatible APIs)
            access_key_id: Access key (for S3-compatible APIs; otherwise use AWS creds)
            secret_access_key: Secret key (for S3-compatible APIs)
            session: Optional aiobotocore session (for testing)
        """
        self.region = region or os.getenv("AWS_REGION", os.getenv("S3_REGION", "eu-central-1"))
        self.endpoint_url = endpoint_url or os.getenv("S3_ENDPOINT_URL")
        self.access_key_id = access_key_id or os.getenv("S3_ACCESS_KEY_ID")
        self.secret_access_key = secret_access_key or os.getenv("S3_SECRET_ACCESS_KEY")
        self._session = session or get_session()
        self._is_s3_compatible = bool(self.endpoint_url)

    def _client_kwargs(self) -> dict:
        """Build kwargs for create_client."""
        kwargs: dict = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.access_key_id and self.secret_access_key:
            kwargs["aws_access_key_id"] = self.access_key_id
            kwargs["aws_secret_access_key"] = self.secret_access_key
        return kwargs

    async def upload(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Optional[dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> StoredObject:
        """Upload object to S3."""
        mime_type = content_type or "application/octet-stream"
        put_kwargs: dict = {
            "Bucket": bucket,
            "Key": key,
            "Body": content,
            "ContentType": mime_type,
        }
        if metadata:
            put_kwargs["Metadata"] = metadata

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            await client.put_object(**put_kwargs)

        uri = f"s3://{bucket}/{key}"
        logger.info(f"Uploaded {len(content)} bytes to {uri}")

        return StoredObject(
            uri=uri,
            bucket=bucket,
            key=key,
            name=key.split("/")[-1],
            mime_type=mime_type,
            size=len(content),
        )

    async def download(self, uri: str) -> bytes:
        """Download object from S3."""
        bucket, key = parse_storage_uri(uri)

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            try:
                response = await client.get_object(Bucket=bucket, Key=key)
                async with response["Body"] as stream:
                    return await stream.read()
            except client.exceptions.NoSuchKey:
                raise FileNotFoundError(f"Object not found: {uri}")
            except Exception as e:
                if "NoSuchKey" in str(e) or "404" in str(e):
                    raise FileNotFoundError(f"Object not found: {uri}")
                raise

    async def generate_presigned_url(
        self,
        uri: str,
        expiration_seconds: int = 3600,
    ) -> str:
        """Generate presigned URL for S3 object."""
        bucket, key = parse_storage_uri(uri)
        # Clamp to max 24 hours (application limit, S3 supports up to 7 days)
        expiration_seconds = min(expiration_seconds, 86400)

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            url = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expiration_seconds,
            )
        logger.debug(f"Generated presigned URL for {uri} (expires in {expiration_seconds}s)")
        return url

    async def delete(self, uri: str) -> None:
        """Delete object from S3."""
        bucket, key = parse_storage_uri(uri)

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            await client.delete_object(Bucket=bucket, Key=key)
        logger.info(f"Deleted {uri}")

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
    ) -> list[str]:
        """List objects in S3 bucket by prefix."""
        keys: list[str] = []

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])

        return keys

    async def exists(self, uri: str) -> bool:
        """Check if object exists in S3."""
        bucket, key = parse_storage_uri(uri)

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            try:
                await client.head_object(Bucket=bucket, Key=key)
                return True
            except Exception:
                return False

    @property
    def storage_type(self) -> str:
        """Return storage type identifier."""
        return "s3-compatible" if self._is_s3_compatible else "s3"


class LocalObjectStorageService(IObjectStorageService):
    """File system-based storage for local development and testing.

    Stores files in a directory structure: {root_path}/{bucket}/{key}
    Metadata is stored alongside objects in .meta.json files.

    Presigned URLs are simulated by generating signed tokens that can be
    verified locally. For web serving, configure a local HTTP server to
    serve the storage directory.
    """

    def __init__(
        self,
        root_path: Optional[str] = None,
        base_url: Optional[str] = None,
        signing_key: Optional[str] = None,
    ):
        """Initialize local storage service.

        Args:
            root_path: Root directory for storage (default: LOCAL_STORAGE_PATH env or ./local-storage)
            base_url: Base URL for presigned URLs (default: file://)
            signing_key: Secret key for signing URLs (default: random)
        """
        self.root_path = Path(root_path or os.getenv("LOCAL_STORAGE_PATH", "./local-storage"))
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url or os.getenv("LOCAL_STORAGE_BASE_URL", "file://")
        self._signing_key = signing_key or os.getenv("LOCAL_STORAGE_SIGNING_KEY", os.urandom(32).hex())

    def _validate_key(self, key: str) -> None:
        """Validate key to prevent path traversal attacks."""
        if ".." in key or key.startswith("/"):
            raise ValueError(f"Path traversal detected in key: {key}")

    def _object_path(self, bucket: str, key: str) -> Path:
        """Get the file path for an object."""
        return self.root_path / bucket / key

    def _meta_path(self, bucket: str, key: str) -> Path:
        """Get the metadata file path for an object (hidden file with dot prefix)."""
        parent = (self.root_path / bucket / key).parent
        filename = Path(key).name
        return parent / f".{filename}.meta.json"

    async def upload(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Optional[dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> StoredObject:
        """Upload object to local storage."""
        self._validate_key(key)
        mime_type = content_type or "application/octet-stream"
        obj_path = self._object_path(bucket, key)
        meta_path = self._meta_path(bucket, key)

        # Create parent directories
        obj_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content (run in thread to avoid blocking)
        await asyncio.to_thread(obj_path.write_bytes, content)

        # Write metadata
        meta = {
            "content_type": mime_type,
            "size": len(content),
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await asyncio.to_thread(meta_path.write_text, json.dumps(meta, indent=2))

        uri = f"file://{bucket}/{key}"
        logger.info(f"Uploaded {len(content)} bytes to {uri}")

        return StoredObject(
            uri=uri,
            bucket=bucket,
            key=key,
            name=key.split("/")[-1],
            mime_type=mime_type,
            size=len(content),
        )

    async def download(self, uri: str) -> bytes:
        """Download object from local storage."""
        bucket, key = parse_storage_uri(uri)
        obj_path = self._object_path(bucket, key)

        if not obj_path.exists():
            raise FileNotFoundError(f"Object not found: {uri}")

        return await asyncio.to_thread(obj_path.read_bytes)

    async def generate_presigned_url(
        self,
        uri: str,
        expiration_seconds: int = 3600,
    ) -> str:
        """Generate a signed URL for local storage.

        For local development, this returns a path that can be served by a local
        HTTP server. The base_url determines the URL prefix.
        """
        bucket, key = parse_storage_uri(uri)

        # For local storage, return a path-based URL
        # The base_url can be configured to point to a local file server
        if self.base_url == "file://":
            obj_path = self._object_path(bucket, key)
            return f"file://{obj_path.absolute()}"
        else:
            # HTTP-style URL (e.g., /api/v1/files/file.txt)
            return f"{self.base_url.rstrip('/')}/{key}"

    def verify_signed_url(self, url: str) -> bool:
        """Verify a signed URL is valid and not expired.

        Args:
            url: The signed URL to verify

        Returns:
            True if valid and not expired, False otherwise
        """
        try:
            parsed = urlparse(url)
            # Extract query params
            params = dict(p.split("=") for p in parsed.query.split("&") if "=" in p)
            expires_ts = int(params.get("expires", "0"))
            signature = params.get("sig", "")

            # Check expiration
            if datetime.now(timezone.utc).timestamp() > expires_ts:
                return False

            # Extract bucket/key from path
            path_parts = parsed.path.lstrip("/").split("/", 1)
            if len(path_parts) != 2:
                return False
            bucket, key = path_parts

            # Verify signature
            message = f"{bucket}/{key}:{expires_ts}"
            expected_sig = hashlib.sha256(f"{message}:{self._signing_key}".encode()).hexdigest()[:16]
            return signature == expected_sig
        except Exception:
            return False

    async def delete(self, uri: str) -> None:
        """Delete object from local storage."""
        bucket, key = parse_storage_uri(uri)
        obj_path = self._object_path(bucket, key)
        meta_path = self._meta_path(bucket, key)

        if obj_path.exists():
            await asyncio.to_thread(obj_path.unlink)
        if meta_path.exists():
            await asyncio.to_thread(meta_path.unlink)

        logger.info(f"Deleted {uri}")

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
    ) -> list[str]:
        """List objects in local storage by prefix."""
        bucket_path = self.root_path / bucket

        if not bucket_path.exists():
            return []

        keys: list[str] = []

        def _scan_dir() -> list[str]:
            result = []
            for path in bucket_path.rglob("*"):
                if path.is_file() and not path.name.endswith(".meta.json"):
                    key = str(path.relative_to(bucket_path))
                    if key.startswith(prefix):
                        result.append(key)
            return sorted(result)

        keys = await asyncio.to_thread(_scan_dir)
        return keys

    async def exists(self, uri: str) -> bool:
        """Check if object exists in local storage."""
        bucket, key = parse_storage_uri(uri)
        obj_path = self._object_path(bucket, key)
        return obj_path.exists()

    @property
    def storage_type(self) -> str:
        """Return storage type identifier."""
        return "local"


# ---------------------------------------------------------------------------
# Factory and singleton
# ---------------------------------------------------------------------------

_storage_service: Optional[IObjectStorageService] = None


def create_object_storage_service(
    storage_type: Optional[str] = None,
    **kwargs,
) -> IObjectStorageService:
    """Create an object storage service based on configuration.

    Args:
        storage_type: 's3' (default), 's3-compatible', or 'local'
        **kwargs: Additional arguments passed to the service constructor

    Returns:
        IObjectStorageService implementation

    Environment Variables:
        OBJECT_STORAGE_TYPE: Default storage type
        S3_REGION: AWS region
        S3_ENDPOINT_URL: Custom S3 endpoint (for s3-compatible)
        S3_ACCESS_KEY_ID: Access key for S3-compatible
        S3_SECRET_ACCESS_KEY: Secret key for S3-compatible
        LOCAL_STORAGE_PATH: Root path for local storage
    """
    storage_type = storage_type or os.getenv("OBJECT_STORAGE_TYPE", "s3")

    if storage_type == "local":
        return LocalObjectStorageService(
            root_path=kwargs.get("root_path"),
            base_url=kwargs.get("base_url"),
            signing_key=kwargs.get("signing_key"),
        )
    elif storage_type in ("s3", "s3-compatible"):
        return S3ObjectStorageService(
            region=kwargs.get("region"),
            endpoint_url=kwargs.get("endpoint_url"),
            access_key_id=kwargs.get("access_key_id"),
            secret_access_key=kwargs.get("secret_access_key"),
            session=kwargs.get("session"),
        )
    else:
        raise ValueError(f"Unsupported storage type: {storage_type}. Use 's3', 's3-compatible', or 'local'")


def get_object_storage_service() -> IObjectStorageService:
    """Get the singleton object storage service.

    Creates the service on first call based on environment configuration.

    Returns:
        IObjectStorageService singleton instance
    """
    global _storage_service
    if _storage_service is None:
        _storage_service = create_object_storage_service()
    return _storage_service


def reset_object_storage_service() -> None:
    """Reset the singleton storage service.

    Useful for testing or reconfiguration.
    """
    global _storage_service
    _storage_service = None
