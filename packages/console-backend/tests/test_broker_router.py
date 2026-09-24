"""Token broker HTTP layer.

``require_broker_client`` decides who may redeem codes and mint tokens, so its refusals
are tested one by one. The browser leg (``/authorize`` → Keycloak → ``/callback``) runs
the real controller and service against the test database, with Authlib's ``broker``
registration mocked (conftest ``mock_oauth``).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from authlib.integrations.starlette_client import OAuthError
from fastapi import HTTPException
from ringier_a2a_sdk.auth import JWTValidationError
from sqlalchemy import text

import console_backend.dependencies as dependencies
import console_backend.routers.broker_router as router
from console_backend.config import config
from console_backend.controllers.broker_controller import BrokerController
from console_backend.models.broker import BrokerClient, BrokerClientCreate
from console_backend.repositories.broker_client_repository import BrokerClientRepository
from console_backend.repositories.broker_login_request_repository import BrokerLoginRequestRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.broker_service import BrokerRefusal, BrokerService

SLACK_CALLBACK = "https://slack.nannos.ringier.ch/api/v1/oauth/callback"
NOW = "2026-09-23T12:00:00+00:00"


def _client(**overrides) -> BrokerClient:
    fields = {
        "id": 1,
        "client_id": "slack-client",
        "name": "Slack",
        "redirect_uris": [SLACK_CALLBACK],
        "enabled": True,
        "created_by": "admin-user-id",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return BrokerClient(**(fields | overrides))


def _service_account_claims(**overrides) -> dict:
    claims = {
        "sub": "sa-sub",
        "azp": "slack-client",
        "aud": [config.oidc.client_id, "account"],
        "preferred_username": "service-account-slack-client",
    }
    return claims | overrides


class TestRequireBrokerClient:
    def _request(self, auth: str | None, service) -> MagicMock:
        request = MagicMock()
        request.headers = {"Authorization": auth} if auth else {}
        request.app.state.broker_service = service
        return request

    def _service(self, client: BrokerClient | None) -> SimpleNamespace:
        return SimpleNamespace(resolve_client=AsyncMock(return_value=client))

    def _validator(self, monkeypatch, claims=None, error=None) -> None:
        validator = MagicMock()
        validator.validate = AsyncMock(return_value=claims, side_effect=error)
        # The router validates through get_token_claims_from_request, which looks the
        # validator up in dependencies.
        monkeypatch.setattr(dependencies, "get_jwt_validator", MagicMock(return_value=validator))

    @pytest.mark.asyncio
    async def test_the_client_calling_as_itself_is_accepted(self, monkeypatch):
        self._validator(monkeypatch, _service_account_claims())
        service = self._service(_client())
        client = await router.require_broker_client(self._request("Bearer t", service), MagicMock())
        assert client.client_id == "slack-client"
        service.resolve_client.assert_awaited_once()
        assert service.resolve_client.await_args.args[1] == "slack-client"

    @pytest.mark.asyncio
    async def test_no_or_an_invalid_bearer_is_401(self, monkeypatch):
        service = self._service(_client())
        with pytest.raises(HTTPException) as exc:
            await router.require_broker_client(self._request(None, service), MagicMock())
        assert exc.value.status_code == 401
        self._validator(monkeypatch, error=JWTValidationError("bad signature"))
        with pytest.raises(HTTPException) as exc:
            await router.require_broker_client(self._request("Bearer t", service), MagicMock())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "claims",
        [
            # A user's access token issued to the same client: it must not mint for others.
            _service_account_claims(preferred_username="some.person", sid="user-session"),
            # The same, from a user whose IdP username copies the service account's.
            _service_account_claims(sid="user-session"),
            # Another client's service account presenting this client's azp.
            _service_account_claims(preferred_username="service-account-email-client"),
            # A service token meant for another audience.
            _service_account_claims(aud=["orchestrator"]),
            _service_account_claims(aud=None),
        ],
    )
    async def test_anything_but_the_clients_own_service_token_for_this_backend_is_403(self, monkeypatch, claims):
        self._validator(monkeypatch, claims)
        with pytest.raises(HTTPException) as exc:
            await router.require_broker_client(self._request("Bearer t", self._service(_client())), MagicMock())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("client", [None, _client(enabled=False)])
    async def test_an_unregistered_or_disabled_client_is_403(self, monkeypatch, client):
        self._validator(monkeypatch, _service_account_claims())
        with pytest.raises(HTTPException) as exc:
            await router.require_broker_client(self._request("Bearer t", self._service(client)), MagicMock())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_disabled_broker_is_503(self, monkeypatch):
        monkeypatch.setattr(config.broker, "enabled", False)
        with pytest.raises(HTTPException) as exc:
            await router.require_broker_client(self._request("Bearer t", self._service(_client())), MagicMock())
        assert exc.value.status_code == 503


@pytest_asyncio.fixture
async def broker(pg_session, test_admin_user_db, user_service) -> BrokerService:
    repo = BrokerClientRepository()
    repo.set_audit_service(AuditService())
    await repo.create_client(
        pg_session,
        test_admin_user_db,
        BrokerClientCreate(client_id="slack-client", name="Slack", redirect_uris=[SLACK_CALLBACK]),
    )
    await pg_session.commit()
    return BrokerService(
        client_repo=repo,
        login_request_repo=BrokerLoginRequestRepository(),
        user_service=user_service,
        scheduler_token_service=MagicMock(),
    )


@pytest.fixture
def token_service() -> MagicMock:
    service = MagicMock()
    service.store_offline_token = AsyncMock()
    return service


@pytest.fixture
def scheduler_service() -> MagicMock:
    service = MagicMock()
    service.release_sign_in_holds = AsyncMock(return_value=0)
    return service


@pytest.fixture
def controller(broker, user_service, token_service, scheduler_service) -> BrokerController:
    return BrokerController(
        broker_service=broker,
        user_service=user_service,
        scheduler_token_service=token_service,
        scheduler_service=scheduler_service,
        outbound_scim_push_service=MagicMock(),
    )


def _browser(query: dict | None = None) -> MagicMock:
    request = MagicMock()
    request.query_params = query or {}
    request.url_for = MagicMock(return_value="https://console.test/api/v1/auth/broker/callback")
    return request


async def _authorize(controller, mock_oauth, pg_session, client_state="their-state") -> str:
    """Start a login and return the OAuth state the controller handed to Authlib."""
    mock_oauth.authorize_redirect.reset_mock()
    await controller.authorize(
        _browser(), pg_session, client_id="slack-client", redirect_uri=SLACK_CALLBACK, client_state=client_state
    )
    return mock_oauth.authorize_redirect.await_args.kwargs["state"]


class TestBrowserLeg:
    @pytest.mark.asyncio
    async def test_authorize_sends_the_browser_to_keycloak_with_the_brokers_own_state(
        self, controller, mock_oauth, pg_session
    ):
        state = await _authorize(controller, mock_oauth, pg_session)

        args = mock_oauth.authorize_redirect.await_args
        assert args.args[1] == "https://console.test/api/v1/auth/broker/callback"
        assert state and state != "their-state"  # the client's state never goes to Keycloak

    @pytest.mark.asyncio
    async def test_authorize_refuses_an_unregistered_redirect_uri(self, controller, pg_session):
        with pytest.raises(BrokerRefusal) as exc:
            await controller.authorize(
                _browser(), pg_session, client_id="slack-client", redirect_uri="https://evil.example/cb", client_state=None
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_callback_signs_the_user_in_and_returns_a_code_with_the_clients_state(
        self, controller, broker, mock_oauth, pg_session, token_service, scheduler_service
    ):
        state = await _authorize(controller, mock_oauth, pg_session)
        mock_oauth.authorize_access_token.return_value = {
            "access_token": "at",
            "refresh_token": "offline-rt",
            "userinfo": {"sub": "new-sub", "email": "new@example.com", "given_name": "New", "groups": ["g1"]},
        }

        response = await controller.callback(_browser({"state": state}), pg_session)

        assert response.status_code == 303
        target = urlsplit(response.headers["location"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == SLACK_CALLBACK
        params = parse_qs(target.query)
        assert params["state"] == ["their-state"]
        code = params["code"][0]
        # The offline token went to the vault, and held subscriptions were released.
        token_service.store_offline_token.assert_awaited_once()
        assert token_service.store_offline_token.await_args.kwargs["refresh_token"] == "offline-rt"
        scheduler_service.release_sign_in_holds.assert_awaited_once()
        # The client can redeem the code for who signed in.
        identity = await broker.redeem(pg_session, await broker.resolve_client(pg_session, "slack-client"), code)
        assert (identity.sub, identity.email, identity.groups) == ("new-sub", "new@example.com", ["g1"])
        user_row = await pg_session.execute(text("SELECT id FROM users WHERE sub = 'new-sub'"))
        assert user_row.scalar() == identity.user_id

    @pytest.mark.asyncio
    async def test_a_failure_at_keycloak_goes_back_to_the_client_as_an_error(
        self, controller, mock_oauth, pg_session
    ):
        state = await _authorize(controller, mock_oauth, pg_session)
        mock_oauth.authorize_access_token.side_effect = OAuthError(error="access_denied", description="User cancelled")

        response = await controller.callback(_browser({"state": state}), pg_session)

        params = parse_qs(urlsplit(response.headers["location"]).query)
        assert params == {"error": ["access_denied"], "error_description": ["User cancelled"], "state": ["their-state"]}

    @pytest.mark.asyncio
    async def test_a_fault_after_keycloak_goes_back_to_the_client_too(
        self, controller, broker, mock_oauth, pg_session, user_service, monkeypatch
    ):
        """The user authenticated; a failure of ours must not leave them on a JSON error page."""
        state = await _authorize(controller, mock_oauth, pg_session)
        mock_oauth.authorize_access_token.return_value = {
            "refresh_token": "rt",
            "userinfo": {"sub": "s4", "email": "s4@example.com"},
        }
        monkeypatch.setattr(user_service, "upsert_user", AsyncMock(side_effect=RuntimeError("database is down")))

        response = await controller.callback(_browser({"state": state}), pg_session)

        target = urlsplit(response.headers["location"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == SLACK_CALLBACK
        params = parse_qs(target.query)
        assert params["error"] == ["server_error"] and params["state"] == ["their-state"]
        assert "code" not in params
        # Nothing was committed: the login is still open, so the user can try again.
        assert await broker.open_login(pg_session, state) is not None

    @pytest.mark.asyncio
    async def test_a_login_completed_meanwhile_goes_back_to_the_client_as_an_error(
        self, controller, broker, mock_oauth, pg_session, monkeypatch
    ):
        state = await _authorize(controller, mock_oauth, pg_session)
        mock_oauth.authorize_access_token.return_value = {
            "refresh_token": "rt",
            "userinfo": {"sub": "s5", "email": "s5@example.com"},
        }
        monkeypatch.setattr(broker, "issue_code", AsyncMock(side_effect=BrokerRefusal(400, "already completed")))

        response = await controller.callback(_browser({"state": state}), pg_session)

        params = parse_qs(urlsplit(response.headers["location"]).query)
        assert params["error"] == ["invalid_request"] and params["state"] == ["their-state"]

    @pytest.mark.asyncio
    async def test_a_callback_that_belongs_to_no_pending_login_is_refused_here(self, controller, pg_session):
        with pytest.raises(BrokerRefusal) as exc:
            await controller.callback(_browser({"state": "forged"}), pg_session)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_a_replayed_callback_cannot_issue_a_second_code(self, controller, mock_oauth, pg_session):
        state = await _authorize(controller, mock_oauth, pg_session)
        mock_oauth.authorize_access_token.return_value = {
            "refresh_token": "rt",
            "userinfo": {"sub": "s2", "email": "s2@example.com"},
        }
        await controller.callback(_browser({"state": state}), pg_session)
        with pytest.raises(BrokerRefusal):
            await controller.callback(_browser({"state": state}), pg_session)

    @pytest.mark.asyncio
    async def test_no_state_from_the_client_means_none_comes_back(self, controller, mock_oauth, pg_session):
        state = await _authorize(controller, mock_oauth, pg_session, client_state=None)
        mock_oauth.authorize_access_token.return_value = {
            "refresh_token": "rt",
            "userinfo": {"sub": "s3", "email": "s3@example.com"},
        }
        response = await controller.callback(_browser({"state": state}), pg_session)
        assert set(parse_qs(urlsplit(response.headers["location"]).query)) == {"code"}
