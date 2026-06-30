"""Unit tests for delivery_channel_router after the group_ids removal.

Visibility is no longer group-scoped (human console users see all channels) and
write-access is restricted to the owning A2A client or system admins (the former
group-manager path is gone). These tests drive the endpoint functions directly with
the injected dependencies (get_client_id_from_request / is_admin_mode) monkeypatched.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

import console_backend.routers.delivery_channel_router as router


def _request_with_repo(repo) -> MagicMock:
    req = MagicMock()
    req.app.state.delivery_channel_repository = repo
    return req


@pytest.mark.asyncio
async def test_human_user_sees_all_channels(monkeypatch):
    """A session-authenticated user (no client_id) is routed to list_all_channels."""
    repo = SimpleNamespace(
        list_all_channels=AsyncMock(return_value=[]),
        list_channels_for_client=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(router, "get_client_id_from_request", AsyncMock(return_value=None))

    await router.list_channels(
        request=_request_with_repo(repo), db=MagicMock(), current_user=MagicMock()
    )

    repo.list_all_channels.assert_awaited_once()
    repo.list_channels_for_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_a2a_client_sees_only_own_channels(monkeypatch):
    """A Bearer-token client (azp present) is scoped to its own channels."""
    repo = SimpleNamespace(
        list_all_channels=AsyncMock(return_value=[]),
        list_channels_for_client=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(router, "get_client_id_from_request", AsyncMock(return_value="client-a"))

    await router.list_channels(
        request=_request_with_repo(repo), db=MagicMock(), current_user=MagicMock()
    )

    repo.list_channels_for_client.assert_awaited_once()
    repo.list_all_channels.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_list_scopes_to_forwarded_installation(monkeypatch):
    """console_list_delivery_channels filters by the installation from request context."""
    repo = SimpleNamespace(
        list_channels_for_installation=AsyncMock(return_value=[]),
        list_all_channels=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(router, "forwarded_installation", MagicMock(return_value="acme"))

    await router.list_delivery_channels_mcp(
        request=_request_with_repo(repo), db=MagicMock(), _current_user=MagicMock()
    )

    repo.list_channels_for_installation.assert_awaited_once()
    assert repo.list_channels_for_installation.call_args.kwargs["installation_id"] == "acme"
    repo.list_all_channels.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_list_without_installation_returns_all(monkeypatch):
    """No installation in context (e.g. web-console) → all channels (accepted trade-off)."""
    repo = SimpleNamespace(
        list_channels_for_installation=AsyncMock(return_value=[]),
        list_all_channels=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(router, "forwarded_installation", MagicMock(return_value=None))

    await router.list_delivery_channels_mcp(
        request=_request_with_repo(repo), db=MagicMock(), _current_user=MagicMock()
    )

    repo.list_all_channels.assert_awaited_once()
    repo.list_channels_for_installation.assert_not_awaited()


@pytest.mark.asyncio
async def test_owning_client_has_write_access(monkeypatch):
    """The client whose azp matches the channel owner may modify it."""
    monkeypatch.setattr(router, "get_client_id_from_request", AsyncMock(return_value="client-a"))
    monkeypatch.setattr(router, "is_admin_mode", MagicMock(return_value=False))

    # No exception == access granted.
    await router._require_channel_write_access(
        request=MagicMock(), owner_client_id="client-a", current_user=MagicMock()
    )


@pytest.mark.asyncio
async def test_admin_has_write_access(monkeypatch):
    """A system admin (admin mode) may modify any channel even without a matching azp."""
    monkeypatch.setattr(router, "get_client_id_from_request", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "is_admin_mode", MagicMock(return_value=True))

    await router._require_channel_write_access(
        request=MagicMock(), owner_client_id="client-a", current_user=MagicMock()
    )


@pytest.mark.asyncio
async def test_non_owner_non_admin_is_forbidden(monkeypatch):
    """A different client that is not an admin is rejected with 403 (no group-manager path)."""
    monkeypatch.setattr(router, "get_client_id_from_request", AsyncMock(return_value="other-client"))
    monkeypatch.setattr(router, "is_admin_mode", MagicMock(return_value=False))

    with pytest.raises(HTTPException) as exc:
        await router._require_channel_write_access(
            request=MagicMock(), owner_client_id="client-a", current_user=MagicMock()
        )
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.asyncio
async def test_session_user_without_admin_is_forbidden(monkeypatch):
    """A plain human user (no client_id, not admin) cannot modify a channel."""
    monkeypatch.setattr(router, "get_client_id_from_request", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "is_admin_mode", MagicMock(return_value=False))

    with pytest.raises(HTTPException) as exc:
        await router._require_channel_write_access(
            request=MagicMock(), owner_client_id="client-a", current_user=MagicMock()
        )
    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
