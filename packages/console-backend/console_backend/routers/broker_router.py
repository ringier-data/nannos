"""Token broker endpoints (``/api/v1/auth/broker``).

Browser leg (no authentication — the user signs in at Keycloak):
  GET  /authorize   — a broker client sends its user here to sign in
  GET  /callback    — Keycloak sends the user back; the browser returns to the client

Client leg (the broker client's own client-credentials token):
  POST /redeem      — trade the one-time code for who signed in and a binding secret
  POST /token       — mint an access token for a bound user and an allowed audience
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from ..config import config
from ..controllers.broker_controller import BrokerController
from ..db.session import DbSession
from ..dependencies import get_token_claims_from_request, own_client_credentials_client
from ..models.broker import (
    BrokerClient,
    BrokerRedeemRequest,
    BrokerRedemption,
    BrokerTokenRequest,
    BrokerTokenResponse,
)
from ..services.broker_service import BrokerRefusal, BrokerService

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

    The bearer must be the client's own client-credentials token
    whose audience includes this backend (``own_client_credentials_client``). A user's access
    token issued to the same client is refused — ``/token`` mints for any user linked to
    the client, which only the client itself may ask for.
    """
    service = _get_broker_service(request)
    claims = await get_token_claims_from_request(request)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    client_id = own_client_credentials_client(claims)
    if client_id is None:
        logger.warning(
            "Broker call refused: not a service-account token for this backend (azp=%s)",
            claims.get("azp") or claims.get("client_id"),
        )
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


@router.post("/redeem", response_model=BrokerRedemption)
async def redeem(
    body: BrokerRedeemRequest,
    request: Request,
    db: DbSession,
    client: BrokerClient = Depends(require_broker_client),
) -> BrokerRedemption:
    """Trade a one-time code for who signed in, and the binding secret for their later
    /token calls. Single use, and only for the client the code was issued to."""
    try:
        identity = await _get_broker_service(request).redeem(
            db,
            client,
            body.code,
            account_key=body.account_key,
            workspace_id=body.workspace_id,
        )
    except BrokerRefusal as e:
        raise _refused(e) from e
    await db.commit()
    await _release_reachability_holds(request, db, client, identity.user_id, body.workspace_id)
    return identity


async def _release_reachability_holds(
    request: Request, db: DbSession, client: BrokerClient, user_id: str, workspace_id: str
) -> None:
    """A bound sign-in reaches every channel of its workspace: switch on the user's
    subscriptions held on those channels (#192). Best effort, after the sign-in is
    committed: a failure here leaves them held, which the next sign-in retries."""
    scheduler = getattr(request.app.state, "scheduler_service", None)
    if scheduler is None or not workspace_id:
        return
    try:
        user = await request.app.state.user_service.get_user(db, user_id)
        if user is not None:
            released = await scheduler.release_reachability_holds(db, user, client.client_id, workspace_id)
            if released:
                logger.info("Switched on %d subscription(s) of user %s reachable again", released, user_id)
    except Exception:  # noqa: BLE001 — the sign-in itself has succeeded
        await db.rollback()
        logger.warning("Could not release reachability holds of user %s", user_id, exc_info=True)


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
        return await _get_broker_service(request).mint(db, client, body.sub, body.audience, body.binding_secret)
    except BrokerRefusal as e:
        raise _refused(e) from e
