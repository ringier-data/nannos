"""Tests for object storage abstraction layer.

Tests cover:
1. StoredObject dataclass
2. parse_storage_uri function
3. LocalObjectStorageService (filesystem operations)
4. S3ObjectStorageService (mocked aiobotocore)
5. Factory function create_object_storage_service
6. Singleton get_object_storage_service
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_common.core.object_storage import (
    IObjectStorageService,
    LocalObjectStorageService,
    S3ObjectStorageService,
    StoredObject,
    create_object_storage_service,
    get_object_storage_service,
    parse_storage_uri,
    reset_object_storage_service,
)


# ---------------------------------------------------------------------------
# StoredObject tests
# ---------------------------------------------------------------------------


class TestStoredObject:
    """Tests for the StoredObject dataclass."""

    def test_create_stored_object(self):
        """Test basic StoredObject creation."""
        obj = StoredObject(
            uri="s3://bucket/path/to/file.txt",
            bucket="bucket",
            key="path/to/file.txt",
            name="file.txt",
            mime_type="text/plain",
            size=1234,
        )
        assert obj.uri == "s3://bucket/path/to/file.txt"
        assert obj.bucket == "bucket"
        assert obj.key == "path/to/file.txt"
        assert obj.name == "file.txt"
        assert obj.mime_type == "text/plain"
        assert obj.size == 1234

    def test_stored_object_with_local_uri(self):
        """Test StoredObject with local file:// URI."""
        obj = StoredObject(
            uri="file://local-bucket/uploads/doc.pdf",
            bucket="local-bucket",
            key="uploads/doc.pdf",
            name="doc.pdf",
            mime_type="application/pdf",
            size=5678,
        )
        assert obj.uri.startswith("file://")


# ---------------------------------------------------------------------------
# parse_storage_uri tests
# ---------------------------------------------------------------------------


