"""Admin router for token-broker client registrations (``/api/v1/admin/broker-clients``).

A registration says which Keycloak client may send its users through the broker, where
the browser may be sent back to, and which audiences may be minted for it. Every write is
audited, and clears the broker's client cache so it applies at once.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..config import config
from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.broker import (
    BrokerClient,
    BrokerClientCreate,
    BrokerClientListResponse,
    BrokerClientUpdate,
    require_https_unless_local,
)
from ..models.user import User
from ..repositories.broker_client_repository import BrokerClientRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/broker-clients", tags=["admin-broker-clients"])


def _get_repository(request: Request) -> BrokerClientRepository:
    return request.app.state.broker_client_repository


def _invalidate_broker_cache(request: Request) -> None:
    service = getattr(request.app.state, "broker_service", None)
    if service is not None:
        service.invalidate_cache()


def _check_redirect_uris(redirect_uris: list[str] | None) -> None:
    if redirect_uris is None:
        return
    try:
        require_https_unless_local(
            redirect_uris, is_local=config.is_local(), allow_loopback=config.is_dev()
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e


@router.post("", response_model=BrokerClient, status_code=status.HTTP_201_CREATED)
async def create_broker_client(
    body: BrokerClientCreate,
    request: Request,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> BrokerClient:
    """Register a broker client."""
    _check_redirect_uris(body.redirect_uris)
    try:
        client = await _get_repository(request).create_client(db, admin, body)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    await db.commit()
    _invalidate_broker_cache(request)
    return client


@router.get("", response_model=BrokerClientListResponse)
async def list_broker_clients(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
) -> BrokerClientListResponse:
    """List every registered broker client."""
    return BrokerClientListResponse(clients=await _get_repository(request).list_all(db))


@router.get("/{client_pk}", response_model=BrokerClient)
async def get_broker_client(
    client_pk: int,
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
) -> BrokerClient:
    client = await _get_repository(request).get_by_id(db, client_pk)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Broker client not found"
        )
    return client


@router.patch("/{client_pk}", response_model=BrokerClient)
async def update_broker_client(
    client_pk: int,
    body: BrokerClientUpdate,
    request: Request,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> BrokerClient:
    """Change a broker client. Omitted fields are unchanged."""
    _check_redirect_uris(body.redirect_uris)
    client = await _get_repository(request).update_client(db, admin, client_pk, body)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Broker client not found"
        )
    await db.commit()
    _invalidate_broker_cache(request)
    return client


@router.delete("/{client_pk}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broker_client(
    client_pk: int,
    request: Request,
    db: DbSession,
    admin: User = Depends(require_admin),
) -> None:
    """Remove a broker client. Its pending sign-ins and user links go with it; the users'
    vaulted offline tokens stay."""
    deleted = await _get_repository(request).delete_client(db, admin, client_pk)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Broker client not found"
        )
    await db.commit()
    _invalidate_broker_cache(request)
