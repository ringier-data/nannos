"""Tests for agent_common.core.object_storage module."""

import json
import os
from pathlib import Path
from unittest.mock import patch

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
# parse_storage_uri
# ---------------------------------------------------------------------------


class TestParseStorageUri:
    def test_s3_uri(self):
        bucket, key = parse_storage_uri("s3://my-bucket/some/path/file.txt")
        assert bucket == "my-bucket"
        assert key == "some/path/file.txt"

    def test_file_uri(self):
        bucket, key = parse_storage_uri("file://local-bucket/docs/report.pdf")
        assert bucket == "local-bucket"
        assert key == "docs/report.pdf"

    def test_unsupported_scheme(self):
        with pytest.raises(ValueError, match="Unsupported storage URI scheme"):
            parse_storage_uri("gcs://bucket/key")

    def test_missing_bucket(self):
        with pytest.raises(ValueError, match="missing bucket"):
            parse_storage_uri("s3:///key")

    def test_missing_key(self):
        with pytest.raises(ValueError, match="missing key"):
            parse_storage_uri("s3://bucket")

    def test_missing_key_trailing_slash(self):
        with pytest.raises(ValueError, match="missing key"):
            parse_storage_uri("s3://bucket/")


# ---------------------------------------------------------------------------
# LocalObjectStorageService
# ---------------------------------------------------------------------------


class TestLocalObjectStorageService:
    @pytest.fixture
    def local_storage(self, tmp_path: Path) -> LocalObjectStorageService:
        return LocalObjectStorageService(root_path=str(tmp_path))

    @pytest.mark.asyncio
    async def test_upload_and_download(self, local_storage: LocalObjectStorageService):
        content = b"hello world"
        stored = await local_storage.upload("test-bucket", "docs/file.txt", content, content_type="text/plain")

        assert isinstance(stored, StoredObject)
        assert stored.uri == "file://test-bucket/docs/file.txt"
        assert stored.bucket == "test-bucket"
        assert stored.key == "docs/file.txt"
        assert stored.name == "file.txt"
        assert stored.mime_type == "text/plain"
        assert stored.size == len(content)

        downloaded = await local_storage.download(stored.uri)
        assert downloaded == content

    @pytest.mark.asyncio
    async def test_upload_creates_metadata_sidecar(self, local_storage: LocalObjectStorageService, tmp_path: Path):
        await local_storage.upload(
            "bucket", "file.bin", b"\x00\x01", metadata={"author": "test"}, content_type="application/octet-stream"
        )

        meta_path = tmp_path / "bucket" / ".file.bin.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["content_type"] == "application/octet-stream"
        assert meta["size"] == 2
        assert meta["metadata"]["author"] == "test"

    @pytest.mark.asyncio
    async def test_download_not_found(self, local_storage: LocalObjectStorageService):
        with pytest.raises(FileNotFoundError):
            await local_storage.download("file://bucket/nonexistent.txt")

    @pytest.mark.asyncio
    async def test_generate_presigned_url(self, local_storage: LocalObjectStorageService):
        await local_storage.upload("bucket", "path/file.txt", b"data")
        url = await local_storage.generate_presigned_url("file://bucket/path/file.txt")
        assert url == "/api/v1/files/local/path/file.txt"

    @pytest.mark.asyncio
    async def test_delete(self, local_storage: LocalObjectStorageService, tmp_path: Path):
        await local_storage.upload("bucket", "to-delete.txt", b"temp")
        assert (tmp_path / "bucket" / "to-delete.txt").exists()

        await local_storage.delete("file://bucket/to-delete.txt")
        assert not (tmp_path / "bucket" / "to-delete.txt").exists()
        assert not (tmp_path / "bucket" / ".to-delete.txt.meta.json").exists()

    @pytest.mark.asyncio
    async def test_list_objects(self, local_storage: LocalObjectStorageService):
        await local_storage.upload("bucket", "a/1.txt", b"1")
        await local_storage.upload("bucket", "a/2.txt", b"2")
        await local_storage.upload("bucket", "b/3.txt", b"3")

        keys = await local_storage.list_objects("bucket", "a/")
        assert sorted(keys) == ["a/1.txt", "a/2.txt"]

    @pytest.mark.asyncio
    async def test_list_objects_excludes_meta(self, local_storage: LocalObjectStorageService):
        await local_storage.upload("bucket", "file.txt", b"data", metadata={"k": "v"})
        keys = await local_storage.list_objects("bucket")
        assert keys == ["file.txt"]

    @pytest.mark.asyncio
    async def test_path_traversal_prevention(self, local_storage: LocalObjectStorageService):
        with pytest.raises(ValueError, match="Path traversal"):
            await local_storage.upload("bucket", "../../etc/passwd", b"bad")

    def test_storage_type(self, local_storage: LocalObjectStorageService):
        assert local_storage.storage_type == "local"


# ---------------------------------------------------------------------------
# S3ObjectStorageService (mocked)
# ---------------------------------------------------------------------------


class TestS3ObjectStorageService:
    def test_storage_type(self):
        with patch("aiobotocore.session.get_session"):
            svc = S3ObjectStorageService(region="us-east-1")
        assert svc.storage_type == "s3"

    def test_client_kwargs_basic(self):
        with patch("aiobotocore.session.get_session"):
            svc = S3ObjectStorageService(region="us-west-2")
        assert svc._client_kwargs() == {"region_name": "us-west-2"}

    def test_client_kwargs_with_endpoint(self):
        with patch("aiobotocore.session.get_session"):
            svc = S3ObjectStorageService(
                region="us-east-1",
                endpoint_url="http://localhost:9000",
                access_key_id="minioadmin",
                secret_access_key="minioadmin",
            )
        kwargs = svc._client_kwargs()
        assert kwargs["endpoint_url"] == "http://localhost:9000"
        assert kwargs["aws_access_key_id"] == "minioadmin"
        assert kwargs["aws_secret_access_key"] == "minioadmin"


# ---------------------------------------------------------------------------
# Factory & singleton
# ---------------------------------------------------------------------------


class TestFactory:
    def setup_method(self):
        reset_object_storage_service()

    def teardown_method(self):
        reset_object_storage_service()

    def test_create_local(self, tmp_path: Path):
        svc = create_object_storage_service("local", root_path=str(tmp_path))
        assert isinstance(svc, LocalObjectStorageService)

    def test_create_s3(self):
        with patch("aiobotocore.session.get_session"):
            svc = create_object_storage_service("s3", region="us-east-1")
        assert isinstance(svc, S3ObjectStorageService)

    def test_create_unsupported(self):
        with pytest.raises(ValueError, match="Unsupported storage type"):
            create_object_storage_service("azure-blob")

    def test_create_from_env(self, tmp_path: Path):
        with patch.dict(os.environ, {"OBJECT_STORAGE_TYPE": "local", "LOCAL_STORAGE_PATH": str(tmp_path)}):
            svc = create_object_storage_service()
        assert isinstance(svc, LocalObjectStorageService)

    def test_singleton(self, tmp_path: Path):
        with patch.dict(os.environ, {"OBJECT_STORAGE_TYPE": "local", "LOCAL_STORAGE_PATH": str(tmp_path)}):
            svc1 = get_object_storage_service()
            svc2 = get_object_storage_service()
        assert svc1 is svc2

    def test_reset_singleton(self, tmp_path: Path):
        with patch.dict(os.environ, {"OBJECT_STORAGE_TYPE": "local", "LOCAL_STORAGE_PATH": str(tmp_path)}):
            svc1 = get_object_storage_service()
            reset_object_storage_service()
            svc2 = get_object_storage_service()
        assert svc1 is not svc2
