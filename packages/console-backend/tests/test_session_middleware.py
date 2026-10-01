"""SessionMiddleware: every user lookup runs on an open DB session.

A closed AsyncSession silently checks out a new pooled connection on reuse and nothing
checks it back in until the garbage collector terminates it. The impersonation lookup
used to run after the session block had closed, so impersonated requests exhausted the
pool faster than GC reclaimed the connections.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request

from console_backend.config import config
from console_backend.dependencies import ADMIN_MODE_HEADER, IMPERSONATE_USER_HEADER
from console_backend.middleware import session_middleware
from console_backend.middleware.session_middleware import SessionMiddleware
from console_backend.models.user import UserStatus

ADMIN = SimpleNamespace(id="admin-id", email="admin@example.com", is_administrator=True, status=UserStatus.ACTIVE)
TARGET = SimpleNamespace(id="target-id", email="target@example.com", is_administrator=False, status=UserStatus.ACTIVE)
SUSPENDED_ADMIN = SimpleNamespace(
    id="suspended-admin-id", email="suspended@example.com", is_administrator=True, status=UserStatus.SUSPENDED
)


class _TrackedSession:
    """Stand-in for AsyncSession that records whether it is still inside its block."""

    def __init__(self) -> None:
        self.open = False

    async def __aenter__(self):
        self.open = True
        return self

    async def __aexit__(self, *exc):
        self.open = False


class _UserService:
    def __init__(self, fail_fetch: bool = False) -> None:
        self.lookups: list[tuple[str, bool]] = []
        self.fail_fetch = fail_fetch

    async def get_user(self, db, user_id):
        self.lookups.append((user_id, db.open))
        return {u.id: u for u in (ADMIN, TARGET, SUSPENDED_ADMIN)}.get(user_id)

    async def fetch_user(self, db, user_id):
        if self.fail_fetch:
            self.lookups.append((user_id, db.open))
            raise ConnectionError("pool exhausted")
        return await self.get_user(db, user_id)


class _SessionService:
    def __init__(self, session_user_id: str) -> None:
        self.session_user_id = session_user_id

    async def get_session(self, session_id):
        return SimpleNamespace(
            user_id=self.session_user_id,
            id_token=None,
            access_token=None,
            access_token_expires_at=None,
            refresh_token=None,
        )


def _app(user_service: _UserService, session_user_id: str) -> FastAPI:
    app = FastAPI()
    app.state.session_service = _SessionService(session_user_id)
    app.state.user_service = user_service
    app.add_middleware(SessionMiddleware)

    @app.get("/whoami")
    async def whoami(request: Request):
        original = getattr(request.state, "original_user", None)
        return {"user": request.state.user.id, "original": original.id if original else None}

    return app


async def _impersonate(user_service: _UserService, session_user: SimpleNamespace, admin_mode: str) -> httpx.Response:
    with (
        patch.object(session_middleware, "get_async_session_factory", return_value=_TrackedSession),
        patch.object(session_middleware, "verify_cookie", return_value="sid"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(user_service, session_user.id)), base_url="http://test"
        ) as client:
            client.cookies.set(config.cookie_name, "signed")
            return await client.get(
                "/whoami",
                headers={IMPERSONATE_USER_HEADER: TARGET.id, ADMIN_MODE_HEADER: admin_mode},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_user", "admin_mode", "expected_user", "expected_original", "expected_lookups"),
    [
        # Admin in admin mode: impersonation applies, both lookups on the open session.
        (ADMIN, "true", TARGET, ADMIN, [(ADMIN.id, True), (TARGET.id, True)]),
        # Admin without admin mode: refused.
        (ADMIN, "false", ADMIN, None, [(ADMIN.id, True)]),
        # Non-admin claiming admin mode: refused.
        (TARGET, "true", TARGET, None, [(TARGET.id, True)]),
        # Suspended admin: refused.
        (SUSPENDED_ADMIN, "true", SUSPENDED_ADMIN, None, [(SUSPENDED_ADMIN.id, True)]),
    ],
)
async def test_impersonation_lookups_run_on_open_session(
    caplog, session_user, admin_mode, expected_user, expected_original, expected_lookups
):
    caplog.set_level(logging.DEBUG, logger=session_middleware.logger.name)
    user_service = _UserService()
    response = await _impersonate(user_service, session_user, admin_mode)

    assert response.status_code == 200
    assert response.json() == {
        "user": expected_user.id,
        "original": expected_original.id if expected_original else None,
    }
    assert user_service.lookups == expected_lookups
    # User identity is logged by id, never by email address.
    assert caplog.records
    assert ADMIN.email not in caplog.text
    assert TARGET.email not in caplog.text


@pytest.mark.asyncio
async def test_failed_target_lookup_fails_closed():
    """A DB error on the target lookup must not run the request as the admin."""
    user_service = _UserService(fail_fetch=True)
    response = await _impersonate(user_service, ADMIN, "true")

    assert response.status_code == 503
    # The console's lockout recovery keys on this code (AuthContext.tsx); pin the literal.
    assert response.json()["code"] == "impersonation_unavailable"
    assert user_service.lookups == [(ADMIN.id, True), (TARGET.id, True)]
