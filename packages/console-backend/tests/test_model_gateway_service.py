"""Tests for ModelGatewayService._request log behavior.

The model catalog probes an internal proxy endpoint that newer LiteLLM versions don't
expose (it falls back to the pinned public cost map). That expected 404 must not be logged
as an error — `optional=True` downgrades it to debug.
"""

import logging

import httpx
import pytest
from console_backend.services.model_gateway_service import ModelGatewayError, ModelGatewayService

_LOGGER = "console_backend.services.model_gateway_service"


class _FakeClient:
    """Async-context httpx client stand-in that always returns a 404."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        return httpx.Response(404, text="Not Found", request=httpx.Request(method, url))


@pytest.fixture
def svc():
    return ModelGatewayService(base_url="http://gateway.test", master_key="k")


@pytest.mark.asyncio
async def test_optional_request_404_is_debug_not_error(svc, caplog, monkeypatch):
    monkeypatch.setattr("console_backend.services.model_gateway_service.httpx.AsyncClient", _FakeClient)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        with pytest.raises(ModelGatewayError):
            await svc._request("GET", "/get/litellm_model_cost_map", optional=True)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # no error noise
    assert any(r.levelno == logging.DEBUG and "404" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_non_optional_request_404_is_error(svc, caplog, monkeypatch):
    """A 404 on a required endpoint is still surfaced as an error."""
    monkeypatch.setattr("console_backend.services.model_gateway_service.httpx.AsyncClient", _FakeClient)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        with pytest.raises(ModelGatewayError):
            await svc._request("GET", "/model/info")

    assert any(r.levelno == logging.ERROR and "404" in r.getMessage() for r in caplog.records)
