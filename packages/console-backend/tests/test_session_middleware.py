"""SessionMiddleware: every user lookup runs on an open DB session.

A closed AsyncSession silently checks out a new pooled connection on reuse and never
returns it. The impersonation lookup used to run after the session block had closed,
leaking one connection per impersonated request until the pool was exhausted.
"""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request

from console_backend.config import config
from console_backend.dependencies import ADMIN_MODE_HEADER, IMPERSONATE_USER_HEADER
from console_backend.middleware import session_middleware
from console_backend.middleware.session_middleware import SessionMiddleware

ADMIN = SimpleNamespace(id="admin-id", email="admin@example.com", is_administrator=True)
TARGET = SimpleNamespace(id="target-id", email="target@example.com", is_administrator=False)


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
    def __init__(self) -> None:
        self.lookups: list[tuple[str, bool]] = []

    async def get_user(self, db, user_id):
        self.lookups.append((user_id, db.open))
        return {u.id: u for u in (ADMIN, TARGET)}.get(user_id)


class _SessionService:
    async def get_session(self, session_id):
        return SimpleNamespace(
            user_id=ADMIN.id,
            id_token=None,
            access_token=None,
            access_token_expires_at=None,
            refresh_token=None,
        )


def _app(user_service: _UserService) -> FastAPI:
    app = FastAPI()
    app.state.session_service = _SessionService()
    app.state.user_service = user_service
    app.add_middleware(SessionMiddleware)

    @app.get("/whoami")
    async def whoami(request: Request):
        original = getattr(request.state, "original_user", None)
        return {"user": request.state.user.id, "original": original.id if original else None}

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("admin_mode", ["true", "false"])
async def test_impersonation_lookups_run_on_open_session(admin_mode):
    user_service = _UserService()
    with (
        patch.object(session_middleware, "get_async_session_factory", return_value=_TrackedSession),
        patch.object(session_middleware, "verify_cookie", return_value="sid"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(user_service)), base_url="http://test"
        ) as client:
            client.cookies.set(config.cookie_name, "signed")
            response = await client.get(
                "/whoami",
                headers={IMPERSONATE_USER_HEADER: TARGET.id, ADMIN_MODE_HEADER: admin_mode},
            )

    assert response.status_code == 200
    if admin_mode == "true":
        assert response.json() == {"user": TARGET.id, "original": ADMIN.id}
        assert user_service.lookups == [(ADMIN.id, True), (TARGET.id, True)]
    else:
        assert response.json() == {"user": ADMIN.id, "original": None}
        assert user_service.lookups == [(ADMIN.id, True)]
