"""Integration tests for SCIM 2.0 router endpoints."""

import os

# Ensure code chooses auto credentials path during imports
os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
import pytest_asyncio
from console_backend.dependencies import require_scim_token
from sqlalchemy import text


@pytest_asyncio.fixture
async def scim_client(client_with_db):
    """HTTP client with SCIM token auth bypassed."""
    app = client_with_db._transport.app

    async def override_require_scim_token():
        return None

    app.dependency_overrides[require_scim_token] = override_require_scim_token
    yield client_with_db
    app.dependency_overrides.pop(require_scim_token, None)


@pytest_asyncio.fixture
async def seed_user(pg_session):
    """Insert a user via SCIM-style SQL for testing retrieval/update/delete."""
    user_id = "scim-test-user-001"
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status,
                               is_administrator, scim_external_id, created_at, updated_at)
            VALUES (:id, :sub, :email, :first_name, :last_name, 'member', 'active',
                    false, :ext_id, NOW(), NOW())
        """),
        {
            "id": user_id,
            "sub": user_id,
            "email": "scimuser@example.com",
            "first_name": "Scim",
            "last_name": "User",
            "ext_id": "ext-001",
        },
    )
    await pg_session.commit()
    return user_id


@pytest_asyncio.fixture
async def seed_group(pg_session):
    """Insert a group for testing."""
    result = await pg_session.execute(
        text("""
            INSERT INTO user_groups (name, description, scim_external_id, created_at, updated_at)
            VALUES ('SCIM Test Group', 'A test group', 'ext-group-001', NOW(), NOW())
            RETURNING id
        """),
    )
    await pg_session.commit()
    row = result.fetchone()
    return str(row[0])


# ─── Discovery Endpoints ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestServiceProviderConfig:
    async def test_get_service_provider_config(self, scim_client):
        response = await scim_client.get("/api/scim/v2/ServiceProviderConfig")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/scim+json"
        data = response.json()
        assert "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig" in data["schemas"]
        assert data["patch"]["supported"] is True
        assert data["bulk"]["supported"] is False

    async def test_get_resource_types(self, scim_client):
        response = await scim_client.get("/api/scim/v2/ResourceTypes")
        assert response.status_code == 200
        data = response.json()
        assert "urn:ietf:params:scim:api:messages:2.0:ListResponse" in data["schemas"]
        assert data["totalResults"] == 2
        resources = data["Resources"]
        names = {rt["name"] for rt in resources}
        assert names == {"User", "Group"}
        # Each resource type should have meta
        for rt in resources:
            assert "meta" in rt
            assert rt["meta"]["resourceType"] == "ResourceType"

    async def test_get_schemas(self, scim_client):
        response = await scim_client.get("/api/scim/v2/Schemas")
        assert response.status_code == 200
        data = response.json()
        assert "urn:ietf:params:scim:api:messages:2.0:ListResponse" in data["schemas"]
        assert data["totalResults"] == 2
        resources = data["Resources"]
        ids = {s["id"] for s in resources}
        assert "urn:ietf:params:scim:schemas:core:2.0:User" in ids
        assert "urn:ietf:params:scim:schemas:core:2.0:Group" in ids

    async def test_get_schema_by_id(self, scim_client):
        response = await scim_client.get(
            "/api/scim/v2/Schemas/urn:ietf:params:scim:schemas:core:2.0:User"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "User"
        assert len(data["attributes"]) > 0

    async def test_get_schema_not_found(self, scim_client):
        response = await scim_client.get("/api/scim/v2/Schemas/urn:nonexistent:schema")
        assert response.status_code == 404


# ─── User CRUD Endpoints ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestScimUsers:
    async def test_create_user(self, scim_client, pg_session):
        response = await scim_client.post(
            "/api/scim/v2/Users",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "newuser@example.com",
                "name": {"givenName": "New", "familyName": "User"},
                "externalId": "ext-new-001",
                "active": True,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["userName"] == "newuser@example.com"
        assert data["name"]["givenName"] == "New"
        assert data["name"]["familyName"] == "User"
        assert data["externalId"] == "ext-new-001"
        assert data["active"] is True
        assert data["id"] is not None
        assert data["meta"]["resourceType"] == "User"

        # Verify in DB
        row = await pg_session.execute(
            text("SELECT email, scim_external_id FROM users WHERE id = :id"),
            {"id": data["id"]},
        )
        db_row = row.mappings().first()
        assert db_row["email"] == "newuser@example.com"
        assert db_row["scim_external_id"] == "ext-new-001"

    async def test_create_user_duplicate_email(self, scim_client, seed_user):
        response = await scim_client.post(
            "/api/scim/v2/Users",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "scimuser@example.com",
                "name": {"givenName": "Dup", "familyName": "User"},
            },
        )
        assert response.status_code == 409

    async def test_get_user(self, scim_client, seed_user):
        response = await scim_client.get(f"/api/scim/v2/Users/{seed_user}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == seed_user
        assert data["userName"] == "scimuser@example.com"
        assert data["externalId"] == "ext-001"

    async def test_get_user_not_found(self, scim_client):
        response = await scim_client.get("/api/scim/v2/Users/nonexistent-id")
        assert response.status_code == 404

    async def test_list_users(self, scim_client, seed_user):
        response = await scim_client.get("/api/scim/v2/Users")
        assert response.status_code == 200
        data = response.json()
        assert "urn:ietf:params:scim:api:messages:2.0:ListResponse" in data["schemas"]
        assert data["totalResults"] >= 1
        assert len(data["Resources"]) >= 1

    async def test_list_users_filter_by_username(self, scim_client, seed_user):
        response = await scim_client.get(
            '/api/scim/v2/Users?filter=userName eq "scimuser@example.com"'
        )
        assert response.status_code == 200
        data = response.json()
        assert data["totalResults"] == 1
        assert data["Resources"][0]["userName"] == "scimuser@example.com"

    async def test_list_users_filter_no_match(self, scim_client, seed_user):
        response = await scim_client.get(
            '/api/scim/v2/Users?filter=userName eq "nobody@example.com"'
        )
        assert response.status_code == 200
        data = response.json()
        assert data["totalResults"] == 0

    async def test_replace_user(self, scim_client, seed_user):
        response = await scim_client.put(
            f"/api/scim/v2/Users/{seed_user}",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "updated@example.com",
                "name": {"givenName": "Updated", "familyName": "Name"},
                "externalId": "ext-001",
                "active": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["userName"] == "updated@example.com"
        assert data["name"]["givenName"] == "Updated"

    async def test_patch_user_deactivate(self, scim_client, seed_user):
        response = await scim_client.patch(
            f"/api/scim/v2/Users/{seed_user}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "active", "value": False}
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is False

    async def test_patch_user_username(self, scim_client, seed_user):
        """PATCH userName should update the SCIM userName, not the email."""
        response = await scim_client.patch(
            f"/api/scim/v2/Users/{seed_user}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {"op": "replace", "path": "userName", "value": "JohnDoe"}
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["userName"] == "JohnDoe"
        # email should remain unchanged
        assert data["emails"][0]["value"] == "scimuser@example.com"

    async def test_delete_user(self, scim_client, seed_user, pg_session):
        response = await scim_client.delete(f"/api/scim/v2/Users/{seed_user}")
        assert response.status_code == 204

        # Verify soft delete in DB
        row = await pg_session.execute(
            text("SELECT deleted_at, status FROM users WHERE id = :id"),
            {"id": seed_user},
        )
        db_row = row.mappings().first()
        assert db_row["status"] == "deleted" or db_row["deleted_at"] is not None

    async def test_delete_user_not_found(self, scim_client):
        response = await scim_client.delete("/api/scim/v2/Users/nonexistent-id")
        assert response.status_code == 404

    async def test_list_users_sort_by_username(self, scim_client, pg_session):
        """sortBy=userName should sort users by email."""
        # Create two users with distinct emails
        for email in ["zulu@example.com", "alpha@example.com"]:
            await pg_session.execute(
                text("""
                    INSERT INTO users (id, sub, email, first_name, last_name, role, status,
                                       is_administrator, created_at, updated_at)
                    VALUES (gen_random_uuid()::text, gen_random_uuid()::text, :email, '', '', 'member', 'active',
                            false, NOW(), NOW())
                """),
                {"email": email},
            )
        await pg_session.commit()

        response = await scim_client.get("/api/scim/v2/Users?sortBy=userName&sortOrder=ascending")
        assert response.status_code == 200
        data = response.json()
        emails = [r["userName"] for r in data["Resources"]]
        assert emails == sorted(emails)

    async def test_create_user_with_emails_array(self, scim_client):
        """POST User with emails array should use emails[].value as email, preserve userName."""
        response = await scim_client.post(
            "/api/scim/v2/Users",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": "bjensen",
                "name": {"givenName": "Barbara", "familyName": "Jensen"},
                "emails": [
                    {"value": "barbara.jensen@example.com", "type": "work", "primary": True}
                ],
                "active": True,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["userName"] == "bjensen"
        assert data["emails"][0]["value"] == "barbara.jensen@example.com"


# ─── Group CRUD Endpoints ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestScimGroups:
    async def test_create_group(self, scim_client, pg_session):
        response = await scim_client.post(
            "/api/scim/v2/Groups",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "Engineering",
                "externalId": "ext-eng-001",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["displayName"] == "Engineering"
        assert data["externalId"] == "ext-eng-001"
        assert data["id"] is not None

        # Verify in DB
        row = await pg_session.execute(
            text("SELECT name, scim_external_id FROM user_groups WHERE id = :id"),
            {"id": int(data["id"])},
        )
        db_row = row.mappings().first()
        assert db_row["name"] == "Engineering"
        assert db_row["scim_external_id"] == "ext-eng-001"

    async def test_create_group_duplicate_name(self, scim_client, seed_group):
        response = await scim_client.post(
            "/api/scim/v2/Groups",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "SCIM Test Group",
            },
        )
        assert response.status_code == 409

    async def test_get_group(self, scim_client, seed_group):
        response = await scim_client.get(f"/api/scim/v2/Groups/{seed_group}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == seed_group
        assert data["displayName"] == "SCIM Test Group"

    async def test_get_group_not_found(self, scim_client):
        response = await scim_client.get("/api/scim/v2/Groups/99999")
        assert response.status_code == 404

    async def test_get_group_invalid_id(self, scim_client):
        """Out-of-range or non-numeric group IDs should return 404, not 500."""
        response = await scim_client.get("/api/scim/v2/Groups/9876543210123456")
        assert response.status_code == 404

        response = await scim_client.get("/api/scim/v2/Groups/not-a-number")
        assert response.status_code == 404

    async def test_list_groups(self, scim_client, seed_group):
        response = await scim_client.get("/api/scim/v2/Groups")
        assert response.status_code == 200
        data = response.json()
        assert data["totalResults"] >= 1

    async def test_list_groups_filter_by_name(self, scim_client, seed_group):
        response = await scim_client.get(
            '/api/scim/v2/Groups?filter=displayName eq "SCIM Test Group"'
        )
        assert response.status_code == 200
        data = response.json()
        assert data["totalResults"] == 1
        assert data["Resources"][0]["displayName"] == "SCIM Test Group"

    async def test_replace_group(self, scim_client, seed_group):
        response = await scim_client.put(
            f"/api/scim/v2/Groups/{seed_group}",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": "Renamed Group",
                "externalId": "ext-group-001",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["displayName"] == "Renamed Group"

    async def test_patch_group_add_member(self, scim_client, seed_group, seed_user):
        response = await scim_client.patch(
            f"/api/scim/v2/Groups/{seed_group}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"value": seed_user}],
                    }
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        member_ids = [m["value"] for m in data.get("members", [])]
        assert seed_user in member_ids

    async def test_patch_group_remove_member(self, scim_client, seed_group, seed_user, pg_session):
        # First add the member
        await pg_session.execute(
            text("""
                INSERT INTO user_group_members (user_group_id, user_id, group_role)
                VALUES (:group_id, :user_id, 'write')
            """),
            {"group_id": int(seed_group), "user_id": seed_user},
        )
        await pg_session.commit()

        response = await scim_client.patch(
            f"/api/scim/v2/Groups/{seed_group}",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": "members",
                        "value": [{"value": seed_user}],
                    }
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        member_ids = [m["value"] for m in data.get("members", [])]
        assert seed_user not in member_ids

    async def test_delete_group(self, scim_client, seed_group, pg_session):
        response = await scim_client.delete(f"/api/scim/v2/Groups/{seed_group}")
        assert response.status_code == 204

        # Verify deleted
        row = await pg_session.execute(
            text("SELECT deleted_at FROM user_groups WHERE id = :id"),
            {"id": int(seed_group)},
        )
        db_row = row.mappings().first()
        assert db_row["deleted_at"] is not None

    async def test_delete_group_not_found(self, scim_client):
        response = await scim_client.delete("/api/scim/v2/Groups/99999")
        assert response.status_code == 404


# ─── Auth Enforcement ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestScimAuth:
    async def test_no_token_returns_401(self, client_with_db):
        """Without the SCIM token override, requests should fail with 401."""
        # Remove override if present
        client_with_db._transport.app.dependency_overrides.pop(require_scim_token, None)

        response = await client_with_db.get("/api/scim/v2/Users")
        assert response.status_code == 401

    async def test_invalid_token_returns_401(self, client_with_db):
        """Invalid bearer token should fail."""
        client_with_db._transport.app.dependency_overrides.pop(require_scim_token, None)

        response = await client_with_db.get(
            "/api/scim/v2/Users",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401
