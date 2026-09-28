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

from console_backend.config import config
from console_backend.models.broker import BrokerClientCreate, BrokerClientUpdate
from console_backend.repositories.broker_client_repository import BrokerClientRepository
from console_backend.repositories.broker_login_request_repository import BrokerLoginRequestRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.scheduler_token_service import NoOfflineTokenError, OfflineTokenExpiredError
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
        ),
    )
    await repo.create_client(
        pg_session,
        test_admin_user_db,
        BrokerClientCreate(
            client_id="cockpit-embed",
            name="Cockpit",
            redirect_uris=["https://pr-*-riad.d.alloy.ch/nannos-auth-callback.html"],
        ),
    )
    await pg_session.commit()
    return repo


@pytest.fixture
def tokens() -> MagicMock:
    service = MagicMock()
    service.get_exchanged_token_response = AsyncMock(return_value={"access_token": "minted", "expires_in": 7200})
    service.mark_expired = AsyncMock()
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
    async def _linked(self, broker, pg_session, user) -> str:
        """Sign *user* in through Slack; the binding secret the client would keep."""
        _, code = await _signed_in(broker, pg_session, user)
        redemption = await broker.redeem(pg_session, await broker.resolve_client(pg_session, "slack-client"), code)
        return redemption.binding_secret

    @pytest.mark.asyncio
    async def test_mints_an_allowed_audience_for_a_linked_user(self, broker, tokens, pg_session, test_user_db):
        secret = await self._linked(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")

        minted = await broker.mint(pg_session, slack, "test-user-sub", "orchestrator", secret)

        assert (minted.access_token, minted.expires_in, minted.token_type) == ("minted", 7200, "Bearer")
        tokens.get_exchanged_token_response.assert_awaited_once_with(pg_session, test_user_db.id, "orchestrator")

    @pytest.mark.asyncio
    async def test_an_audience_the_client_is_not_allowed_is_refused(self, broker, pg_session, test_user_db):
        secret = await self._linked(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "test-user-sub", "cockpit-embed", secret)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_client_may_mint_the_always_granted_audiences_and_its_own_id(
        self, broker, tokens, pg_session, test_user_db, monkeypatch
    ):
        monkeypatch.setattr(config.broker, "always_granted_audiences", ["orchestrator", "agent-console"])
        _, code = await _signed_in(
            broker, pg_session, test_user_db, "cockpit-embed", "https://pr-7-riad.d.alloy.ch/nannos-auth-callback.html"
        )
        cockpit = await broker.resolve_client(pg_session, "cockpit-embed")
        secret = (await broker.redeem(pg_session, cockpit, code)).binding_secret

        for audience in ("orchestrator", "agent-console", "cockpit-embed"):
            minted = await broker.mint(pg_session, cockpit, "test-user-sub", audience, secret)
            assert minted.access_token == "minted"
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, cockpit, "test-user-sub", "gatana", secret)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_client_reaches_only_users_who_signed_in_through_it(
        self, broker, tokens, pg_session, test_user_db
    ):
        secret = await self._linked(broker, pg_session, test_user_db)  # through Slack
        cockpit = await broker.resolve_client(pg_session, "cockpit-embed")
        with pytest.raises(BrokerRefusal) as exc:
            # Slack's secret for this user names no binding of the cockpit.
            await broker.mint(pg_session, cockpit, "test-user-sub", "cockpit-embed", secret)
        assert exc.value.status_code == 409
        # An unknown subject gets the same answer, so the check leaks nothing.
        slack = await broker.resolve_client(pg_session, "slack-client")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "nobody", "orchestrator", secret)
        assert exc.value.status_code == 409
        tokens.get_exchanged_token_response.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (NoOfflineTokenError("No offline token stored"), 409),  # never vaulted
            (OfflineTokenExpiredError("refused (invalid_grant)", datetime.now(timezone.utc)), 409),  # vaulted token is dead
            (ValueError("Expecting value"), 502),  # e.g. a non-JSON Keycloak reply: our fault, not a sign-in cue
            (_keycloak_error(400, "invalid_grant"), 409),  # refused at the exchange step
            (_keycloak_error(403, "access_denied"), 502),  # exchange not permitted: config
            (httpx.ConnectError("down"), 502),
            (RuntimeError("kms"), 502),
        ],
    )
    async def test_mint_failures(self, broker, tokens, pg_session, test_user_db, error, status):
        secret = await self._linked(broker, pg_session, test_user_db)
        tokens.get_exchanged_token_response.side_effect = error
        slack = await broker.resolve_client(pg_session, "slack-client")
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "test-user-sub", "orchestrator", secret)
        assert exc.value.status_code == status

    @pytest.mark.asyncio
    async def test_a_dead_token_is_marked_so_the_scheduler_and_console_see_it_too(
        self, broker, tokens, pg_session, test_user_db
    ):
        secret = await self._linked(broker, pg_session, test_user_db)
        stored_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        tokens.get_exchanged_token_response.side_effect = OfflineTokenExpiredError("refused", stored_at)
        slack = await broker.resolve_client(pg_session, "slack-client")

        with pytest.raises(BrokerRefusal):
            await broker.mint(pg_session, slack, "test-user-sub", "orchestrator", secret)

        tokens.mark_expired.assert_awaited_once_with(pg_session, test_user_db.id, stored_at)


