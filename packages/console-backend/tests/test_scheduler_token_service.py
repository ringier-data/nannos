"""SchedulerTokenService: the whole-response exchange the token broker needs (its clients
cache by ``expires_in``), without changing what the scheduler's string API returns, and
the bulk "who has a vaulted token" lookup group defaults use."""

from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import text

from console_backend.services.scheduler_token_service import SchedulerTokenService

ISSUER = "https://login.test/realms/nannos"
TOKEN_URL = ISSUER + "/protocol/openid-connect/token"


@pytest.fixture
def service() -> SchedulerTokenService:
    return SchedulerTokenService(oidc_issuer=ISSUER, oidc_client_id="agent-console", oidc_client_secret="secret")


@pytest.mark.asyncio
async def test_exchange_returns_the_whole_response_and_the_string_api_is_unchanged(service):
    body = {"access_token": "aud-token", "expires_in": 300, "token_type": "Bearer"}
    with respx.mock(assert_all_called=True) as router:
        route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))

        assert await service.exchange_token_response("subject", audience="orchestrator") == body
        assert await service.exchange_token("subject", audience="orchestrator") == "aud-token"

    form = parse_qs(route.calls[0].request.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:token-exchange"]
    assert form["subject_token"] == ["subject"]
    assert form["audience"] == ["orchestrator"]
    assert form["client_id"] == ["agent-console"]


@pytest.mark.asyncio
async def test_exchange_errors_still_raise(service):
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(403, json={"error": "access_denied"}))
        with pytest.raises(httpx.HTTPStatusError):
            await service.exchange_token_response("subject", audience="cockpit-embed")


@pytest.mark.asyncio
async def test_get_exchanged_token_response_refreshes_the_vaulted_token_then_exchanges(service, monkeypatch):
    refresh = AsyncMock(return_value="fresh-access-token")
    exchange = AsyncMock(return_value={"access_token": "aud-token", "expires_in": 60})
    monkeypatch.setattr(service, "_refresh_access_token", refresh)
    monkeypatch.setattr(service, "exchange_token_response", exchange)
    db = MagicMock()

    assert await service.get_exchanged_token_response(db, "u1", "orchestrator") == {
        "access_token": "aud-token",
        "expires_in": 60,
    }
    assert await service.get_exchanged_token(db, "u1", "orchestrator") == "aud-token"
    refresh.assert_awaited_with(db, "u1")
    exchange.assert_awaited_with("fresh-access-token", audience="orchestrator")


@pytest.mark.asyncio
async def test_users_with_consent_is_one_lookup_for_a_list(service, pg_session):
    for uid in ("ready", "not-ready"):
        await pg_session.execute(
            text(
                "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) "
                "VALUES (:id, :id, :email, 'T', 'U', false, 'member', 'active')"
            ),
            {"id": uid, "email": f"{uid}@example.com"},
        )
    await pg_session.execute(
        text("INSERT INTO user_offline_tokens (user_id, encrypted_token) VALUES ('ready', :blob)"),
        {"blob": b"\x00\x01"},
    )
    await pg_session.commit()

    assert await service.users_with_consent(pg_session, ["ready", "not-ready", "unknown"]) == {"ready"}
    assert await service.users_with_consent(pg_session, []) == set()
