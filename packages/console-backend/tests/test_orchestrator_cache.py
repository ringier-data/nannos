"""Tests for best-effort orchestrator discovery-cache invalidation."""

import httpx
import pytest

from console_backend.config import config
from console_backend.services import orchestrator_cache as oc


class _FakeResponse:
    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        pass


class _FakeClient:
    """Records the single POST the helper makes; returns a 200-ish response."""

    last_url: str | None = None
    last_headers: dict | None = None
    raise_on_post: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None):
        _FakeClient.last_url = url
        _FakeClient.last_headers = headers
        if _FakeClient.raise_on_post is not None:
            raise _FakeClient.raise_on_post
        return _FakeResponse()


class _FakeOAuthClient:
    """Stand-in for OidcOAuth2Client: records the client-credentials audience requested."""

    requested_audience: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def get_token(self, audience):
        _FakeOAuthClient.requested_audience = audience
        return f"svc-token-for::{audience}"


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeClient.last_url = None
    _FakeClient.last_headers = None
    _FakeClient.raise_on_post = None
    _FakeOAuthClient.requested_audience = None
    yield


@pytest.fixture
def _patch(monkeypatch):
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(oc, "OidcOAuth2Client", _FakeOAuthClient)
    # Happy-path config: orchestrator reachable and its OIDC client id known.
    monkeypatch.setattr(config.orchestrator, "base_domain", "localhost:10001", raising=False)
    monkeypatch.setattr(config.orchestrator, "client_id", "orchestrator", raising=False)


@pytest.mark.asyncio
async def test_skips_when_orchestrator_not_configured(monkeypatch, _patch):
    monkeypatch.setattr(config.orchestrator, "base_domain", "", raising=False)
    await oc.invalidate_orchestrator_discovery_cache("test")
    assert _FakeClient.last_url is None  # no call attempted


@pytest.mark.asyncio
async def test_skips_when_client_id_not_configured(monkeypatch, _patch):
    monkeypatch.setattr(config.orchestrator, "client_id", "", raising=False)
    await oc.invalidate_orchestrator_discovery_cache("test")
    assert _FakeClient.last_url is None  # no audience to mint a token for


@pytest.mark.asyncio
async def test_posts_with_client_credentials_bearer(_patch):
    await oc.invalidate_orchestrator_discovery_cache("test")
    assert _FakeClient.last_url == "http://localhost:10001/internal/discovery-cache/invalidate"
    # A service token was minted for the orchestrator audience and sent as a Bearer.
    assert _FakeOAuthClient.requested_audience == "orchestrator"
    assert _FakeClient.last_headers["Authorization"] == "Bearer svc-token-for::orchestrator"
    assert "X-Internal-Auth" not in _FakeClient.last_headers


@pytest.mark.asyncio
async def test_swallows_errors(_patch):
    _FakeClient.raise_on_post = httpx.ConnectError("orchestrator down")
    # Must not raise — invalidation is best-effort and must never block the triggering action.
    await oc.invalidate_orchestrator_discovery_cache("test")
