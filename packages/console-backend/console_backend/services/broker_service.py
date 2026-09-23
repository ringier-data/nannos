"""Token broker: console-backend signs users in on behalf of registered clients (the chat
clients, the cockpit BFF), keeps the single offline token per user, and mints
audience-scoped access tokens for those clients when they ask.

Why a broker: Keycloak binds a refresh token to the client it was issued to, so a token a
chat client holds is useless to the scheduler here, and every client holding its own copy
leaves the scheduler blind to users who never opened the console. With the broker there
is one sign-in, one custodian, and signing in anywhere makes a user scheduler-ready.

The flow, per login:

1. The client sends the browser to ``/authorize`` with its client id, its registered
   callback and its own state. ``begin_login`` records a pending login keyed by a hash of
   the OAuth state it hands to Keycloak.
2. Keycloak sends the browser back to ``/callback``. The controller runs the ordinary
   console sign-in (user upsert, offline token vaulted), and ``issue_code`` stores a
   one-time code and a snapshot of who signed in.
3. The browser lands on the client's callback with ``code`` and the client's ``state``.
   The client ``redeem``\\s the code, authenticated with its own client-credentials token.
   Redeeming also links the user to that client.
4. From then on the client asks ``mint`` for tokens for the audiences it is allowed, for
   users linked to it.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..models.broker import BrokerClient, BrokerIdentity, BrokerTokenResponse, validate_redirect_uri
from ..models.user import User
from ..repositories.broker_client_repository import BrokerClientRepository
from ..repositories.broker_login_request_repository import BrokerLoginRequest, BrokerLoginRequestRepository
from .scheduler_token_service import NoOfflineTokenError

if TYPE_CHECKING:
    from .scheduler_token_service import SchedulerTokenService
    from .user_service import UserService

logger = logging.getLogger(__name__)

#: Client lookups happen on every redeem and mint; a short cache keeps them off the
#: database, and every admin write clears it.
_CLIENT_CACHE_TTL_SECONDS = 60.0
#: Expired login rows linger this long before the opportunistic purge removes them.
_PURGE_GRACE = timedelta(hours=1)
#: Used when Keycloak's exchange response carries no ``expires_in``.
_DEFAULT_EXPIRES_IN = 300


class BrokerRefusal(Exception):
    """A broker request that cannot be honoured, with the HTTP status to answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _is_invalid_grant(exc: httpx.HTTPStatusError) -> bool:
    """Keycloak rejected the credential itself: expired, revoked, or its session is gone."""
    if exc.response.status_code != 400:
        return False
    try:
        return exc.response.json().get("error") == "invalid_grant"
    except ValueError:
        return False


