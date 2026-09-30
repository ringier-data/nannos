"""The model Test endpoint streams the probe's progress as NDJSON (nannos#318).

The verdict arrives last, in-band: the HTTP status is sent before it exists, so a refused
model is an ``error`` event on a 200, not a 502.
"""

import json

import httpx
import pytest
from fastapi import FastAPI

from console_backend.db.session import get_db_session
from console_backend.dependencies import require_admin
from console_backend.routers import admin_model_gateway_router as r
from console_backend.services.model_gateway_service import ModelGatewayError


class _Gateway:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def test_model(self, model_name, model_id=None, on_progress=None):
        on_progress({"type": "plan", "shapes": [{"shape": "tools_auto", "label": "Tools, model decides"}]})
        on_progress({"type": "step", "shape": "tools_auto", "step": "tools_auto", "label": "Tools, model decides"})
        if self.error:
            raise self.error
        on_progress({"type": "result", "shape": "tools_auto", "ok": True})
        return self.result


class _Defaults:
    def __init__(self, served):
        self.served = served

    async def utility_tiers_served_by(self, db, alias):
        return self.served


def _client(gateway, served=()):
    app = FastAPI()
    app.include_router(r.router)
    app.state.model_gateway_service = gateway
    app.state.model_defaults_service = _Defaults(list(served))
    app.dependency_overrides[require_admin] = lambda: object()
    app.dependency_overrides[get_db_session] = lambda: None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _events(client):
    resp = await client.post("/api/v1/admin/model-gateway/models/m/test")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in resp.text.splitlines() if line]


@pytest.mark.asyncio
async def test_progress_then_the_verdict_last():
    result = {"probe": {"capabilities": {"response_format": True}, "results": []}, "recorded": True}
    async with _client(_Gateway(result=result)) as client:
        events = await _events(client)
    assert [e["type"] for e in events] == ["plan", "step", "result", "done"]
    assert events[-1] == {"type": "done", "status": "ok", "model_name": "m", **result, "warning": None}


@pytest.mark.asyncio
async def test_a_refused_model_is_an_error_event():
    async with _client(_Gateway(error=ModelGatewayError("the model rejects request shapes"))) as client:
        events = await _events(client)
    assert events[-1]["type"] == "error"
    assert events[-1]["status_code"] == 502 and "rejects request shapes" in events[-1]["detail"]


@pytest.mark.asyncio
async def test_a_sitting_utility_default_that_now_rejects_response_format_is_warned_about():
    result = {"probe": {"capabilities": {"response_format": False}}, "recorded": True}
    async with _client(_Gateway(result=result), served=["chat (default)"]) as client:
        events = await _events(client)
    assert "chat (default)" in events[-1]["warning"]
