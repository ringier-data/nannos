"""Embed scope (ADR-0006): the token's azp binding — made at connect and stamped on the
socket session — decides the execute-only target and the conversation scope. The client's
own claims are never trusted for either."""

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from console_backend.models.socket_session import SocketSession

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _session(bound: int | None) -> SocketSession:
    return SocketSession(
        socket_id="socket:s1",
        user_id="user-1",
        http_session_id="h1",
        embedded_sub_agent_id=bound,
    )


def test_apply_embed_scope_bound_connection_overrides_client_claim():
    import app as app_module

    metadata = {
        "executeOnlySubAgentId": 99,
        "subAgentId": 98,
        "pageContext": {"campaignId": 1},
    }
    assert app_module._apply_embed_scope(metadata, _session(20)) == 20
    assert metadata["executeOnlySubAgentId"] == 20
    assert "subAgentId" not in metadata
    assert metadata["pageContext"] == {"campaignId": 1}  # everything else untouched


def test_apply_embed_scope_unbound_connection_drops_client_claim():
    import app as app_module

    metadata = {"executeOnlySubAgentId": 99, "subAgentId": 98}
    assert app_module._apply_embed_scope(metadata, _session(None)) is None
    assert "executeOnlySubAgentId" not in metadata and "subAgentId" not in metadata


def _token_path_mocks(*, azp: str | None, bind_result, existing_user=True):
    claims = {"sub": "sub-1", "email": "e@x", "exp": time.time() + 600}
    if azp is not None:
        claims["azp"] = azp
    validator = MagicMock()
    validator.validate = AsyncMock(return_value=claims)

    db = MagicMock()
    db.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)

    user = SimpleNamespace(id="user-1")
    sio = MagicMock()
    sio.app_instance.state.user_service.get_user_by_sub = AsyncMock(
        return_value=user if existing_user else None
    )
    sio.app_instance.state.user_service.upsert_user = AsyncMock(return_value=user)
    sio.app_instance.state.embed_binding_service.bind_connection = bind_result
    sio.app_instance.state.session_service.create_session = AsyncMock(
        return_value="stored-1"
    )
    return validator, factory, db, user, sio


@pytest.mark.asyncio
async def test_token_path_binds_azp_and_activates_user():
    import app as app_module

    bind = AsyncMock(return_value=20)
    validator, factory, db, user, sio = _token_path_mocks(
        azp="nannos-embedded", bind_result=bind
    )
    with (
        patch(
            "console_backend.utils.jwt_validators.get_jwt_validator",
            return_value=validator,
        ),
        patch("app.get_async_session_factory", return_value=factory),
        patch("app.sio", sio),
    ):
        result = await app_module._resolve_socket_user_via_token("a.jwt")

    assert result == app_module._SocketTokenAuth(
        http_session_id="stored-1", embedded_sub_agent_id=20
    )
    bind.assert_awaited_once()
    assert bind.await_args.kwargs == {"user": user, "azp": "nannos-embedded"}
    db.commit.assert_awaited()  # the activation row must land


@pytest.mark.asyncio
async def test_token_path_unbound_azp_connects_unbound():
    import app as app_module

    bind = AsyncMock(return_value=None)
    validator, factory, db, _, sio = _token_path_mocks(
        azp="agent-console", bind_result=bind
    )
    with (
        patch(
            "console_backend.utils.jwt_validators.get_jwt_validator",
            return_value=validator,
        ),
        patch("app.get_async_session_factory", return_value=factory),
        patch("app.sio", sio),
    ):
        result = await app_module._resolve_socket_user_via_token("a.jwt")

    assert result == app_module._SocketTokenAuth(
        http_session_id="stored-1", embedded_sub_agent_id=None
    )
    db.commit.assert_not_awaited()  # nothing to persist for an existing, unbound user


@pytest.mark.asyncio
async def test_token_path_binding_failure_does_not_reject_the_login():
    import app as app_module

    bind = AsyncMock(side_effect=RuntimeError("db hiccup"))
    validator, factory, _, _, sio = _token_path_mocks(
        azp="nannos-embedded", bind_result=bind
    )
    with (
        patch(
            "console_backend.utils.jwt_validators.get_jwt_validator",
            return_value=validator,
        ),
        patch("app.get_async_session_factory", return_value=factory),
        patch("app.sio", sio),
    ):
        result = await app_module._resolve_socket_user_via_token("a.jwt")

    assert result is not None and result.embedded_sub_agent_id is None
    sio.app_instance.state.session_service.create_session.assert_awaited_once()


