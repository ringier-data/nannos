"""Object storage abstraction layer for file operations.

Provides a pluggable interface for object storage backends, enabling deployment
flexibility across AWS S3, S3-compatible APIs (MinIO, DigitalOcean Spaces, Wasabi),
and local filesystem storage for development.

Design choice: We use a custom thin abstraction over libraries like apache-libcloud
to minimize dependencies and keep the interface simple. The ABC is intentionally
small — community contributors can add backends (GCS, Azure Blob, etc.) by
implementing IObjectStorageService.

Backends:
- S3ObjectStorageService: AWS S3 and any S3-compatible endpoint (MinIO, etc.)
- LocalObjectStorageService: Filesystem-based storage for local development

Configuration is driven by environment variables:
- OBJECT_STORAGE_TYPE: "s3" (default) or "local"
- S3_ENDPOINT_URL: Optional override for S3-compatible endpoints
- S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY: Optional explicit credentials
- LOCAL_STORAGE_PATH: Root directory for local storage (default: ./local-storage)
- STORAGE_PRESIGNED_TTL_SECONDS: Default presigned URL TTL (default: 3600)
"""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StoredObject:
    """Represents a stored object in any backend.

    This is the low-level type returned by IObjectStorageService.
    Higher-level services (e.g. console-backend's FileStorageService)
    may wrap this in their own domain-specific types.
    """

    uri: str  # Canonical URI (s3://bucket/key or file://path)
    bucket: str
    key: str
    name: str  # Original filename (basename of key)
    mime_type: str
    size: int


