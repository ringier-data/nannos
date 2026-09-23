"""Admin CRUD for broker client registrations, driven through the endpoint functions
against the test database. Every write must clear the broker's client cache."""

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

import console_backend.routers.broker_client_router as router
from console_backend.config import config
from console_backend.models.broker import BrokerClientCreate, BrokerClientUpdate
from console_backend.repositories.broker_client_repository import BrokerClientRepository
from console_backend.services.audit_service import AuditService


@pytest_asyncio.fixture
async def request_(pg_session) -> MagicMock:
    repo = BrokerClientRepository()
    repo.set_audit_service(AuditService())
    request = MagicMock()
    request.app.state.broker_client_repository = repo
    request.app.state.broker_service = MagicMock()
    return request


def _body(**overrides) -> BrokerClientCreate:
    fields = {
        "client_id": "email-client",
        "name": "Email",
        "redirect_uris": ["https://email.nannos.ringier.ch/api/v1/oauth/callback"],
        "audiences": ["orchestrator"],
    }
    return BrokerClientCreate(**(fields | overrides))


@pytest.mark.asyncio
async def test_create_list_update_delete(request_, pg_session, test_admin_user_db):
    created = await router.create_broker_client(_body(), request_, pg_session, test_admin_user_db)
    assert created.client_id == "email-client" and created.created_by == test_admin_user_db.id
    request_.app.state.broker_service.invalidate_cache.assert_called_once()

    listed = await router.list_broker_clients(request_, pg_session, test_admin_user_db)
    assert [c.client_id for c in listed.clients] == ["email-client"]

    updated = await router.update_broker_client(
        created.id, BrokerClientUpdate(audiences=["orchestrator", "agent-console"]), request_, pg_session, test_admin_user_db
    )
    assert updated.audiences == ["orchestrator", "agent-console"]
    assert updated.redirect_uris == created.redirect_uris  # untouched

    # Description is the one nullable field: an omitted field keeps it, a null clears it.
    described = await router.update_broker_client(
        created.id, BrokerClientUpdate(description="Mail bot"), request_, pg_session, test_admin_user_db
    )
    assert described.description == "Mail bot"
    kept = await router.update_broker_client(
        created.id, BrokerClientUpdate(enabled=False), request_, pg_session, test_admin_user_db
    )
    assert kept.description == "Mail bot" and kept.enabled is False
    cleared = await router.update_broker_client(
        created.id, BrokerClientUpdate(description=None), request_, pg_session, test_admin_user_db
    )
    assert cleared.description is None

    await router.delete_broker_client(created.id, request_, pg_session, test_admin_user_db)
    with pytest.raises(HTTPException) as exc:
        await router.get_broker_client(created.id, request_, pg_session, test_admin_user_db)
    assert exc.value.status_code == 404
    assert request_.app.state.broker_service.invalidate_cache.call_count == 6


@pytest.mark.asyncio
async def test_a_second_registration_of_the_same_client_is_409(request_, pg_session, test_admin_user_db):
    await router.create_broker_client(_body(), request_, pg_session, test_admin_user_db)
    with pytest.raises(HTTPException) as exc:
        await router.create_broker_client(_body(name="Again"), request_, pg_session, test_admin_user_db)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_plain_http_redirects_are_refused_outside_local_development(
    request_, pg_session, test_admin_user_db, monkeypatch
):
    monkeypatch.setattr(config, "environment", "prod")
    with pytest.raises(HTTPException) as exc:
        await router.create_broker_client(
            _body(redirect_uris=["http://email.nannos.ringier.ch/api/v1/oauth/callback"]),
            request_,
            pg_session,
            test_admin_user_db,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_unknown_ids_are_404(request_, pg_session, test_admin_user_db):
    with pytest.raises(HTTPException) as exc:
        await router.update_broker_client(999, BrokerClientUpdate(enabled=False), request_, pg_session, test_admin_user_db)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await router.delete_broker_client(999, request_, pg_session, test_admin_user_db)
    assert exc.value.status_code == 404
