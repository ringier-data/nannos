"""Tests for the GET /api/v1/config frontend configuration endpoint."""

import pytest


@pytest.mark.asyncio
async def test_frontend_config_returns_expected_shape(client):
    """GET /api/v1/config returns all required keys with correct types."""
    response = await client.get("/api/v1/config")
    assert response.status_code == 200

    data = response.json()
    assert "orchestratorUrl" in data
    assert "keycloakBaseUrl" in data
    assert "keycloakRealm" in data
    assert "langsmith" in data
    assert "organizationId" in data["langsmith"]
    assert "projectId" in data["langsmith"]
    assert "autoApprove" in data
    assert isinstance(data["autoApprove"]["maxSystemPromptLength"], int)
    assert isinstance(data["autoApprove"]["maxMcpToolsCount"], int)


@pytest.mark.asyncio
async def test_frontend_config_is_cacheable(client):
    """GET /api/v1/config sets a Cache-Control header for browser caching."""
    response = await client.get("/api/v1/config")
    assert response.status_code == 200
    assert "public" in response.headers.get("cache-control", "")
    assert "max-age=300" in response.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_frontend_config_no_auth_required(client):
    """GET /api/v1/config is accessible without authentication."""
    # The `client` fixture has no auth headers — a 200 proves the endpoint is public.
    response = await client.get("/api/v1/config")
    assert response.status_code == 200