class TestParseStorageUri:
    """Tests for the parse_storage_uri function."""

    def test_parse_s3_uri(self):
        """Test parsing S3 URI."""
        bucket, key = parse_storage_uri("s3://my-bucket/path/to/file.txt")
        assert bucket == "my-bucket"
        assert key == "path/to/file.txt"

    def test_parse_s3_uri_nested_key(self):
        """Test parsing S3 URI with deeply nested key."""
        bucket, key = parse_storage_uri("s3://bucket/a/b/c/d/file.json")
        assert bucket == "bucket"
        assert key == "a/b/c/d/file.json"

    def test_parse_file_uri(self):
        """Test parsing local file:// URI."""
        bucket, key = parse_storage_uri("file://local-storage/uploads/doc.pdf")
        assert bucket == "local-storage"
        assert key == "uploads/doc.pdf"

    def test_parse_invalid_scheme_raises(self):
        """Test that invalid scheme raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported storage URI scheme"):
            parse_storage_uri("https://example.com/file.txt")

    def test_parse_missing_bucket_raises(self):
        """Test that missing bucket raises ValueError."""
        with pytest.raises(ValueError, match="missing bucket name"):
            parse_storage_uri("s3:///just-a-key")

    def test_parse_missing_key_raises(self):
        """Test that missing key raises ValueError."""
        with pytest.raises(ValueError, match="missing key"):
            parse_storage_uri("s3://bucket-only/")


# ---------------------------------------------------------------------------
# LocalObjectStorageService tests
# ---------------------------------------------------------------------------


class TestLocalObjectStorageService:
    """Tests for the LocalObjectStorageService."""

    @pytest.fixture
    def temp_storage(self):
        """Create a temporary storage directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def local_service(self, temp_storage):
        """Create a LocalObjectStorageService with temp directory."""
        return LocalObjectStorageService(root_path=temp_storage, base_url="/api/v1/files")

    @pytest.mark.asyncio
    async def test_upload_creates_file(self, local_service, temp_storage):
        """Test that upload creates the file on disk."""
        content = b"Hello, World!"
        result = await local_service.upload(
            bucket="test-bucket",
            key="hello.txt",
            content=content,
            content_type="text/plain",
        )

        assert result.uri == "file://test-bucket/hello.txt"
        assert result.bucket == "test-bucket"
        assert result.key == "hello.txt"
        assert result.name == "hello.txt"
        assert result.mime_type == "text/plain"
        assert result.size == len(content)

        # Verify file exists on disk
        file_path = Path(temp_storage) / "test-bucket" / "hello.txt"
        assert file_path.exists()
        assert file_path.read_bytes() == content

    @pytest.mark.asyncio
    async def test_upload_creates_nested_directories(self, local_service, temp_storage):
        """Test that upload creates nested directory structure."""
        content = b"nested content"
        await local_service.upload(
            bucket="bucket",
            key="a/b/c/nested.txt",
            content=content,
        )

        file_path = Path(temp_storage) / "bucket" / "a" / "b" / "c" / "nested.txt"
        assert file_path.exists()

    @pytest.mark.asyncio
    async def test_upload_writes_metadata(self, local_service, temp_storage):
        """Test that upload creates metadata sidecar file."""
        await local_service.upload(
            bucket="bucket",
            key="file.txt",
            content=b"content",
            content_type="text/plain",
            metadata={"author": "test"},
        )

        meta_path = Path(temp_storage) / "bucket" / ".file.txt.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["content_type"] == "text/plain"
        assert meta["metadata"]["author"] == "test"

    @pytest.mark.asyncio
    async def test_download_returns_content(self, local_service):
        """Test downloading file content."""
        content = b"download me"
        await local_service.upload(bucket="bucket", key="dl.txt", content=content)

        downloaded = await local_service.download("file://bucket/dl.txt")
        assert downloaded == content

    @pytest.mark.asyncio
    async def test_download_not_found_raises(self, local_service):
        """Test that downloading missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            await local_service.download("file://bucket/missing.txt")

    @pytest.mark.asyncio
    async def test_generate_presigned_url_returns_local_path(self, local_service):
        """Test that presigned URL returns local API path."""
        url = await local_service.generate_presigned_url("file://bucket/file.txt")
        assert url == "/api/v1/files/file.txt"

    @pytest.mark.asyncio
    async def test_delete_removes_file_and_metadata(self, local_service, temp_storage):
        """Test that delete removes both file and metadata."""
        await local_service.upload(bucket="bucket", key="delete-me.txt", content=b"x")

        file_path = Path(temp_storage) / "bucket" / "delete-me.txt"
        meta_path = Path(temp_storage) / "bucket" / ".delete-me.txt.meta.json"
        assert file_path.exists()
        assert meta_path.exists()

        await local_service.delete("file://bucket/delete-me.txt")
        assert not file_path.exists()
        assert not meta_path.exists()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_file_succeeds(self, local_service):
        """Test that deleting nonexistent file doesn't raise."""
        # Should not raise
        await local_service.delete("file://bucket/nonexistent.txt")

    @pytest.mark.asyncio
    async def test_list_objects_returns_keys(self, local_service):
        """Test listing objects returns correct keys."""
        await local_service.upload(bucket="bucket", key="a/file1.txt", content=b"1")
        await local_service.upload(bucket="bucket", key="a/file2.txt", content=b"2")
        await local_service.upload(bucket="bucket", key="b/file3.txt", content=b"3")

        # List with prefix
        keys = await local_service.list_objects(bucket="bucket", prefix="a/")
        assert sorted(keys) == ["a/file1.txt", "a/file2.txt"]

    @pytest.mark.asyncio
    async def test_list_objects_empty_bucket(self, local_service):
        """Test listing objects in nonexistent bucket returns empty list."""
        keys = await local_service.list_objects(bucket="nonexistent", prefix="")
        assert keys == []

    def test_storage_type_returns_local(self, local_service):
        """Test storage_type property returns 'local'."""
        assert local_service.storage_type == "local"

    @pytest.mark.asyncio
    async def test_path_traversal_prevented(self, local_service):
        """Test that path traversal attacks are prevented."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            await local_service.upload(
                bucket="bucket",
                key="../../../etc/passwd",
                content=b"malicious",
            )


# ---------------------------------------------------------------------------
# S3ObjectStorageService tests (mocked)
# ---------------------------------------------------------------------------


class TestS3ObjectStorageService:
    """Tests for S3ObjectStorageService with mocked aiobotocore."""

    @pytest.fixture
    def mock_s3_client(self):
        """Create a mock S3 client."""
        client = AsyncMock()
        client.put_object = AsyncMock()
        client.get_object = AsyncMock()
        client.delete_object = AsyncMock()
        client.generate_presigned_url = AsyncMock(return_value="https://presigned.example.com/file")

        # Mock paginator for list_objects
        paginator = MagicMock()
        async_pages = AsyncMock()
        async_pages.__aiter__.return_value = iter([
            {"Contents": [{"Key": "file1.txt"}, {"Key": "file2.txt"}]}
        ])
        paginator.paginate.return_value = async_pages
        client.get_paginator = MagicMock(return_value=paginator)

        return client

    @pytest.fixture
    def s3_service(self, mock_s3_client):
        """Create S3ObjectStorageService with mocked client."""
        with patch("agent_common.core.object_storage.get_session") as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance

            # Create async context manager for client
            cm = AsyncMock()
            cm.__aenter__.return_value = mock_s3_client
            cm.__aexit__.return_value = None
            session_instance.create_client.return_value = cm

            service = S3ObjectStorageService(
                region="us-east-1",
                endpoint_url=None,
            )
            # Store mock for assertions
            service._mock_client = mock_s3_client
            yield service

    @pytest.mark.asyncio
    async def test_upload_calls_put_object(self, s3_service):
        """Test that upload calls S3 put_object."""
        content = b"test content"
        result = await s3_service.upload(
            bucket="test-bucket",
            key="path/to/file.txt",
            content=content,
            content_type="text/plain",
            metadata={"key": "value"},
        )

        assert result.uri == "s3://test-bucket/path/to/file.txt"
        assert result.bucket == "test-bucket"
        assert result.key == "path/to/file.txt"
        assert result.name == "file.txt"
        assert result.size == len(content)

        s3_service._mock_client.put_object.assert_called_once()
        call_kwargs = s3_service._mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == "path/to/file.txt"
        assert call_kwargs["Body"] == content
        assert call_kwargs["ContentType"] == "text/plain"
        assert call_kwargs["Metadata"] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_download_returns_content(self, s3_service):
        """Test that download returns S3 object content."""
        expected_content = b"downloaded content"

        # Mock the response body as an async context manager
        body_stream = AsyncMock()
        body_stream.__aenter__.return_value = body_stream
        body_stream.__aexit__.return_value = None
        body_stream.read = AsyncMock(return_value=expected_content)
        s3_service._mock_client.get_object.return_value = {"Body": body_stream}

        content = await s3_service.download("s3://bucket/file.txt")
        assert content == expected_content

    @pytest.mark.asyncio
    async def test_generate_presigned_url(self, s3_service):
        """Test presigned URL generation."""
        url = await s3_service.generate_presigned_url(
            uri="s3://bucket/file.txt",
            expiration_seconds=3600,
        )

        assert url == "https://presigned.example.com/file"
        s3_service._mock_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "bucket", "Key": "file.txt"},
            ExpiresIn=3600,
        )

    @pytest.mark.asyncio
    async def test_presigned_url_max_expiration(self, s3_service):
        """Test that presigned URL expiration is capped at 24 hours."""
        await s3_service.generate_presigned_url(
            uri="s3://bucket/file.txt",
            expiration_seconds=100000,  # More than 24 hours
        )

        call_args = s3_service._mock_client.generate_presigned_url.call_args
        assert call_args[1]["ExpiresIn"] == 86400  # 24 hours max

    @pytest.mark.asyncio
    async def test_delete_calls_delete_object(self, s3_service):
        """Test that delete calls S3 delete_object."""
        await s3_service.delete("s3://bucket/file.txt")

        s3_service._mock_client.delete_object.assert_called_once_with(
            Bucket="bucket", Key="file.txt"
        )

    def test_storage_type_returns_s3(self, s3_service):
        """Test storage_type property returns 's3'."""
        assert s3_service.storage_type == "s3"

    def test_client_kwargs_with_endpoint_url(self):
        """Test that endpoint_url is included in client kwargs."""
        with patch("agent_common.core.object_storage.get_session"):
            service = S3ObjectStorageService(
                region="us-east-1",
                endpoint_url="https://minio.example.com",
                access_key_id="access",
                secret_access_key="secret",
            )
            kwargs = service._client_kwargs()
            assert kwargs["endpoint_url"] == "https://minio.example.com"
            assert kwargs["aws_access_key_id"] == "access"
            assert kwargs["aws_secret_access_key"] == "secret"


# ---------------------------------------------------------------------------
# Factory function tests
# ---------------------------------------------------------------------------


class TestCreateObjectStorageService:
    """Tests for create_object_storage_service factory."""

    def test_create_local_service(self):
        """Test creating local storage service."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = create_object_storage_service(
                storage_type="local",
                root_path=tmpdir,
            )
            assert isinstance(service, LocalObjectStorageService)
            assert service.storage_type == "local"

    def test_create_s3_service(self):
        """Test creating S3 storage service."""
        with patch("agent_common.core.object_storage.get_session"):
            service = create_object_storage_service(storage_type="s3")
            assert isinstance(service, S3ObjectStorageService)
            assert service.storage_type == "s3"

    def test_create_s3_service_with_endpoint(self):
        """Test creating S3-compatible service with custom endpoint."""
        with patch("agent_common.core.object_storage.get_session"):
            service = create_object_storage_service(
                storage_type="s3",
                endpoint_url="https://minio.example.com",
                access_key_id="access",
                secret_access_key="secret",
            )
            assert isinstance(service, S3ObjectStorageService)
            assert service.endpoint_url == "https://minio.example.com"

    def test_create_from_env_var(self, monkeypatch):
        """Test that storage type falls back to OBJECT_STORAGE_TYPE env var."""
        monkeypatch.setenv("OBJECT_STORAGE_TYPE", "local")
        monkeypatch.setenv("LOCAL_STORAGE_PATH", "/tmp/test-storage")

        service = create_object_storage_service()
        assert isinstance(service, LocalObjectStorageService)

    def test_invalid_storage_type_raises(self):
        """Test that invalid storage type raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported storage type"):
            create_object_storage_service(storage_type="azure")


# ---------------------------------------------------------------------------
# Singleton tests
# ---------------------------------------------------------------------------


class TestSingleton:
    """Tests for get_object_storage_service singleton."""

    def teardown_method(self):
        """Reset singleton after each test."""
        reset_object_storage_service()

    def test_singleton_returns_same_instance(self, monkeypatch):
        """Test that get_object_storage_service returns same instance."""
        monkeypatch.setenv("OBJECT_STORAGE_TYPE", "local")
        monkeypatch.setenv("LOCAL_STORAGE_PATH", "/tmp/singleton-test")

        service1 = get_object_storage_service()
        service2 = get_object_storage_service()
        assert service1 is service2

    def test_reset_clears_singleton(self, monkeypatch):
        """Test that reset_object_storage_service clears the singleton."""
        monkeypatch.setenv("OBJECT_STORAGE_TYPE", "local")
        monkeypatch.setenv("LOCAL_STORAGE_PATH", "/tmp/reset-test")

        service1 = get_object_storage_service()
        reset_object_storage_service()
        service2 = get_object_storage_service()
        assert service1 is not service2


# ---------------------------------------------------------------------------
# Interface compliance tests
# ---------------------------------------------------------------------------


class TestInterfaceCompliance:
    """Test that implementations comply with IObjectStorageService interface."""

    def test_local_service_implements_interface(self):
        """Test LocalObjectStorageService implements all abstract methods."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LocalObjectStorageService(root_path=tmpdir)
            assert isinstance(service, IObjectStorageService)

    def test_s3_service_implements_interface(self):
        """Test S3ObjectStorageService implements all abstract methods."""
        with patch("agent_common.core.object_storage.get_session"):
            service = S3ObjectStorageService()
            assert isinstance(service, IObjectStorageService)
