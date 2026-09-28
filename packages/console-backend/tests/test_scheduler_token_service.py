"""SchedulerTokenService: the whole-response exchange the token broker needs (its clients
cache by ``expires_in``), without changing what the scheduler's string API returns, and
the bulk "who has a vaulted token" lookup group defaults use."""

from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import text

from console_backend.services import scheduler_token_service as token_module
from console_backend.services.scheduler_token_service import (
    NoOfflineTokenError,
    OfflineTokenExpiredError,
    SchedulerTokenService,
)

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


async def _vault(pg_session, user_id: str, *, expired: bool = False) -> None:
    await pg_session.execute(
        text(
            "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) "
            "VALUES (:id, :id, :email, 'T', 'U', false, 'member', 'active')"
        ),
        {"id": user_id, "email": f"{user_id}@example.com"},
    )
    await pg_session.execute(
        text(
            "INSERT INTO user_offline_tokens (user_id, encrypted_token, expired_at) "
            "VALUES (:id, :blob, CASE WHEN :expired THEN NOW() END)"
        ),
        {"id": user_id, "blob": b"\x00\x01", "expired": expired},
    )
    await pg_session.commit()


async def _stored_at(pg_session, user_id: str):
    return (
        await pg_session.execute(text("SELECT updated_at FROM user_offline_tokens WHERE user_id = :u"), {"u": user_id})
    ).scalar_one()


class _Kms:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def generate_data_key(self, **_):
        return {"Plaintext": b"k" * 32, "CiphertextBlob": b"wrapped-dek"}


class TestADeadTokenCountsAsAbsent:
    """A token Keycloak refused used to stay in the vault reading as "has consent", so the
    console called its owner ready while every run under it failed."""

    @pytest.mark.asyncio
    async def test_an_expired_token_is_no_consent(self, service, pg_session):
        await _vault(pg_session, "live")
        await _vault(pg_session, "dead", expired=True)

        assert await service.users_with_consent(pg_session, ["live", "dead"]) == {"live"}
        assert await service.has_consent(pg_session, "live")
        assert not await service.has_consent(pg_session, "dead")

    @pytest.mark.asyncio
    async def test_mark_expired_takes_consent_away_and_a_new_token_gives_it_back(
        self, service, pg_session, monkeypatch
    ):
        await _vault(pg_session, "u1")

        await service.mark_expired(pg_session, "u1", await _stored_at(pg_session, "u1"))
        assert not await service.has_consent(pg_session, "u1")

        monkeypatch.setattr(token_module, "_get_kms_client", lambda: _Kms())
        await service.store_offline_token(pg_session, "u1", "fresh-refresh-token")

        assert await service.has_consent(pg_session, "u1")

    @pytest.mark.asyncio
    async def test_a_token_stored_after_the_refusal_is_not_marked(self, service, pg_session, monkeypatch):
        """The user signs in between the refused refresh and the mark: the fresh token
        must stay live, or they would have to sign in a second time."""
        await _vault(pg_session, "u1")
        refused = await _stored_at(pg_session, "u1")
        monkeypatch.setattr(token_module, "_get_kms_client", lambda: _Kms())
        await service.store_offline_token(pg_session, "u1", "fresh-refresh-token")

        await service.mark_expired(pg_session, "u1", refused)

        assert await service.has_consent(pg_session, "u1")


class TestRefreshTellsADeadTokenFromAnOutage:
    @pytest.fixture
    def vaulted(self, service, pg_session, monkeypatch):
        monkeypatch.setattr(service, "_decrypt_blob", AsyncMock(return_value="refresh-token"))
        return _vault(pg_session, "u1")

    @pytest.mark.asyncio
    async def test_invalid_grant_is_an_expired_token(self, service, pg_session, vaulted):
        await vaulted
        with respx.mock() as router:
            router.post(TOKEN_URL).mock(
                return_value=httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "Offline session not active"}
                )
            )
            with pytest.raises(OfflineTokenExpiredError) as exc_info:
                await service.get_access_token(pg_session, "u1")
        # Names the refused token, so mark_expired cannot hit a fresher one.
        assert exc_info.value.stored_at == await _stored_at(pg_session, "u1")

    @pytest.mark.asyncio
    async def test_any_other_keycloak_error_says_nothing_about_the_token(self, service, pg_session, vaulted):
        await vaulted
        with respx.mock() as router:
            router.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="unavailable"))
            with pytest.raises(httpx.HTTPStatusError):
                await service.get_access_token(pg_session, "u1")

    @pytest.mark.asyncio
    async def test_a_token_already_marked_is_not_refreshed_again(self, service, pg_session, vaulted):
        await vaulted
        await service.mark_expired(pg_session, "u1", await _stored_at(pg_session, "u1"))
        with respx.mock(assert_all_called=False) as router:
            route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "x"}))
            with pytest.raises(OfflineTokenExpiredError):
                await service.get_access_token(pg_session, "u1")
        assert not route.called

    @pytest.mark.asyncio
    async def test_no_token_at_all_is_still_the_plain_error(self, service, pg_session):
        with pytest.raises(NoOfflineTokenError) as exc_info:
            await service.get_access_token(pg_session, "nobody")
        assert not isinstance(exc_info.value, OfflineTokenExpiredError)
