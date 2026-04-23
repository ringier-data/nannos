"""Object storage abstraction for console-backend.

This is a standalone copy of the storage abstraction that mirrors the one in
agent-common but avoids the dependency on agent-common (which brings in heavy
LLM/LangChain dependencies not needed by console-backend).

Backends:
- S3ObjectStorageService: AWS S3 and any S3-compatible endpoint (MinIO, etc.)
- LocalObjectStorageService: Filesystem-based storage for local development
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
    """Represents a stored object in any backend."""

    uri: str
    bucket: str
    key: str
    name: str
    mime_type: str
    size: int


def parse_storage_uri(uri: str) -> tuple[str, str]:
    """Parse a storage URI (s3://bucket/key or file://bucket/key) into (bucket, key)."""
    parsed = urlparse(uri)
    if parsed.scheme not in ("s3", "file"):
        raise ValueError(f"Unsupported storage URI scheme: {parsed.scheme}")
    if not parsed.netloc:
        raise ValueError(f"Invalid storage URI: missing bucket in {uri}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not key:
        raise ValueError(f"Invalid storage URI: missing key in {uri}")
    return bucket, key


class IObjectStorageService(ABC):
    """Abstract interface for object storage operations."""

    @abstractmethod
    async def upload(self, bucket: str, key: str, content: bytes,
                     metadata: Optional[dict[str, str]] = None,
                     content_type: str = "application/octet-stream") -> StoredObject: ...

    @abstractmethod
    async def download(self, uri: str) -> bytes: ...

    @abstractmethod
    async def generate_presigned_url(self, uri: str, expiration_seconds: int = 3600) -> str: ...

    @abstractmethod
    async def delete(self, uri: str) -> None: ...

    @abstractmethod
    async def list_objects(self, bucket: str, prefix: str = "") -> list[str]: ...

    @property
    @abstractmethod
    def storage_type(self) -> str: ...


class S3ObjectStorageService(IObjectStorageService):
    """AWS S3 and S3-compatible backend."""

    def __init__(self, region: Optional[str] = None, endpoint_url: Optional[str] = None,
                 access_key_id: Optional[str] = None, secret_access_key: Optional[str] = None):
        from aiobotocore.session import get_session
        self.region = region or os.getenv("AWS_REGION", "eu-central-1")
        self.endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session = get_session()

    def _client_kwargs(self) -> dict:
        kwargs: dict = {"region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self._access_key_id and self._secret_access_key:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        return kwargs

    async def upload(self, bucket: str, key: str, content: bytes,
                     metadata: Optional[dict[str, str]] = None,
                     content_type: str = "application/octet-stream") -> StoredObject:
        put_kwargs: dict = {"Bucket": bucket, "Key": key, "Body": content, "ContentType": content_type}
        if metadata:
            put_kwargs["Metadata"] = metadata
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            await client.put_object(**put_kwargs)
        uri = f"s3://{bucket}/{key}"
        name = key.rsplit("/", 1)[-1] if "/" in key else key
        return StoredObject(uri=uri, bucket=bucket, key=key, name=name, mime_type=content_type, size=len(content))

    async def download(self, uri: str) -> bytes:
        bucket, key = parse_storage_uri(uri)
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            response = await client.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                return await stream.read()

    async def generate_presigned_url(self, uri: str, expiration_seconds: int = 3600) -> str:
        bucket, key = parse_storage_uri(uri)
        expiration_seconds = min(expiration_seconds, 86400)
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            return await client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expiration_seconds)

    async def delete(self, uri: str) -> None:
        bucket, key = parse_storage_uri(uri)
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            await client.delete_object(Bucket=bucket, Key=key)

    async def list_objects(self, bucket: str, prefix: str = "") -> list[str]:
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
    """Filesystem-based storage backend for local development."""

    def __init__(self, root_path: Optional[str] = None, base_url: str = "/api/v1/files/local"):
        self.root_path = Path(root_path or os.getenv("LOCAL_STORAGE_PATH", "./local-storage"))
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")

    def _object_path(self, bucket: str, key: str) -> Path:
        resolved = (self.root_path / bucket / key).resolve()
        if not str(resolved).startswith(str(self.root_path.resolve())):
            raise ValueError(f"Path traversal detected: {bucket}/{key}")
        return resolved

    def _meta_path(self, obj_path: Path) -> Path:
        return obj_path.parent / f".{obj_path.name}.meta.json"

    async def upload(self, bucket: str, key: str, content: bytes,
                     metadata: Optional[dict[str, str]] = None,
                     content_type: str = "application/octet-stream") -> StoredObject:
        obj_path = self._object_path(bucket, key)
        obj_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(obj_path.write_bytes, content)
        meta = {"content_type": content_type, "size": len(content), "metadata": metadata or {}}
        await asyncio.to_thread(self._meta_path(obj_path).write_text, json.dumps(meta))
        uri = f"file://{bucket}/{key}"
        name = key.rsplit("/", 1)[-1] if "/" in key else key
        return StoredObject(uri=uri, bucket=bucket, key=key, name=name, mime_type=content_type, size=len(content))

    async def download(self, uri: str) -> bytes:
        bucket, key = parse_storage_uri(uri)
        obj_path = self._object_path(bucket, key)
        if not obj_path.exists():
            raise FileNotFoundError(f"Object not found: {uri}")
        return await asyncio.to_thread(obj_path.read_bytes)

    async def generate_presigned_url(self, uri: str, expiration_seconds: int = 3600) -> str:
        bucket, key = parse_storage_uri(uri)
        return f"{self.base_url}/{key}"

    async def delete(self, uri: str) -> None:
        bucket, key = parse_storage_uri(uri)
        obj_path = self._object_path(bucket, key)
        meta_path = self._meta_path(obj_path)
        if obj_path.exists():
            await asyncio.to_thread(obj_path.unlink)
        if meta_path.exists():
            await asyncio.to_thread(meta_path.unlink)

    async def list_objects(self, bucket: str, prefix: str = "") -> list[str]:
        bucket_path = self.root_path / bucket
        if not bucket_path.exists():
            return []
        keys: list[str] = []
        prefix_path = bucket_path / prefix if prefix else bucket_path
        if prefix_path.is_file():
            keys.append(str(prefix_path.relative_to(bucket_path)))
        elif prefix_path.exists():
            for p in prefix_path.rglob("*"):
                if p.is_file() and not p.name.endswith(".meta.json"):
                    keys.append(str(p.relative_to(bucket_path)))
        return sorted(keys)

    @property
    def storage_type(self) -> str:
        return "local"


def create_object_storage_service(
    storage_type: Optional[str] = None, **kwargs
) -> IObjectStorageService:
    """Create an object storage service based on configuration."""
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
        raise ValueError(f"Unsupported storage type: {storage_type}")