class BrokerService:
    def __init__(
        self,
        *,
        client_repo: BrokerClientRepository,
        login_request_repo: BrokerLoginRequestRepository,
        user_service: "UserService",
        scheduler_token_service: "SchedulerTokenService",
    ) -> None:
        self._clients = client_repo
        self._requests = login_request_repo
        self._users = user_service
        self._tokens = scheduler_token_service
        # client_id -> (monotonic expiry, client). Registered clients only: the ids come
        # from callers, and ``/authorize`` takes them unauthenticated.
        self._client_cache: dict[str, tuple[float, BrokerClient]] = {}

    # ---------------------------------------------------------------- clients

    async def resolve_client(self, db: AsyncSession, client_id: str | None) -> BrokerClient | None:
        """The registered client with this id, or None. A found client is cached for a
        minute; an unknown id is looked up every time, so random ids cannot fill the cache.

        Every replica has its own cache, and an admin write clears only the cache of the
        replica that served it. A change can therefore take up to a minute to reach all.
        """
        if not client_id:
            return None
        cached = self._client_cache.get(client_id)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        client = await self._clients.get_by_client_id(db, client_id)
        if client is None:
            self._client_cache.pop(client_id, None)
        else:
            self._client_cache[client_id] = (time.monotonic() + _CLIENT_CACHE_TTL_SECONDS, client)
        return client

    def invalidate_cache(self) -> None:
        self._client_cache.clear()

    # ------------------------------------------------------------------ login

    async def begin_login(
        self, db: AsyncSession, *, client_id: str, redirect_uri: str, client_state: str | None
    ) -> str:
        """Record a login for *client_id* and return the OAuth state to send to Keycloak.

        The caller must commit before redirecting: the row has to exist when Keycloak
        sends the browser back.
        """
        client = await self.resolve_client(db, client_id)
        if client is None:
            raise BrokerRefusal(404, "Unknown broker client")
        if not client.enabled:
            raise BrokerRefusal(403, "This broker client is disabled")
        try:
            validate_redirect_uri(redirect_uri, allow_wildcard=False)
        except ValueError:
            raise BrokerRefusal(400, "redirect_uri is not a valid redirect URI") from None
        if not client.allows_redirect_uri(redirect_uri):
            raise BrokerRefusal(400, "redirect_uri is not registered for this broker client")

        now = datetime.now(timezone.utc)
        await self._requests.purge_expired(db, before=now - _PURGE_GRACE)
        state = secrets.token_urlsafe(32)
        await self._requests.create_pending(
            db,
            state_hash=_digest(state),
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            client_state=client_state,
            now=now,
            expires_at=now + timedelta(seconds=config.broker.login_request_ttl_seconds),
        )
        return state

    async def open_login(self, db: AsyncSession, state: str | None) -> BrokerLoginRequest | None:
        """The pending login Keycloak's callback belongs to, or None when it is unknown,
        expired, or was already completed."""
        if not state:
            return None
        return await self._requests.get_open(db, _digest(state), datetime.now(timezone.utc))

    async def issue_code(
        self, db: AsyncSession, login: BrokerLoginRequest, user: User, identity: BrokerIdentity
    ) -> str:
        """Complete *login*: store a one-time code and the identity, and return the code."""
        now = datetime.now(timezone.utc)
        code = secrets.token_urlsafe(32)
        issued = await self._requests.issue_code(
            db,
            state_hash=login.state_hash,
            code_hash=_digest(code),
            user_id=user.id,
            identity=identity.model_dump(),
            now=now,
            code_expires_at=now + timedelta(seconds=config.broker.code_ttl_seconds),
        )
        if not issued:
            raise BrokerRefusal(400, "This sign-in was already completed or has expired. Start again from the app.")
        logger.info("Broker sign-in: user %s via client %s", user.id, login.client_id)
        return code

    @staticmethod
    def identity_from_userinfo(userinfo: dict[str, Any], user_id: str) -> BrokerIdentity:
        """What ``/redeem`` returns: the union of the claims broker clients read."""
        groups = userinfo.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]

        def _text(key: str) -> str | None:
            value = userinfo.get(key)
            return str(value) if value not in (None, "") else None

        verified = userinfo.get("email_verified")
        return BrokerIdentity(
            user_id=user_id,
            sub=str(userinfo["sub"]),
            email=_text("email"),
            email_verified=verified if isinstance(verified, bool) else None,
            name=_text("name"),
            preferred_username=_text("preferred_username"),
            given_name=_text("given_name"),
            family_name=_text("family_name"),
            groups=[str(g) for g in groups],
            phone_number=_text("phone_number"),
            phone_number_idp=_text("phone_number_idp"),
            company_name=_text("company_name"),
        )

    # --------------------------------------------------------- client calls

    async def redeem(self, db: AsyncSession, client: BrokerClient, code: str) -> BrokerIdentity:
        """Trade a one-time code for the identity of who signed in. Single use."""
        identity = await self._requests.redeem(
            db, code_hash=_digest(code), client_id=client.client_id, now=datetime.now(timezone.utc)
        )
        if identity is None:
            # Unknown, expired, reused, or issued to another client: one answer for all.
            logger.warning("Broker redeem refused for client %s", client.client_id)
            raise BrokerRefusal(400, "invalid_grant")
        return BrokerIdentity(**identity)

    async def mint(self, db: AsyncSession, client: BrokerClient, sub: str, audience: str) -> BrokerTokenResponse:
        """An access token for *audience* on behalf of the user *sub*, from their vaulted
        offline token (refresh, then RFC 8693 exchange).

        Every "this user cannot be served" answer is a 409, because the remedy is always
        the same: the client signs the user in again through the broker.
        """
        if audience not in client.audiences:
            raise BrokerRefusal(403, f"This broker client may not mint tokens for audience {audience!r}")
        user = await self._users.get_user_by_sub(db, sub)
        # A client reaches only the users who signed in through it. An unknown subject
        # gets the same answer, so the check does not tell a client who exists.
        if user is None or not await self._requests.is_linked(db, client.client_id, user.id):
            raise BrokerRefusal(409, "The user has not signed in through this client")
        try:
            data = await self._tokens.get_exchanged_token_response(db, user.id, audience)
        except NoOfflineTokenError:
            # Signed in somewhere, but never through a flow that vaults the offline token.
            # Only this case: any other ValueError (a non-JSON Keycloak reply, a bad vault
            # blob) is a fault of ours and must not make the client drop the sign-in.
            raise BrokerRefusal(409, "User has not signed in to Nannos (no offline token)") from None
        except httpx.HTTPStatusError as exc:
            if _is_invalid_grant(exc):
                # The vaulted token is dead (30 days idle, revoked, or its offline session
                # ended). Signing in again replaces it; the client's cue is the same 409.
                logger.info("Broker mint: vaulted offline token of user %s is no longer valid", user.id)
                raise BrokerRefusal(409, "The user's Nannos sign-in has expired; they must sign in again") from None
            logger.error(
                "Broker mint for client %s, audience %s failed: Keycloak %s",
                client.client_id,
                audience,
                exc.response.status_code,
            )
            raise BrokerRefusal(502, f"Keycloak refused to mint a token for audience {audience!r}") from None
        except httpx.HTTPError as exc:
            logger.error("Broker mint for client %s: Keycloak unreachable (%s)", client.client_id, exc)
            raise BrokerRefusal(502, "Keycloak is unreachable") from None
        except Exception:  # noqa: BLE001 — e.g. KMS; same answer as the federated exchange
            logger.exception("Broker mint for client %s, audience %s failed", client.client_id, audience)
            raise BrokerRefusal(502, "Failed to mint a token") from None
        return BrokerTokenResponse(
            access_token=data["access_token"],
            expires_in=int(data.get("expires_in") or _DEFAULT_EXPIRES_IN),
        )
