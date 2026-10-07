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
    resolve_token = AsyncMock(return_value=app_module._SocketTokenAuth("stored-session-1"))
    notifications = MagicMock()
    sio = _mock_sio()

    with (
        patch("app.sio", sio),
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
    # An unbound token leaves the socket session unstamped.
    create_session = sio.app_instance.state.socket_session_service.create_session
    assert create_session.await_args.kwargs["embedded_sub_agent_id"] is None


@pytest.mark.asyncio
async def test_bound_token_stamps_the_socket_session(monkeypatch):
    """Embed bindings (ADR-0006): the sub-agent bound to the token's azp is stamped on the
    socket session at connect, so send_message can scope turns without trusting the client."""
    import app as app_module

    monkeypatch.setattr(app_module.config, "environment", "prod")
    app_module._socket_owned_sessions.pop("sid-9", None)
    resolve_token = AsyncMock(
        return_value=app_module._SocketTokenAuth("stored-session-9", embedded_sub_agent_id=20)
    )
    sio = _mock_sio()

    with (
        patch("app.sio", sio),
        patch("app._resolve_socket_user_via_cookie", AsyncMock(return_value=None)),
        patch("app._resolve_socket_user_via_token", resolve_token),
        patch("app.socket_notification_manager", MagicMock()),
    ):
        assert await app_module.handle_connect("sid-9", environ={}, auth={"token": "bound.jwt"}) is True

    create_session = sio.app_instance.state.socket_session_service.create_session
    create_session.assert_awaited_once()
    assert create_session.await_args.kwargs["embedded_sub_agent_id"] == 20
    assert create_session.await_args.kwargs["http_session_id"] == "stored-session-9"
    assert app_module._socket_owned_sessions.get("sid-9") == "stored-session-9"


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


def _cookie_connect_mocks(bind_result=20, bind_error: Exception | None = None):
    """A cookie-authenticated socket plus the services `_bind_console_assistant` reaches."""
    sio = _mock_sio()
    user = SimpleNamespace(id="user-1")
    sio.app_instance.state.user_service.get_user = AsyncMock(return_value=user)
    embed = MagicMock()
    embed.bind_connection = AsyncMock(return_value=bind_result, side_effect=bind_error)
    sio.app_instance.state.embed_binding_service = embed
    db = MagicMock()
    db.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return sio, embed, db, factory, user


@pytest.mark.asyncio
async def test_console_assistant_cookie_socket_binds_by_console_client_id(monkeypatch):
    """The console hosting its own assistant: a cookie socket that asks for embed scope is
    bound through the binding listing the console's OIDC client id, like a host's token."""
    import app as app_module

    monkeypatch.setattr(app_module.config.oidc, "client_id", "agent-console")
    sio, embed, db, factory, user = _cookie_connect_mocks(bind_result=20)

    with (
        patch("app.sio", sio),
        patch("app.get_async_session_factory", return_value=factory),
        patch("app._resolve_socket_user_via_cookie", AsyncMock(return_value="browser-session")),
        patch("app.socket_notification_manager", MagicMock()),
    ):
        assert await app_module.handle_connect("sid-c1", environ={}, auth={"embedScope": True}) is True

    embed.bind_connection.assert_awaited_once_with(db, user=user, azp="agent-console")
    db.commit.assert_awaited_once()
    create_session = sio.app_instance.state.socket_session_service.create_session
    assert create_session.await_args.kwargs["embedded_sub_agent_id"] == 20
    assert create_session.await_args.kwargs["http_session_id"] == "browser-session"
    # The browser login's session is never socket-owned (handle_disconnect must not delete it).
    assert "sid-c1" not in app_module._socket_owned_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [None, {}, {"embedScope": "yes"}])
async def test_cookie_socket_without_the_opt_in_stays_unbound(auth):
    """The console's main chat connects with the same cookie and must keep the orchestrator."""
    import app as app_module

    sio, embed, _db, factory, _user = _cookie_connect_mocks()

    with (
        patch("app.sio", sio),
        patch("app.get_async_session_factory", return_value=factory),
        patch("app._resolve_socket_user_via_cookie", AsyncMock(return_value="browser-session")),
        patch("app.socket_notification_manager", MagicMock()),
    ):
        assert await app_module.handle_connect("sid-c2", environ={}, auth=auth) is True

    embed.bind_connection.assert_not_awaited()
    create_session = sio.app_instance.state.socket_session_service.create_session
    assert create_session.await_args.kwargs["embedded_sub_agent_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_result,bind_error", [(None, None), (None, RuntimeError("db down"))])
async def test_console_assistant_binding_problem_connects_unbound(bind_result, bind_error):
    """No binding for the console client yet, or a lookup failure: connect, unscoped."""
    import app as app_module

    sio, _embed, db, factory, _user = _cookie_connect_mocks(bind_result=bind_result, bind_error=bind_error)

    with (
        patch("app.sio", sio),
        patch("app.get_async_session_factory", return_value=factory),
        patch("app._resolve_socket_user_via_cookie", AsyncMock(return_value="browser-session")),
        patch("app.socket_notification_manager", MagicMock()),
    ):
        assert await app_module.handle_connect("sid-c3", environ={}, auth={"embedScope": True}) is True

    db.commit.assert_not_awaited()
    create_session = sio.app_instance.state.socket_session_service.create_session
    assert create_session.await_args.kwargs["embedded_sub_agent_id"] is None
