"""The tier listing's drift check compares the gateway with what the console declared for each
head — not with each tier's own stored chain, which differs on a head shared by two tiers."""

import httpx
import pytest
from fastapi import FastAPI

from console_backend.db.session import get_db_session
from console_backend.dependencies import require_admin
from console_backend.routers import admin_model_gateway_router as r


class _Defaults:
    def __init__(self, groups, declared):
        self.groups, self.declared = groups, declared

    async def get_all_tier_groups(self, db):
        return self.groups

    async def declared_chains(self, db):
        return self.declared


class _Gateway:
    def __init__(self, live):
        self.live = live

    async def get_fallbacks(self, head):
        return self.live.get(head, [])


def _client(groups, declared, live):
    app = FastAPI()
    app.include_router(r.router)
    app.state.model_defaults_service = _Defaults(groups, declared)
    app.state.model_gateway_service = _Gateway(live)
    app.dependency_overrides[require_admin] = lambda: object()
    app.dependency_overrides[get_db_session] = lambda: None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _states(client):
    resp = await client.get("/api/v1/admin/model-gateway/tiers")
    assert resp.status_code == 200
    return {t["role"]: (t["fallbacks"], t["gateway_state"]) for t in resp.json()}


@pytest.mark.asyncio
async def test_a_shared_head_with_an_empty_stored_chain_is_in_sync():
    """chat and chat:low both default to x; only chat:low has a chain. The gateway holds x → [y]
    for both — as declared — so neither tier is drifted (review round 8)."""
    async with _client({"chat": ["x"], "chat:low": ["x", "y"]}, {"x": ["y"]}, {"x": ["y"]}) as client:
        states = await _states(client)
    assert states["chat"] == ([], "in_sync")
    assert states["chat:low"] == (["y"], "in_sync")


@pytest.mark.asyncio
async def test_a_gateway_chain_that_differs_from_the_declaration_is_drifted():
    async with _client({"chat": ["x", "y"]}, {"x": ["y"]}, {"x": ["z"]}) as client:
        states = await _states(client)
    assert states["chat"] == (["y"], "drifted")