def parse_storage_uri(uri: str) -> tuple[str, str]:
    """Parse a storage URI into bucket and key.

    Supports:
    - s3://bucket/key
    - file://bucket/key (local storage)

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

    All methods are async coroutines. Implementations must handle their own
    connection/session lifecycle.
    """

    @abstractmethod
    async def upload(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Optional[dict[str, str]] = None,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Upload content to storage.

        Args:
            bucket: Bucket/container name
            key: Object key/path
            content: File content as bytes
            metadata: Optional metadata tags
            content_type: MIME type

        Returns:
            StoredObject with URI and metadata
        """

    @abstractmethod
    async def download(self, uri: str) -> bytes:
        """Download object content from storage.

        Args:
            uri: Storage URI (s3://bucket/key or file://bucket/key)

        Returns:
            File content as bytes

        Raises:
            ValueError: If URI is invalid
            FileNotFoundError: If object does not exist
        """

    @abstractmethod
    async def generate_presigned_url(
        self,
        uri: str,
        expiration_seconds: int = 3600,
    ) -> str:
        """Generate a presigned/temporary access URL.

        Args:
            uri: Storage URI
            expiration_seconds: URL validity duration (max 86400)

        Returns:
            Presigned URL (or local serving path for LocalObjectStorageService)
        """

    @abstractmethod
    async def delete(self, uri: str) -> None:
        """Delete an object from storage.

        Args:
            uri: Storage URI

        Raises:
            ValueError: If URI is invalid
        """

    @abstractmethod
    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
    ) -> list[str]:
        """List object keys by prefix.

        Args:
            bucket: Bucket/container name
            prefix: Key prefix filter

        Returns:
            List of object keys matching the prefix
        """

    @property
    @abstractmethod
    def storage_type(self) -> str:
        """Return storage type identifier (e.g. 's3', 'local')."""


class S3ObjectStorageService(IObjectStorageService):
    """AWS S3 and S3-compatible backend implementation.

    Works with:
    - AWS S3 (default, uses IAM/env credentials)
    - MinIO, DigitalOcean Spaces, Wasabi, etc. (via endpoint_url)

    When endpoint_url is provided, explicit access_key_id and secret_access_key
    are typically required for S3-compatible services.
    """

    def __init__(
        self,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
    ):
        from aiobotocore.session import get_session

        self.region = region or os.getenv("AWS_REGION", "eu-central-1")
        self.endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session = get_session()

    def _client_kwargs(self) -> dict:
        """Build kwargs for create_client()."""
        kwargs: dict = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self._access_key_id and self._secret_access_key:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        return kwargs

    async def upload(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Optional[dict[str, str]] = None,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        put_kwargs: dict = {
            "Bucket": bucket,
            "Key": key,
            "Body": content,
            "ContentType": content_type,
        }
        if metadata:
            put_kwargs["Metadata"] = metadata

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            await client.put_object(**put_kwargs)

        uri = f"s3://{bucket}/{key}"
        name = key.rsplit("/", 1)[-1] if "/" in key else key
        logger.info(f"Uploaded {len(content)} bytes to {uri}")

        return StoredObject(
            uri=uri,
            bucket=bucket,
            key=key,
            name=name,
            mime_type=content_type,
            size=len(content),
        )

    async def download(self, uri: str) -> bytes:
        bucket, key = parse_storage_uri(uri)
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            response = await client.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                return await stream.read()

    async def generate_presigned_url(
        self,
        uri: str,
        expiration_seconds: int = 3600,
    ) -> str:
        bucket, key = parse_storage_uri(uri)
        expiration_seconds = min(expiration_seconds, 86400)  # Max 24 hours

        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            url = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expiration_seconds,
            )
        logger.debug(f"Generated presigned URL for {uri} (expires in {expiration_seconds}s)")
        return url

    async def delete(self, uri: str) -> None:
        bucket, key = parse_storage_uri(uri)
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            await client.delete_object(Bucket=bucket, Key=key)
        logger.info(f"Deleted {uri}")

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
    ) -> list[str]:
        keys: list[str] = []
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        return keys

    @property
    def storage_type(self) -> str:
        return "s3"


class LocalObjectStorageService(IObjectStorageService):
    """Filesystem-based storage backend for local development.

    Files are stored under {root_path}/{bucket}/{key}. Metadata is stored in
    sidecar .meta.json files alongside each object.

    Presigned URLs are replaced with local API paths (e.g. /api/v1/files/local/{key}).
    """

    def __init__(
        self,
        root_path: Optional[str] = None,
        base_url: str = "/api/v1/files/local",
    ):
        self.root_path = Path(root_path or os.getenv("LOCAL_STORAGE_PATH", "./local-storage"))
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        logger.info(f"Local object storage initialized at: {self.root_path.resolve()}")

    def _object_path(self, bucket: str, key: str) -> Path:
        """Get the filesystem path for an object, preventing path traversal."""
        # Resolve to prevent traversal attacks
        resolved = (self.root_path / bucket / key).resolve()
        if not str(resolved).startswith(str(self.root_path.resolve())):
            raise ValueError(f"Path traversal detected: {bucket}/{key}")
        return resolved

    def _meta_path(self, obj_path: Path) -> Path:
        return obj_path.parent / f".{obj_path.name}.meta.json"

    async def upload(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Optional[dict[str, str]] = None,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        obj_path = self._object_path(bucket, key)
        obj_path.parent.mkdir(parents=True, exist_ok=True)

        # Write file content
        await asyncio.to_thread(obj_path.write_bytes, content)

        # Write metadata sidecar
        meta = {
            "content_type": content_type,
            "size": len(content),
            "metadata": metadata or {},
        }
        meta_path = self._meta_path(obj_path)
        await asyncio.to_thread(meta_path.write_text, json.dumps(meta))

        uri = f"file://{bucket}/{key}"
        name = key.rsplit("/", 1)[-1] if "/" in key else key
        logger.info(f"Uploaded {len(content)} bytes to local storage: {obj_path}")

        return StoredObject(
            uri=uri,
            bucket=bucket,
            key=key,
            name=name,
            mime_type=content_type,
            size=len(content),
        )

    async def download(self, uri: str) -> bytes:
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
        bucket, key = parse_storage_uri(uri)
        # Local storage doesn't have real presigned URLs — return a local API path
        return f"{self.base_url}/{key}"

    async def delete(self, uri: str) -> None:
        bucket, key = parse_storage_uri(uri)
        obj_path = self._object_path(bucket, key)
        meta_path = self._meta_path(obj_path)

        if obj_path.exists():
            await asyncio.to_thread(obj_path.unlink)
        if meta_path.exists():
            await asyncio.to_thread(meta_path.unlink)
        logger.info(f"Deleted local object: {uri}")

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
    ) -> list[str]:
        bucket_path = self._object_path(bucket, "").parent / bucket
        if not bucket_path.exists():
            return []

        keys: list[str] = []
        prefix_path = bucket_path / prefix if prefix else bucket_path
        # Walk the prefix directory
        if prefix_path.is_file():
            # Exact match
            keys.append(str(prefix_path.relative_to(bucket_path)))
        elif prefix_path.exists():
            for p in prefix_path.rglob("*"):
                if p.is_file() and not p.name.endswith(".meta.json"):
                    keys.append(str(p.relative_to(bucket_path)))
        else:
            # Prefix might be a partial path — scan parent
            parent = prefix_path.parent
            if parent.exists():
                stem = prefix_path.name
                for p in parent.rglob("*"):
                    if p.is_file() and not p.name.endswith(".meta.json"):
                        rel = str(p.relative_to(bucket_path))
                        if rel.startswith(prefix):
                            keys.append(rel)
        return sorted(keys)

    @property
    def storage_type(self) -> str:
        return "local"


# ---------------------------------------------------------------------------
# Factory & singleton
# ---------------------------------------------------------------------------

_object_storage_service: Optional[IObjectStorageService] = None


def create_object_storage_service(
    storage_type: Optional[str] = None,
    **kwargs,
) -> IObjectStorageService:
    """Create an object storage service based on configuration.

    Args:
        storage_type: "s3" (default) or "local". Falls back to OBJECT_STORAGE_TYPE env var.
        **kwargs: Additional keyword arguments passed to the storage service constructor.

    Returns:
        Configured IObjectStorageService instance
    """
    storage_type = storage_type or os.getenv("OBJECT_STORAGE_TYPE", "s3")

    if storage_type == "local":
        return LocalObjectStorageService(
            root_path=kwargs.get("root_path") or os.getenv("LOCAL_STORAGE_PATH"),
            base_url=kwargs.get("base_url", "/api/v1/files/local"),
        )
    elif storage_type == "s3":
        return S3ObjectStorageService(
            region=kwargs.get("region") or os.getenv("AWS_REGION"),
            endpoint_url=kwargs.get("endpoint_url") or os.getenv("S3_ENDPOINT_URL"),
            access_key_id=kwargs.get("access_key_id") or os.getenv("S3_ACCESS_KEY_ID"),
            secret_access_key=kwargs.get("secret_access_key") or os.getenv("S3_SECRET_ACCESS_KEY"),
        )
    else:
        raise ValueError(
            f"Unsupported storage type: {storage_type}. "
            f"Expected 's3' or 'local'. Set OBJECT_STORAGE_TYPE environment variable."
        )


def get_object_storage_service() -> IObjectStorageService:
    """Get the singleton object storage service instance.

    Creates the service on first call using environment variables for configuration.
    Thread-safe for typical async usage (single event loop).

    Returns:
        Shared IObjectStorageService instance
    """
    global _object_storage_service
    if _object_storage_service is None:
        _object_storage_service = create_object_storage_service()
    return _object_storage_service


def reset_object_storage_service() -> None:
    """Reset the singleton instance. Primarily for testing."""
    global _object_storage_service
    _object_storage_service = None
