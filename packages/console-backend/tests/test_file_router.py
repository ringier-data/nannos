"""Unit tests for file router - upload and URL regeneration endpoints."""

from unittest.mock import AsyncMock, patch

import pytest

from playground_backend.models.user import User


@pytest.mark.asyncio
class TestFileRouter:
    """Tests for file upload and URL regeneration endpoints."""

    async def test_regenerate_urls_success(self, aws_mock, client_with_db, test_user: User):
        """Test successful URL regeneration for user's files."""
        # Mock the FileStorageService
        mock_storage = AsyncMock()
        mock_storage._prefix = "uploads"
        mock_storage._sanitize_segment = lambda val, default: val.replace("@", "-")
        mock_storage.generate_presigned_get_url = AsyncMock(
            return_value="https://s3.amazonaws.com/bucket/uploads/test-user/file.pdf?X-Amz-Expires=3600"
        )

        with patch(
            "playground_backend.routers.file_router.getattr",
            return_value=mock_storage,
        ):
            response = await client_with_db.post(
                "/api/v1/files/regenerate-urls",
                json={
                    "files": [
                        {
                            "key": f"uploads/{test_user.id}/conversation-123/file.pdf",
                            "name": "file.pdf",
                        }
                    ]
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "files" in data
        assert len(data["files"]) == 1
        assert data["files"][0]["key"] == f"uploads/{test_user.id}/conversation-123/file.pdf"
        assert data["files"][0]["name"] == "file.pdf"
        assert "X-Amz-Expires" in data["files"][0]["url"]

    async def test_regenerate_urls_rejects_other_users_files(self, aws_mock, client_with_db, test_user: User):
        """Test that regenerating URLs for other users' files is rejected."""
        mock_storage = AsyncMock()
        mock_storage._prefix = "uploads"
        mock_storage._sanitize_segment = lambda val, default: val.replace("@", "-")

        with patch(
            "playground_backend.routers.file_router.getattr",
            return_value=mock_storage,
        ):
            response = await client_with_db.post(
                "/api/v1/files/regenerate-urls",
                json={
                    "files": [
                        {
                            "key": "uploads/other-user-id/conversation-123/file.pdf",
                            "name": "file.pdf",
                        }
                    ]
                },
            )

        assert response.status_code == 403
        assert "Access denied" in response.json()["detail"]

    async def test_regenerate_urls_rejects_invalid_key_format(self, aws_mock, client_with_db, test_user: User):
        """Test that invalid S3 key formats are rejected."""
        mock_storage = AsyncMock()
        mock_storage._prefix = "uploads"

        with patch(
            "playground_backend.routers.file_router.getattr",
            return_value=mock_storage,
        ):
            response = await client_with_db.post(
                "/api/v1/files/regenerate-urls",
                json={
                    "files": [
                        {
                            "key": "invalid/key/format/file.pdf",
                            "name": "file.pdf",
                        }
                    ]
                },
            )

        assert response.status_code == 403
        assert "Invalid file key format" in response.json()["detail"]

    async def test_regenerate_urls_requires_file_key(self, aws_mock, client_with_db, test_user: User):
        """Test that requests without file keys are rejected."""
        response = await client_with_db.post(
            "/api/v1/files/regenerate-urls",
            json={"files": [{"name": "file.pdf"}]},  # Missing 'key'
        )

        assert response.status_code == 400
        assert "must have a 'key' field" in response.json()["detail"]

    async def test_regenerate_urls_requires_at_least_one_file(self, aws_mock, client_with_db, test_user: User):
        """Test that empty file list is rejected."""
        response = await client_with_db.post(
            "/api/v1/files/regenerate-urls",
            json={"files": []},
        )

        assert response.status_code == 400
        assert "At least one file key must be provided" in response.json()["detail"]

    async def test_regenerate_urls_handles_multiple_files(self, aws_mock, client_with_db, test_user: User):
        """Test batch URL regeneration for multiple files."""
        mock_storage = AsyncMock()
        mock_storage._prefix = "uploads"
        mock_storage._sanitize_segment = lambda val, default: val.replace("@", "-")

        # Mock to return different URLs for different keys
        async def mock_generate_url(key):
            return f"https://s3.amazonaws.com/bucket/{key}?X-Amz-Expires=3600"

        mock_storage.generate_presigned_get_url = mock_generate_url

        with patch(
            "playground_backend.routers.file_router.getattr",
            return_value=mock_storage,
        ):
            response = await client_with_db.post(
                "/api/v1/files/regenerate-urls",
                json={
                    "files": [
                        {
                            "key": f"uploads/{test_user.id}/conversation-123/file1.pdf",
                            "name": "file1.pdf",
                        },
                        {
                            "key": f"uploads/{test_user.id}/conversation-123/file2.png",
                            "name": "file2.png",
                        },
                    ]
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["files"]) == 2
        assert data["files"][0]["name"] == "file1.pdf"
        assert data["files"][1]["name"] == "file2.png"
        assert all("X-Amz-Expires" in f["url"] for f in data["files"])
