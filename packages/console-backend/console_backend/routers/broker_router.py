"""Token broker endpoints (``/api/v1/auth/broker``).

Browser leg (no authentication — the user signs in at Keycloak):
  GET  /authorize   — a broker client sends its user here to sign in
  GET  /callback    — Keycloak sends the user back; the browser returns to the client

Client leg (the broker client's own client-credentials token):
  POST /redeem      — trade the one-time code for who signed in
  POST /token       — mint an access token for a linked user and an allowed audience
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from ringier_a2a_sdk.auth import JWTValidationError

from ..config import config
from ..controllers.broker_controller import BrokerController
from ..db.session import DbSession
from ..dependencies import token_is_service_account
from ..models.broker import (
    BrokerClient,
    BrokerIdentity,
    BrokerRedeemRequest,
    BrokerTokenRequest,
    BrokerTokenResponse,
)
from ..services.broker_service import BrokerRefusal, BrokerService
from ..utils.jwt_validators import get_jwt_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth/broker", tags=["auth-broker"])


def _get_broker_service(request: Request) -> BrokerService:
    service = getattr(request.app.state, "broker_service", None)
    if service is None or not config.broker.enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The token broker is not enabled")
    return service


def _get_controller(request: Request) -> BrokerController:
    state = request.app.state
    return BrokerController(
        broker_service=_get_broker_service(request),
        user_service=state.user_service,
        scheduler_token_service=getattr(state, "scheduler_token_service", None),
        keycloak_admin_service=getattr(state, "keycloak_admin_service", None),
        scheduler_service=getattr(state, "scheduler_service", None),
        outbound_scim_push_service=getattr(state, "outbound_scim_push_service", None),
    )


def _refused(e: BrokerRefusal) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


async def require_broker_client(request: Request, db: DbSession) -> BrokerClient:
    """Accept only a registered, enabled broker client calling as itself.

    The bearer must be the client's own client-credentials token: a service-account
    token whose ``azp`` is the registered client id and whose audience includes this
    backend. A user's access token issued to the same client is refused — ``/token``
    mints for any user linked to the client, which only the client itself may ask for.
    """
    service = _get_broker_service(request)
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = await get_jwt_validator(issuer=config.oidc.issuer).validate(auth_header.split(" ", 1)[1])
    except JWTValidationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    client_id = claims.get("azp") or claims.get("client_id")
    audiences = claims.get("aud") or []
    if isinstance(audiences, str):
        audiences = [audiences]
    if not token_is_service_account(claims) or config.oidc.client_id not in audiences:
        logger.warning("Broker call refused: not a service-account token for this backend (azp=%s)", client_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A broker client must call with its own client-credentials token",
        )
    client = await service.resolve_client(db, client_id)
    if client is None or not client.enabled:
        logger.warning("Broker call refused: azp=%s is not a registered, enabled broker client", client_id)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a registered broker client")
    return client


@router.get("/authorize")
async def authorize(
    request: Request,
    db: DbSession,
    client_id: str = Query(min_length=1, max_length=255),
    redirect_uri: str = Query(min_length=1, max_length=2048),
    state: str | None = Query(default=None, max_length=2048),
) -> RedirectResponse:
    """Start a brokered sign-in. The browser comes back to ``redirect_uri`` with
    ``code`` (or ``error``) and the ``state`` given here, unchanged."""
    try:
        return await _get_controller(request).authorize(
            request, db, client_id=client_id, redirect_uri=redirect_uri, client_state=state
        )
    except BrokerRefusal as e:
        raise _refused(e) from e


@router.get("/callback")
async def broker_callback(request: Request, db: DbSession) -> RedirectResponse:
    """Keycloak's redirect target for brokered sign-ins. Registered on agent-console."""
    try:
        return await _get_controller(request).callback(request, db)
    except BrokerRefusal as e:
        raise _refused(e) from e


@router.post("/redeem", response_model=BrokerIdentity)
async def redeem(
    body: BrokerRedeemRequest,
    request: Request,
    db: DbSession,
    client: BrokerClient = Depends(require_broker_client),
) -> BrokerIdentity:
    """Trade a one-time code for who signed in. Single use, and only for the client the
    code was issued to."""
    try:
        identity = await _get_broker_service(request).redeem(db, client, body.code)
    except BrokerRefusal as e:
        raise _refused(e) from e
    await db.commit()
    return identity


@router.post("/token", response_model=BrokerTokenResponse)
async def mint_token(
    body: BrokerTokenRequest,
    request: Request,
    db: DbSession,
    client: BrokerClient = Depends(require_broker_client),
) -> BrokerTokenResponse:
    """Mint an access token for *audience* on behalf of a user who signed in through this
    client. 409 means the user must sign in again."""
    try:
        return await _get_broker_service(request).mint(db, client, body.sub, body.audience)
    except BrokerRefusal as e:
        raise _refused(e) from e