async def _binding_rows(pg_session, user_id: str) -> list[dict]:
    result = await pg_session.execute(
        text(
            "SELECT client_id, account_key, workspace_id, secret_hash FROM broker_bindings "
            "WHERE user_id = :u ORDER BY client_id, account_key"
        ),
        {"u": user_id},
    )
    return [dict(r) for r in result.mappings().all()]


async def _workspace_installations(pg_session, client_id: str, workspace_id: str) -> list[str] | None:
    result = await pg_session.execute(
        text("SELECT installation_ids FROM broker_workspaces WHERE client_id = :c AND workspace_id = :t"),
        {"c": client_id, "t": workspace_id},
    )
    return result.scalar_one_or_none()


class TestBindingSecret:
    """ADR-0011 amendment 1: a leaked client credential alone mints for no one. The secret
    /redeem returns is the second factor; the binding is keyed by the client row that
    keeps it, and its workspace's installations say where the user can be reached."""

    @pytest.mark.asyncio
    async def test_redeem_binds_the_sign_in_and_stores_only_a_hash(self, broker, pg_session, test_user_db):
        _, code = await _signed_in(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")

        redemption = await broker.redeem(pg_session, slack, code, account_key="T1:U1", workspace_id="T1")

        assert redemption.sub == "test-user-sub" and len(redemption.binding_secret) >= 32
        [row] = await _binding_rows(pg_session, test_user_db.id)
        assert (row["client_id"], row["account_key"], row["workspace_id"]) == ("slack-client", "T1:U1", "T1")
        assert redemption.binding_secret not in row["secret_hash"]
        # Where the workspace can be reached is the registration's business, not a sign-in's.
        assert await _workspace_installations(pg_session, "slack-client", "T1") is None

    @pytest.mark.asyncio
    async def test_signing_in_again_into_a_row_replaces_its_binding(self, broker, pg_session, test_user_db):
        slack = await broker.resolve_client(pg_session, "slack-client")
        _, code = await _signed_in(broker, pg_session, test_user_db)
        old = (await broker.redeem(pg_session, slack, code, account_key="T1:U1", workspace_id="T1")).binding_secret
        _, code = await _signed_in(broker, pg_session, test_user_db)
        new = (await broker.redeem(pg_session, slack, code, account_key="T1:U1", workspace_id="T1")).binding_secret

        assert [r["account_key"] for r in await _binding_rows(pg_session, test_user_db.id)] == ["T1:U1"]
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "test-user-sub", "orchestrator", old)
        assert exc.value.status_code == 409
        assert (await broker.mint(pg_session, slack, "test-user-sub", "orchestrator", new)).access_token

    @pytest.mark.asyncio
    async def test_two_rows_of_one_user_never_evict_each_other(self, broker, pg_session, test_user_db):
        """One person, two addresses (or two Slack accounts in a team): two client rows,
        two bindings, both secrets live."""
        slack = await broker.resolve_client(pg_session, "slack-client")
        secrets = []
        for key in ("ada@example.com", "ada.lovelace@example.com"):
            _, code = await _signed_in(broker, pg_session, test_user_db)
            secrets.append((await broker.redeem(pg_session, slack, code, account_key=key)).binding_secret)

        assert len(await _binding_rows(pg_session, test_user_db.id)) == 2
        for secret in secrets:
            assert (await broker.mint(pg_session, slack, "test-user-sub", "orchestrator", secret)).access_token

    @pytest.mark.asyncio
    async def test_without_an_account_key_there_is_one_binding_per_user(self, broker, pg_session, test_user_db):
        slack = await broker.resolve_client(pg_session, "slack-client")
        for _ in range(2):
            _, code = await _signed_in(broker, pg_session, test_user_db)
            await broker.redeem(pg_session, slack, code)

        [row] = await _binding_rows(pg_session, test_user_db.id)
        assert row["account_key"] == f"user:{test_user_db.id}"

    @pytest.mark.asyncio
    async def test_a_workspaces_installations_follow_the_client_not_the_sign_in(self, broker, pg_session, test_user_db):
        """An app installed after the user signed in reaches them too: installations live
        on the workspace, which only the client's registration writes, and a sign-in leaves them."""
        slack = await broker.resolve_client(pg_session, "slack-client")
        await broker.set_workspace_installations(pg_session, slack, "T1", ["A2", "A1", "A1"])
        _, code = await _signed_in(broker, pg_session, test_user_db)
        await broker.redeem(pg_session, slack, code, account_key="T1:U1", workspace_id="T1")
        assert await _workspace_installations(pg_session, "slack-client", "T1") == ["A1", "A2"]

        _, code = await _signed_in(broker, pg_session, test_user_db)
        await broker.redeem(pg_session, slack, code, account_key="T1:U1", workspace_id="T1")
        assert await _workspace_installations(pg_session, "slack-client", "T1") == ["A1", "A2"]

        await broker.set_workspace_installations(pg_session, slack, "T1", ["A2"])
        assert await _workspace_installations(pg_session, "slack-client", "T1") == ["A2"]

    @pytest.mark.asyncio
    async def test_a_client_that_requires_the_secret_refuses_without_it(self, broker, pg_session, test_user_db):
        _, code = await _signed_in(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")
        await broker.redeem(pg_session, slack, code)
        assert slack.require_binding_secret, "new registrations require it"

        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "test-user-sub", "orchestrator")
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_a_wrong_secret_is_refused_even_where_none_is_required(
        self, broker, client_repo, pg_session, test_user_db, test_admin_user_db
    ):
        slack = await broker.resolve_client(pg_session, "slack-client")
        await client_repo.update_client(
            pg_session, test_admin_user_db, slack.id, BrokerClientUpdate(require_binding_secret=False)
        )
        broker.invalidate_cache()
        slack = await broker.resolve_client(pg_session, "slack-client")
        _, code = await _signed_in(broker, pg_session, test_user_db)
        await broker.redeem(pg_session, slack, code)

        # The legacy link still serves a client that sends nothing...
        assert (await broker.mint(pg_session, slack, "test-user-sub", "orchestrator")).access_token
        # ...but a secret that is sent is always checked.
        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, "test-user-sub", "orchestrator", "not-the-secret")
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_one_users_secret_never_mints_for_another(
        self, broker, pg_session, test_user_db, test_admin_user_db
    ):
        _, code = await _signed_in(broker, pg_session, test_user_db)
        slack = await broker.resolve_client(pg_session, "slack-client")
        secret = (await broker.redeem(pg_session, slack, code)).binding_secret
        _, code = await _signed_in(broker, pg_session, test_admin_user_db)
        await broker.redeem(pg_session, slack, code)

        with pytest.raises(BrokerRefusal) as exc:
            await broker.mint(pg_session, slack, test_admin_user_db.sub, "orchestrator", secret)
        assert exc.value.status_code == 409


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
                BrokerClientCreate(client_id="slack-client", name="Again", redirect_uris=[SLACK_CALLBACK]),
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