def _conv(conversation_id: str, metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=conversation_id,
        user_id="user-1",
        started_at=NOW,
        last_message_at=NOW,
        status="active",
        metadata=metadata,
        title=conversation_id,
        agent_url=None,
        sub_agent_config_hash=None,
    )


async def _list_conversations(bound: int | None, query_param: str | None):
    from console_backend.routers import conversation_router as cr

    conversations = [
        _conv("mine", {"embedded_sub_agent_id": "20"}),
        _conv("console", {}),
        _conv("other-app", {"embedded_sub_agent_id": "7"}),
    ]
    request = MagicMock()
    request.app.state.conversation_service.get_conversations_by_user_id = AsyncMock(
        return_value=conversations
    )
    request.app.state.embed_binding_service.sub_agent_id_for_azp = AsyncMock(
        return_value=bound
    )
    with patch.object(
        cr, "get_client_id_from_request", AsyncMock(return_value="some-client")
    ):
        out = await cr.get_conversations_by_user(
            request,
            user_id=None,
            limit=20,
            sub_agent_config_hash=None,
            exclude_playground=False,
            embedded_sub_agent_id=query_param,
            search=None,
            user=SimpleNamespace(id="user-1"),
        )
    return [c["conversation_id"] for c in out["conversations"]]


@pytest.mark.asyncio
async def test_conversation_list_scope_comes_from_the_bound_token():
    # A bound token sees only its own app's conversations, whatever the query string claims.
    assert await _list_conversations(bound=20, query_param="7") == ["mine"]
    assert await _list_conversations(bound=20, query_param=None) == ["mine"]


@pytest.mark.asyncio
async def test_conversation_list_unbound_token_keeps_the_query_parameter():
    assert await _list_conversations(bound=None, query_param="7") == ["other-app"]
    assert await _list_conversations(bound=None, query_param=None) == [
        "mine",
        "console",
        "other-app",
    ]


# --------------------------------------------------- the handshake's agent label


def _label_mocks(binding, sub_agent=None):
    db = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    sio = MagicMock()
    sio.app_instance.state.embed_binding_service.get_binding = AsyncMock(
        return_value=binding
    )
    sio.app_instance.state.sub_agent_service.get_sub_agent_by_id = AsyncMock(
        return_value=sub_agent
    )
    return factory, sio


async def _label_for(session, binding, sub_agent=None):
    import app as app_module

    factory, sio = _label_mocks(binding, sub_agent)
    with (
        patch("app.get_async_session_factory", return_value=factory),
        patch("app.sio", sio),
    ):
        return await app_module._embedded_agent_info(session)


@pytest.mark.asyncio
async def test_handshake_names_the_bound_agent_as_its_host_published_it():
    """The A2A card in the same handshake names the ORCHESTRATOR; a host page must not
    label its assistant with that, and must not have to guess from its own origin."""
    binding = SimpleNamespace(
        revision="abc123def4567890",
        agent=SimpleNamespace(
            name="Alloy AI Assistant",
            description="Helps with campaigns.",
            organization="Ringier Advertising",
        ),
    )
    assert await _label_for(_session(20), binding) == {
        "subAgentId": "20",
        "name": "Alloy AI Assistant",
        "description": "Helps with campaigns.",
        "organization": "Ringier Advertising",
        "revision": "abc123def4567890",
    }


@pytest.mark.asyncio
async def test_handshake_omits_the_label_for_console_and_unbound_connections():
    assert await _label_for(_session(None), None) is None


@pytest.mark.asyncio
async def test_handshake_falls_back_to_the_row_name_before_the_first_sync():
    """Bound but never synced (an unreachable host): the derived row name is
    hyphenated, but it still beats labelling the page 'Orchestrator Agent'."""
    binding = SimpleNamespace(revision=None, agent=None)
    row = SimpleNamespace(name="Alloy-AI-Assistant")
    assert await _label_for(_session(20), binding, row) == {
        "subAgentId": "20",
        "name": "Alloy-AI-Assistant",
    }


@pytest.mark.asyncio
async def test_handshake_label_failure_never_breaks_the_handshake():
    import app as app_module

    factory, sio = _label_mocks(None)
    sio.app_instance.state.embed_binding_service.get_binding = AsyncMock(
        side_effect=RuntimeError("database is down")
    )
    with (
        patch("app.get_async_session_factory", return_value=factory),
        patch("app.sio", sio),
    ):
        assert await app_module._embedded_agent_info(_session(20)) is None
