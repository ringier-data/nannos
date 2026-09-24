"""Browser leg of the token broker: ``/authorize`` and ``/callback``.

The sign-in itself is the console's own: the same Keycloak client, scope and claim
handling (``onboard_user_from_userinfo``), under a separate Authlib registration so a
console login in the same browser cannot disturb it. What differs is where the browser
goes at the end: back to the broker client, with a one-time code instead of a session.
"""

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuthError, StarletteOAuth2App
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories.broker_login_request_repository import BrokerLoginRequest
from ..services.broker_service import BrokerRefusal, BrokerService
from .auth_controller import oauth, onboard_user_from_userinfo

if TYPE_CHECKING:
    from ..services.keycloak_admin_service import KeycloakAdminService
    from ..services.scheduler_service import SchedulerService
    from ..services.scheduler_token_service import SchedulerTokenService
    from ..services.user_service import UserService

logger = logging.getLogger(__name__)


class BrokerController:
    def __init__(
        self,
        broker_service: BrokerService,
        user_service: "UserService",
        scheduler_token_service: "SchedulerTokenService | None" = None,
        keycloak_admin_service: "KeycloakAdminService | None" = None,
        scheduler_service: "SchedulerService | None" = None,
        outbound_scim_push_service: Any = None,
    ) -> None:
        self.broker = broker_service
        self.user_service = user_service
        self.scheduler_token_service = scheduler_token_service
        self.keycloak_admin_service = keycloak_admin_service
        self.scheduler_service = scheduler_service
        self.outbound_scim_push_service = outbound_scim_push_service

    async def authorize(
        self,
        request: Request,
        db: AsyncSession,
        *,
        client_id: str,
        redirect_uri: str,
        client_state: str | None,
    ) -> RedirectResponse:
        """Start a sign-in for a broker client and send the browser to Keycloak."""
        state = await self.broker.begin_login(
            db, client_id=client_id, redirect_uri=redirect_uri, client_state=client_state
        )
        # The pending login must exist before Keycloak can send the browser back.
        await db.commit()
        broker_app: StarletteOAuth2App = oauth.broker  # type: ignore[assignment]
        return await broker_app.authorize_redirect(request, request.url_for("broker_callback"), state=state)

    async def callback(self, request: Request, db: AsyncSession) -> RedirectResponse:
        """Finish the sign-in and send the browser back to the client with a one-time code.

        Every failure after the pending login is known goes back to the client as
        ``?error=`` so it can tell its user: what Keycloak reports (the user cancelled, the
        session state is gone), a login completed by a concurrent callback, and a fault of
        ours while onboarding the user. Only a callback that belongs to no pending login is
        answered here, because there is nowhere safe to send it.
        """
        login = await self.broker.open_login(db, request.query_params.get("state"))
        if login is None:
            raise BrokerRefusal(400, "This sign-in link has expired or was already used. Start again from the app.")

        try:
            broker_app: StarletteOAuth2App = oauth.broker  # type: ignore[assignment]
            token = await broker_app.authorize_access_token(request)
        except OAuthError as e:
            logger.info("Broker sign-in for client %s ended at Keycloak: %s", login.client_id, e.error)
            return self._back_to_client(
                login, error="access_denied", error_description=e.description or e.error or "Sign-in failed"
            )

        userinfo = token.get("userinfo") or {}
        if not userinfo.get("sub") or not userinfo.get("email"):
            logger.error("Broker sign-in for client %s: token response carries no usable user info", login.client_id)
            return self._back_to_client(
                login, error="server_error", error_description="The sign-in returned no user information"
            )

        try:
            user = await onboard_user_from_userinfo(
                db,
                userinfo,
                token.get("refresh_token"),
                user_service=self.user_service,
                scheduler_token_service=self.scheduler_token_service,
                keycloak_admin_service=self.keycloak_admin_service,
                scheduler_service=self.scheduler_service,
            )
            code = await self.broker.issue_code(db, login, user, BrokerService.identity_from_userinfo(userinfo, user.id))
            await db.commit()
        except BrokerRefusal as e:
            # The login was completed under our feet (a concurrent callback): the user is
            # signed in, but this browser has no code to bring back.
            await db.rollback()
            logger.info("Broker sign-in for client %s could not issue a code: %s", login.client_id, e.detail)
            return self._back_to_client(login, error="invalid_request", error_description=e.detail)
        except Exception:  # noqa: BLE001 — the user authenticated; leave them with a way back
            await db.rollback()
            logger.exception("Broker sign-in for client %s failed after Keycloak", login.client_id)
            return self._back_to_client(
                login, error="server_error", error_description="Signing you in failed. Please try again."
            )
        # Outbound SCIM push, as the console login does; fire-and-forget after the commit.
        if self.outbound_scim_push_service is not None:
            self.outbound_scim_push_service.push_user(user.id, "update")
        return self._back_to_client(login, code=code)

    @staticmethod
    def _back_to_client(login: BrokerLoginRequest, **params: str) -> RedirectResponse:
        """Redirect to the client's registered callback. Its ``state`` comes back verbatim."""
        query = {key: value for key, value in params.items() if value}
        if login.client_state is not None:
            query["state"] = login.client_state
        # Registered redirect URIs carry no query (models.broker.validate_redirect_uri).
        return RedirectResponse(url=f"{login.redirect_uri}?{urlencode(query)}", status_code=303)
