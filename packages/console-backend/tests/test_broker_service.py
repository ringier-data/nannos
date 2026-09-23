"""Token broker service against a real database.

The login lifecycle (pending → code issued → redeemed, each step once), the link that
confines a client to the users who signed in through it, and the answers minting gives
when a user cannot be served. Keycloak and KMS are stubbed at the token service.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from console_backend.models.broker import BrokerClientCreate, BrokerClientUpdate
from console_backend.repositories.broker_client_repository import BrokerClientRepository
from console_backend.repositories.broker_login_request_repository import BrokerLoginRequestRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.scheduler_token_service import NoOfflineTokenError
from console_backend.services.broker_service import BrokerRefusal, BrokerService

SLACK_CALLBACK = "https://slack.nannos.ringier.ch/api/v1/oauth/callback"
USERINFO = {
    "sub": "test-user-sub",
    "email": "test@example.com",
    "email_verified": True,
    "name": "Test User",
    "preferred_username": "test",
    "given_name": "Test",
    "family_name": "User",
    "groups": ["nannos-admins"],
    "phone_number": "+41790000000",
    "company_name": "Test Company",
}


def _keycloak_error(status_code: int, error: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://login.example/realms/nannos/protocol/openid-connect/token")
    response = httpx.Response(status_code, json={"error": error}, request=request)
    return httpx.HTTPStatusError(error, request=request, response=response)


@pytest_asyncio.fixture
async def client_repo(pg_session, test_admin_user_db) -> BrokerClientRepository:
    repo = BrokerClientRepository()
    repo.set_audit_service(AuditService())
    await repo.create_client(
        pg_session,
        test_admin_user_db,
        BrokerClientCreate(
            client_id="slack-client",
            name="Slack",
            redirect_uris=[SLACK_CALLBACK],
            audiences=["orchestrator", "agent-console"],
        ),
    )
    await repo.create_client(
        pg_session,
        test_admin_user_db,
        BrokerClientCreate(
            client_id="cockpit-embed",
            name="Cockpit",
            redirect_uris=["https://pr-*-riad.d.alloy.ch/nannos-auth-callback.html"],
            audiences=["cockpit-embed"],
        ),
    )
    await pg_session.commit()
    return repo


@pytest.fixture
def tokens() -> MagicMock:
    service = MagicMock()
    service.get_exchanged_token_response = AsyncMock(return_value={"access_token": "minted", "expires_in": 7200})
    return service


@pytest.fixture
def broker(client_repo, user_service, tokens) -> BrokerService:
    return BrokerService(
        client_repo=client_repo,
        login_request_repo=BrokerLoginRequestRepository(),
        user_service=user_service,
        scheduler_token_service=tokens,
    )


async def _signed_in(broker: BrokerService, pg_session, user, client_id="slack-client", redirect_uri=SLACK_CALLBACK):
    """Run a login up to the issued code, as the callback would."""
    state = await broker.begin_login(pg_session, client_id=client_id, redirect_uri=redirect_uri, client_state="cs-1")
    login = await broker.open_login(pg_session, state)
    assert login is not None
    code = await broker.issue_code(pg_session, login, user, BrokerService.identity_from_userinfo(USERINFO, user.id))
    await pg_session.commit()
    return state, code


class TestBeginLogin:
    @pytest.mark.asyncio
    async def test_records_a_pending_login_keyed_by_a_hash_of_the_state(self, broker, pg_session):
        state = await broker.begin_login(
            pg_session, client_id="slack-client", redirect_uri=SLACK_CALLBACK, client_state="their-state"
        )
        rows = (await pg_session.execute(text("SELECT * FROM broker_login_requests"))).mappings().all()
        assert len(rows) == 1
        assert rows[0]["state_hash"] != state  # never the raw value
        login = await broker.open_login(pg_session, state)
        assert login is not None
        assert (login.client_id, login.redirect_uri, login.client_state) == (
            "slack-client",
            SLACK_CALLBACK,
            "their-state",
        )

    @pytest.mark.asyncio
    async def test_a_wildcard_registration_accepts_a_concrete_preview_origin(self, broker, pg_session):
        uri = "https://pr-42-riad.d.alloy.ch/nannos-auth-callback.html"
        state = await broker.begin_login(pg_session, client_id="cockpit-embed", redirect_uri=uri, client_state=None)
        assert (await broker.open_login(pg_session, state)).redirect_uri == uri

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("client_id", "redirect_uri", "status"),
        [
            ("unknown-client", SLACK_CALLBACK, 404),
            ("slack-client", SLACK_CALLBACK + "/", 400),  # exact match only
            ("slack-client", "https://evil.example/api/v1/oauth/callback", 400),
            ("cockpit-embed", "https://pr-*-riad.d.alloy.ch/nannos-auth-callback.html", 400),  # no pattern requests
        ],
    )
    async def test_refuses_what_the_registration_does_not_allow(
        self, broker, pg_session, client_id, redirect_uri, status
    ):
        with pytest.raises(BrokerRefusal) as exc:
            await broker.begin_login(pg_session, client_id=client_id, redirect_uri=redirect_uri, client_state=None)
        assert exc.value.status_code == status

    @pytest.mark.asyncio
    async def test_a_disabled_client_cannot_start_a_login(self, broker, client_repo, pg_session, test_admin_user_db):
        slack = await client_repo.get_by_client_id(pg_session, "slack-client")
        await client_repo.update_client(pg_session, test_admin_user_db, slack.id, BrokerClientUpdate(enabled=False))
        broker.invalidate_cache()
        with pytest.raises(BrokerRefusal) as exc:
            await broker.begin_login(pg_session, client_id="slack-client", redirect_uri=SLACK_CALLBACK, client_state=None)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_an_expired_login_is_not_open(self, broker, pg_session):
        state = await broker.begin_login(pg_session, client_id="slack-client", redirect_uri=SLACK_CALLBACK, client_state=None)
        await pg_session.execute(
            text("UPDATE broker_login_requests SET expires_at = :past"),
            {"past": datetime.now(timezone.utc) - timedelta(seconds=1)},
        )
        assert await broker.open_login(pg_session, state) is None
        assert await broker.open_login(pg_session, None) is None
        assert await broker.open_login(pg_session, "never-issued") is None


class TestCodeAndRedeem:
    @pytest.mark.asyncio
    async def test_redeem_returns_the_identity_once_and_links_the_user(self, broker, pg_session, test_user_db):
        _, code = await _signed_in(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")

        identity = await broker.redeem(pg_session, slack, code)
        assert identity.user_id == test_user_db.id
        assert identity.sub == "test-user-sub"
        assert identity.groups == ["nannos-admins"]
        assert identity.email_verified is True
        assert identity.phone_number == "+41790000000"
        linked = await pg_session.execute(
            text("SELECT 1 FROM broker_client_users WHERE client_id = 'slack-client' AND user_id = :u"),
            {"u": test_user_db.id},
        )
        assert linked.first() is not None

        with pytest.raises(BrokerRefusal) as exc:
            await broker.redeem(pg_session, slack, code)
        assert (exc.value.status_code, exc.value.detail) == (400, "invalid_grant")

    @pytest.mark.asyncio
    async def test_only_the_client_the_code_was_issued_to_can_redeem_it(self, broker, pg_session, test_user_db):
        _, code = await _signed_in(broker, pg_session, test_user_db)
        cockpit = await broker.resolve_client(pg_session, "cockpit-embed")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.redeem(pg_session, cockpit, code)
        assert exc.value.status_code == 400
        # ...and the refusal did not burn the code for its rightful owner.
        slack = await broker.resolve_client(pg_session, "slack-client")
        assert (await broker.redeem(pg_session, slack, code)).sub == "test-user-sub"

    @pytest.mark.asyncio
    async def test_an_expired_code_cannot_be_redeemed(self, broker, pg_session, test_user_db):
        _, code = await _signed_in(broker, pg_session, test_user_db)
        await pg_session.execute(
            text("UPDATE broker_login_requests SET code_expires_at = :past"),
            {"past": datetime.now(timezone.utc) - timedelta(seconds=1)},
        )
        slack = await broker.resolve_client(pg_session, "slack-client")
        with pytest.raises(BrokerRefusal):
            await broker.redeem(pg_session, slack, code)

    @pytest.mark.asyncio
    async def test_a_completed_login_cannot_issue_a_second_code(self, broker, pg_session, test_user_db):
        state, _ = await _signed_in(broker, pg_session, test_user_db)
        # The callback replayed with the same state finds no open login.
        assert await broker.open_login(pg_session, state) is None


class TestMint:
    async def _linked(self, broker, pg_session, user) -> None:
        _, code = await _signed_in(broker, pg_session, user)
        await broker.redeem(pg_session, await broker.resolve_client(pg_session, "slack-client"), code)

    @pytest.mark.asyncio
    async def test_mints_an_allowed_audience_for_a_linked_user(self, broker, tokens, pg_session, test_user_db):
        await self._linked(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")

        minted = await broker.mint(pg_session, slack, "test-user-sub", "orchestrator")

        assert (minted.access_token, minted.expires_in, minted.token_type) == ("minted", 7200, "Bearer")
        tokens.get_exchanged_token_response.assert_awaited_once_with(pg_session, test_user_db.id, "orchestrator")

    @pytest.mark.asyncio
    async def test_an_audience_the_client_is_not_allowed_is_refused(self, broker, pg_session, test_user_db):
        await self._linked(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "test-user-sub", "cockpit-embed")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_client_reaches_only_users_who_signed_in_through_it(
        self, broker, tokens, pg_session, test_user_db
    ):
        await self._linked(broker, pg_session, test_user_db)  # through Slack
        cockpit = await broker.resolve_client(pg_session, "cockpit-embed")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, cockpit, "test-user-sub", "cockpit-embed")
        assert exc.value.status_code == 409
        # An unknown subject gets the same answer, so the check leaks nothing.
        slack = await broker.resolve_client(pg_session, "slack-client")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "nobody", "orchestrator")
        assert exc.value.status_code == 409
        tokens.get_exchanged_token_response.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (NoOfflineTokenError("No offline token stored"), 409),  # never vaulted
            (ValueError("Expecting value"), 502),  # e.g. a non-JSON Keycloak reply: our fault, not a sign-in cue
            (_keycloak_error(400, "invalid_grant"), 409),  # vaulted token is dead
            (_keycloak_error(403, "access_denied"), 502),  # exchange not permitted: config
            (httpx.ConnectError("down"), 502),
            (RuntimeError("kms"), 502),
        ],
    )
    async def test_mint_failures(self, broker, tokens, pg_session, test_user_db, error, status):
        await self._linked(broker, pg_session, test_user_db)
        tokens.get_exchanged_token_response.side_effect = error
        slack = await broker.resolve_client(pg_session, "slack-client")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "test-user-sub", "orchestrator")
        assert exc.value.status_code == status


class TestClientCache:
    @pytest.mark.asyncio
    async def test_an_admin_write_takes_effect_once_the_cache_is_cleared(
        self, broker, client_repo, pg_session, test_admin_user_db
    ):
        slack = await broker.resolve_client(pg_session, "slack-client")
        assert slack.enabled
        await client_repo.update_client(pg_session, test_admin_user_db, slack.id, BrokerClientUpdate(enabled=False))
        assert (await broker.resolve_client(pg_session, "slack-client")).enabled  # cached
        broker.invalidate_cache()
        assert not (await broker.resolve_client(pg_session, "slack-client")).enabled

    @pytest.mark.asyncio
    async def test_unknown_ids_are_not_cached(self, broker, pg_session):
        # /authorize takes client ids unauthenticated: caching misses would let random
        # ids grow the cache without bound.
        for n in range(3):
            assert await broker.resolve_client(pg_session, f"no-such-client-{n}") is None
        assert set(broker._client_cache) == set()
        await broker.resolve_client(pg_session, "slack-client")
        assert set(broker._client_cache) == {"slack-client"}


class TestClientRepository:
    @pytest.mark.asyncio
    async def test_writes_are_audited_and_duplicates_refused(self, client_repo, pg_session, test_admin_user_db):
        audit = await pg_session.execute(
            text("SELECT count(*) FROM audit_logs WHERE entity_type = 'broker_client' AND action = 'create'")
        )
        assert audit.scalar() == 2
        with pytest.raises(ValueError, match="already registered"):
            await client_repo.create_client(
                pg_session,
                test_admin_user_db,
                BrokerClientCreate(client_id="slack-client", name="Again", redirect_uris=[SLACK_CALLBACK], audiences=["x"]),
            )
        # The savepoint kept the transaction usable.
        assert len(await client_repo.list_all(pg_session)) == 2

    @pytest.mark.asyncio
    async def test_deleting_a_client_takes_its_links_and_pending_logins(
        self, broker, client_repo, pg_session, test_admin_user_db, test_user_db
    ):
        _, code = await _signed_in(broker, pg_session, test_user_db)
        await broker.redeem(pg_session, await broker.resolve_client(pg_session, "slack-client"), code)
        await broker.begin_login(pg_session, client_id="slack-client", redirect_uri=SLACK_CALLBACK, client_state=None)
        slack = await client_repo.get_by_client_id(pg_session, "slack-client")

        assert await client_repo.delete_client(pg_session, test_admin_user_db, slack.id)

        for table in ("broker_client_users", "broker_login_requests"):
            left = await pg_session.execute(text(f"SELECT count(*) FROM {table} WHERE client_id = 'slack-client'"))
            assert left.scalar() == 0
