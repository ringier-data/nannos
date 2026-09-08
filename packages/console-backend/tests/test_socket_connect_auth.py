"""Socket.IO connect authentication: the bearer-token path (embedded hosts) works in every
environment, production included (ADR-0002 Amendments 2 and 4). It used to be gated to
non-production during the embed spike, which silently broke the cockpit widget in prod."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_sio(user_id: str = "user-1") -> MagicMock:
    sio = MagicMock()
    sio.app_instance = MagicMock()
    sio.app_instance.state.session_service.get_session = AsyncMock(
        return_value=SimpleNamespace(user_id=user_id)
    )
    sio.app_instance.state.socket_session_service.create_session = AsyncMock()
    return sio


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["dev", "stg", "prod"])
async def test_bearer_token_authenticates_socket_in_every_environment(
    monkeypatch, environment
):
    import app as app_module

    monkeypatch.setattr(app_module.config, "environment", environment)
    app_module._socket_owned_sessions.pop("sid-1", None)
    resolve_token = AsyncMock(return_value="stored-session-1")
    notifications = MagicMock()

    with (
        patch("app.sio", _mock_sio()),
        patch("app._resolve_socket_user_via_cookie", AsyncMock(return_value=None)),
        patch("app._resolve_socket_user_via_token", resolve_token),
        patch("app.socket_notification_manager", notifications),
    ):
        accepted = await app_module.handle_connect(
            "sid-1", environ={}, auth={"token": "a.nannos.jwt"}
        )

    assert accepted is True
    resolve_token.assert_awaited_once_with("a.nannos.jwt")
    # The token path mints a socket-owned StoredSession that handle_disconnect must destroy.
    assert app_module._socket_owned_sessions.get("sid-1") == "stored-session-1"
    notifications.register_connection.assert_called_once_with("user-1", "sid-1")


@pytest.mark.asyncio
async def test_no_cookie_and_no_token_is_rejected(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module.config, "environment", "prod")
    resolve_token = AsyncMock(return_value=None)

    with (
        patch("app.sio", _mock_sio()),
        patch("app._resolve_socket_user_via_cookie", AsyncMock(return_value=None)),
        patch("app._resolve_socket_user_via_token", resolve_token),
    ):
        assert await app_module.handle_connect("sid-2", environ={}, auth=None) is False
        assert (
            await app_module.handle_connect("sid-3", environ={}, auth={"token": "bad"})
            is False
        )

    # No token → the validator is never consulted; a bad token → consulted once and rejected.
    resolve_token.assert_awaited_once_with("bad")
    assert "sid-2" not in app_module._socket_owned_sessions
    assert "sid-3" not in app_module._socket_owned_sessions
