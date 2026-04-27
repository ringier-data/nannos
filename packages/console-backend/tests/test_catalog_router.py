"""Tests for catalog API endpoints.

Tests cover HTTP-level integration:
- CRUD endpoints (create, get, list, update, delete)
- Sync trigger endpoint
- Sync status endpoint
- Permission checks via HTTP responses
"""

import pytest
from httpx import AsyncClient

# --- Helpers ---


async def _create_catalog_via_api(client: AsyncClient, name: str = "Test Catalog") -> dict:
    """Create a catalog via the API and return the response body."""
    response = await client.post(
        "/api/v1/catalogs",
        json={
            "name": name,
            "description": "Test description",
            "source_type": "google_drive",
            "source_config": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Tests ---


class TestCatalogEndpoints:
    """Test catalog CRUD endpoints."""

    @pytest.mark.asyncio
    async def test_create_catalog(self, client_with_db: AsyncClient):
        data = await _create_catalog_via_api(client_with_db)
        assert data["name"] == "Test Catalog"
        assert data["source_type"] == "google_drive"
        assert data["status"] == "active"
        assert data["has_connection"] is False

    @pytest.mark.asyncio
    async def test_list_catalogs(self, client_with_db: AsyncClient):
        await _create_catalog_via_api(client_with_db, "Cat A")
        await _create_catalog_via_api(client_with_db, "Cat B")

        response = await client_with_db.get("/api/v1/catalogs")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 2
        names = {c["name"] for c in body["items"]}
        assert "Cat A" in names
        assert "Cat B" in names

    @pytest.mark.asyncio
    async def test_get_catalog(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.get(f"/api/v1/catalogs/{catalog_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == catalog_id
        assert data["name"] == "Test Catalog"
        assert data["owner"] is not None

    @pytest.mark.asyncio
    async def test_get_catalog_not_found(self, client_with_db: AsyncClient):
        response = await client_with_db.get("/api/v1/catalogs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_catalog(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.patch(
            f"/api/v1/catalogs/{catalog_id}",
            json={"name": "Updated Name", "description": "New description"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data["description"] == "New description"

    @pytest.mark.asyncio
    async def test_update_catalog_source_config(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.patch(
            f"/api/v1/catalogs/{catalog_id}",
            json={
                "source_config": {
                    "shared_drive_id": "drive-abc",
                    "shared_drive_name": "Marketing",
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["source_config"]["shared_drive_id"] == "drive-abc"

    @pytest.mark.asyncio
    async def test_delete_catalog(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.delete(f"/api/v1/catalogs/{catalog_id}")
        assert response.status_code == 204

        # Verify deleted
        response = await client_with_db.get(f"/api/v1/catalogs/{catalog_id}")
        assert response.status_code == 404


class TestCatalogSyncEndpoints:
    """Test sync-related endpoints."""

    @pytest.mark.asyncio
    async def test_trigger_sync_without_drive_returns_400(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.post(f"/api/v1/catalogs/{catalog_id}/sync")
        assert response.status_code == 400
        assert "no sources configured" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_trigger_sync_with_drive(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        # Set source config with shared drive
        await client_with_db.patch(
            f"/api/v1/catalogs/{catalog_id}",
            json={"source_config": {"shared_drive_id": "drive-xyz"}},
        )

        response = await client_with_db.post(f"/api/v1/catalogs/{catalog_id}/sync")
        assert response.status_code == 202
        data = response.json()
        assert data["catalog_id"] == catalog_id
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_trigger_sync_duplicate_returns_400(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        await client_with_db.patch(
            f"/api/v1/catalogs/{catalog_id}",
            json={"source_config": {"shared_drive_id": "drive-xyz"}},
        )

        # First sync
        response = await client_with_db.post(f"/api/v1/catalogs/{catalog_id}/sync")
        assert response.status_code == 202

        # Second sync should fail
        response = await client_with_db.post(f"/api/v1/catalogs/{catalog_id}/sync")
        assert response.status_code == 400
        assert "already in progress" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_get_sync_status_no_jobs(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.get(f"/api/v1/catalogs/{catalog_id}/sync/status")
        assert response.status_code == 200
        assert response.json() is None

    @pytest.mark.asyncio
    async def test_get_sync_status_after_trigger(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        await client_with_db.patch(
            f"/api/v1/catalogs/{catalog_id}",
            json={"source_config": {"shared_drive_id": "drive-xyz"}},
        )
        await client_with_db.post(f"/api/v1/catalogs/{catalog_id}/sync")

        response = await client_with_db.get(f"/api/v1/catalogs/{catalog_id}/sync/status")
        assert response.status_code == 200
        data = response.json()
        assert data is not None
        assert data["status"] == "pending"
        assert data["catalog_id"] == catalog_id


class TestCatalogFilesEndpoints:
    """Test file and page listing endpoints."""

    @pytest.mark.asyncio
    async def test_list_files_empty(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.get(f"/api/v1/catalogs/{catalog_id}/files")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_pages_empty(self, client_with_db: AsyncClient):
        created = await _create_catalog_via_api(client_with_db)
        catalog_id = created["id"]

        response = await client_with_db.get(f"/api/v1/catalogs/{catalog_id}/pages")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
