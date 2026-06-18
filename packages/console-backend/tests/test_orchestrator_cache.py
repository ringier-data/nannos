"""Tests for best-effort, scoped orchestrator discovery-cache invalidation."""

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
    last_json: dict | None = None
    raise_on_post: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.last_url = url
        _FakeClient.last_headers = headers
        _FakeClient.last_json = json
        if _FakeClient.raise_on_post is not None:
            raise _FakeClient.raise_on_post
        return _FakeResponse()


class _FakeOAuthClient:
    """Stand-in for the shared OidcOAuth2Client: records the audience requested."""

    requested_audience: str | None = None

    async def get_token(self, audience):
        _FakeOAuthClient.requested_audience = audience
        return f"svc-token-for::{audience}"


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeClient.last_url = None
    _FakeClient.last_headers = None
    _FakeClient.last_json = None
    _FakeClient.raise_on_post = None
    _FakeOAuthClient.requested_audience = None
    yield


@pytest.fixture
def _patch(monkeypatch):
    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    # Happy-path config: orchestrator reachable and its OIDC client id known.
    monkeypatch.setattr(config.orchestrator, "base_domain", "localhost:10001", raising=False)
    monkeypatch.setattr(config.orchestrator, "client_id", "orchestrator", raising=False)


@pytest.mark.asyncio
async def test_skips_when_orchestrator_not_configured(monkeypatch, _patch):
    monkeypatch.setattr(config.orchestrator, "base_domain", "", raising=False)
    await oc.invalidate_orchestrator_discovery_cache(_FakeOAuthClient(), "test", ["sub-1"])
    assert _FakeClient.last_url is None  # no call attempted


@pytest.mark.asyncio
async def test_skips_when_client_id_not_configured(monkeypatch, _patch):
    monkeypatch.setattr(config.orchestrator, "client_id", "", raising=False)
    await oc.invalidate_orchestrator_discovery_cache(_FakeOAuthClient(), "test", ["sub-1"])
    assert _FakeClient.last_url is None  # no audience to mint a token for


@pytest.mark.asyncio
async def test_skips_when_no_affected_users(_patch):
    # Empty list = "no affected users" → must not POST (distinct from None = flush-all).
    await oc.invalidate_orchestrator_discovery_cache(_FakeOAuthClient(), "test", [])
    assert _FakeClient.last_url is None


@pytest.mark.asyncio
async def test_posts_scoped_with_client_credentials_bearer(_patch):
    await oc.invalidate_orchestrator_discovery_cache(_FakeOAuthClient(), "test", ["sub-1", "sub-2"])
    assert _FakeClient.last_url == "http://localhost:10001/internal/discovery-cache/invalidate"
    # A service token was minted for the orchestrator audience and sent as a Bearer.
    assert _FakeOAuthClient.requested_audience == "orchestrator"
    assert _FakeClient.last_headers["Authorization"] == "Bearer svc-token-for::orchestrator"
    assert "X-Internal-Auth" not in _FakeClient.last_headers
    # The affected user subs are carried in the body so the orchestrator can scope the flush.
    assert _FakeClient.last_json == {"user_subs": ["sub-1", "sub-2"]}


@pytest.mark.asyncio
async def test_posts_empty_body_for_fleet_wide_flush(_patch):
    # user_subs=None is the explicit "flush everything" path.
    await oc.invalidate_orchestrator_discovery_cache(_FakeOAuthClient(), "test", None)
    assert _FakeClient.last_url is not None
    assert _FakeClient.last_json == {}


@pytest.mark.asyncio
async def test_swallows_errors(_patch):
    _FakeClient.raise_on_post = httpx.ConnectError("orchestrator down")
    # Must not raise — invalidation is best-effort and must never block the triggering action.
    await oc.invalidate_orchestrator_discovery_cache(_FakeOAuthClient(), "test", ["sub-1"])


class _FakeBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args):
        self.tasks.append((func, args))


class _FakeAppState:
    def __init__(self, oauth_service):
        self.oauth_service = oauth_service


class _FakeApp:
    def __init__(self, oauth_service):
        self.state = _FakeAppState(oauth_service)


class _FakeRequest:
    def __init__(self, oauth_service):
        self.app = _FakeApp(oauth_service)


def test_schedule_enqueues_task_with_shared_client():
    oauth = _FakeOAuthClient()
    bg = _FakeBackgroundTasks()
    req = _FakeRequest(oauth)
    oc.schedule_orchestrator_discovery_cache_invalidation(bg, req, "reason", ["sub-1"])
    assert len(bg.tasks) == 1
    func, args = bg.tasks[0]
    assert func is oc.invalidate_orchestrator_discovery_cache
    assert args == (oauth, "reason", ["sub-1"])


def test_schedule_skips_empty_user_subs():
    bg = _FakeBackgroundTasks()
    req = _FakeRequest(_FakeOAuthClient())
    oc.schedule_orchestrator_discovery_cache_invalidation(bg, req, "reason", [])
    assert bg.tasks == []


def test_schedule_skips_when_no_oauth_service():
    bg = _FakeBackgroundTasks()
    req = _FakeRequest(None)
    oc.schedule_orchestrator_discovery_cache_invalidation(bg, req, "reason", ["sub-1"])
    assert bg.tasks == []
