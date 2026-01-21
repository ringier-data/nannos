"""Tests for KeycloakAdminService."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from keycloak import KeycloakError

from playground_backend.services.keycloak_admin_service import (
    KeycloakAdminService,
    KeycloakSyncError,
)


@pytest.fixture
def mock_keycloak_admin():
    """Create a mocked KeycloakAdmin instance."""
    with patch("playground_backend.services.keycloak_admin_service.KeycloakAdmin") as mock_kc:
        mock_instance = Mock()
        mock_instance.a_add_mapper_to_client = AsyncMock()
        mock_kc.return_value = mock_instance
        yield mock_instance


class TestKeycloakAdminServiceInit:
    """Test KeycloakAdminService initialization."""

    def test_init_with_valid_issuer(self, mock_keycloak_admin):
        """Test service initializes correctly with valid issuer URL."""
        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        assert service.realm == "a2a"
        assert service.server_url == "https://login.alloy.ch"
        assert service.oidc_client_id == "web-client"

    def test_init_with_invalid_issuer(self, mock_keycloak_admin):
        """Test service raises error with invalid issuer format."""
        with pytest.raises(ValueError, match="Invalid OIDC issuer URL format"):
            KeycloakAdminService(
                issuer="https://login.alloy.ch/invalid",
                admin_client_id="nannos-admin",
                admin_client_secret="secret",
                oidc_client_id="web-client",
                group_name_prefix="",
            )

    def test_init_with_keycloak_connection_error(self):
        """Test service raises KeycloakSyncError on connection failure."""
        with patch("playground_backend.services.keycloak_admin_service.KeycloakAdmin") as mock_kc:
            mock_kc.side_effect = Exception("Connection failed")

            with pytest.raises(KeycloakSyncError, match="Failed to initialize Keycloak Admin client"):
                KeycloakAdminService(
                    issuer="https://login.alloy.ch/realms/a2a",
                    admin_client_id="nannos-admin",
                    admin_client_secret="secret",
                    oidc_client_id="web-client",
                    group_name_prefix="",
                )


class TestEnsureGroupMapperConfigured:
    """Test ensure_group_mapper_configured method."""

    @pytest.mark.asyncio
    async def test_creates_mapper_when_missing(self, mock_keycloak_admin):
        """Test creates group mapper when it doesn't exist."""
        # Mock client lookup
        mock_keycloak_admin.a_get_clients = AsyncMock(return_value=[{"id": "client-id-123", "clientId": "web-client"}])
        # Mock mapper lookup (empty list = no existing mapper)
        mock_keycloak_admin.a_get_mappers_from_client = AsyncMock(return_value=[])

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        await service.ensure_group_mapper_configured()

        # Verify mapper was added
        mock_keycloak_admin.a_add_mapper_to_client.assert_called_once()
        call_args = mock_keycloak_admin.a_add_mapper_to_client.call_args
        assert call_args[0][0] == "client-id-123"
        mapper_config = call_args[0][1]
        assert mapper_config["name"] == "groups"
        assert mapper_config["protocolMapper"] == "oidc-group-membership-mapper"
        assert mapper_config["config"]["full.path"] == "false"

    @pytest.mark.asyncio
    async def test_skips_creation_when_mapper_exists(self, mock_keycloak_admin):
        """Test skips mapper creation when it already exists."""
        # Mock client and existing mapper
        mock_keycloak_admin.a_get_clients = AsyncMock(return_value=[{"id": "client-id-123", "clientId": "web-client"}])
        mock_keycloak_admin.a_get_mappers_from_client = AsyncMock(
            return_value=[{"protocol": "openid-connect", "name": "groups"}]
        )

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        await service.ensure_group_mapper_configured()

        # Verify mapper was NOT added
        mock_keycloak_admin.a_add_mapper_to_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_client_not_found(self, mock_keycloak_admin):
        """Test handles case when OIDC client doesn't exist."""
        # Mock client lookup with no matching client
        mock_keycloak_admin.a_get_clients = AsyncMock(return_value=[{"id": "other-id", "clientId": "other-client"}])

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        # Should not raise error, just log warning
        await service.ensure_group_mapper_configured()

        mock_keycloak_admin.a_add_mapper_to_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_warns_on_keycloak_error(self, mock_keycloak_admin, caplog):
        """Test warning when client cannot be configured due to Keycloak error."""
        mock_keycloak_admin.a_get_clients.side_effect = KeycloakError("Permission denied", response_code=403)

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        with caplog.at_level("WARNING"):
            await service.ensure_group_mapper_configured()
            assert "Failed to configure group mapper on" in caplog.text


class TestCreateGroup:
    """Test create_group method."""

    @pytest.mark.asyncio
    async def test_creates_group_successfully(self, mock_keycloak_admin):
        """Test creates group and returns group ID."""
        mock_keycloak_admin.a_create_group = AsyncMock(return_value="kc-group-id-123")

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        group_id = await service.create_group("Engineering", "Engineering team")

        assert group_id == "kc-group-id-123"
        mock_keycloak_admin.a_create_group.assert_called_once()
        call_args = mock_keycloak_admin.a_create_group.call_args[0][0]
        assert call_args["name"] == "Engineering"
        assert call_args["description"] == "Engineering team"

    @pytest.mark.asyncio
    async def test_creates_group_without_description(self, mock_keycloak_admin):
        """Test creates group without description."""
        mock_keycloak_admin.a_create_group = AsyncMock(return_value="kc-group-id-456")

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        group_id = await service.create_group("Marketing")

        assert group_id == "kc-group-id-456"
        call_args = mock_keycloak_admin.a_create_group.call_args[0][0]
        assert call_args["name"] == "Marketing"
        assert call_args["description"] is None

    @pytest.mark.asyncio
    async def test_raises_on_keycloak_error(self, mock_keycloak_admin):
        """Test raises KeycloakSyncError on Keycloak API errors."""
        mock_keycloak_admin.a_create_group.side_effect = KeycloakError("Group already exists", response_code=409)

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        with pytest.raises(KeycloakSyncError, match="Failed to create Keycloak group"):
            await service.create_group("DuplicateGroup")


class TestUpdateGroup:
    """Test update_group method."""

    @pytest.mark.asyncio
    async def test_updates_group_successfully(self, mock_keycloak_admin):
        """Test updates group name and description."""
        mock_keycloak_admin.a_update_group = AsyncMock()
        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        await service.update_group("kc-group-id-123", "New Name", "New Description")

        mock_keycloak_admin.a_update_group.assert_called_once()
        call_args = mock_keycloak_admin.a_update_group.call_args[0]
        assert call_args[0] == "kc-group-id-123"
        assert call_args[1]["name"] == "New Name"
        assert call_args[1]["description"] == "New Description"

    @pytest.mark.asyncio
    async def test_clears_description_when_none(self, mock_keycloak_admin):
        """Test clears description when None is provided."""
        mock_keycloak_admin.a_update_group = AsyncMock()
        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        await service.update_group("kc-group-id-123", "Name Only", None)

        call_args = mock_keycloak_admin.a_update_group.call_args[0][1]
        assert call_args["description"] is None

    @pytest.mark.asyncio
    async def test_raises_on_keycloak_error(self, mock_keycloak_admin):
        """Test raises KeycloakSyncError on Keycloak API errors."""
        mock_keycloak_admin.a_update_group.side_effect = KeycloakError("Group not found", response_code=404)

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        with pytest.raises(KeycloakSyncError, match="Failed to update Keycloak group"):
            await service.update_group("invalid-id", "Name")


class TestDeleteGroup:
    """Test delete_group method."""

    @pytest.mark.asyncio
    async def test_deletes_group_successfully(self, mock_keycloak_admin):
        """Test deletes group from Keycloak."""
        mock_keycloak_admin.a_delete_group = AsyncMock()
        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        await service.delete_group("kc-group-id-123")

        mock_keycloak_admin.a_delete_group.assert_called_once_with("kc-group-id-123")

    @pytest.mark.asyncio
    async def test_raises_on_keycloak_error(self, mock_keycloak_admin):
        """Test raises KeycloakSyncError on Keycloak API errors."""
        mock_keycloak_admin.a_delete_group.side_effect = KeycloakError("Group not found", response_code=404)

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        with pytest.raises(KeycloakSyncError, match="Failed to delete Keycloak group"):
            await service.delete_group("invalid-id")


class TestAddUserToGroup:
    """Test add_user_to_group method."""

    @pytest.mark.asyncio
    async def test_adds_user_successfully(self, mock_keycloak_admin):
        """Test adds user to group."""
        mock_keycloak_admin.a_group_user_add = AsyncMock()
        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        await service.add_user_to_group("user-sub-123", "kc-group-id-456")

        mock_keycloak_admin.a_group_user_add.assert_called_once_with("user-sub-123", "kc-group-id-456")

    @pytest.mark.asyncio
    async def test_raises_on_keycloak_error(self, mock_keycloak_admin):
        """Test raises KeycloakSyncError on Keycloak API errors."""
        mock_keycloak_admin.a_group_user_add.side_effect = KeycloakError("User not found", response_code=404)

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        with pytest.raises(KeycloakSyncError, match="Failed to add user"):
            await service.add_user_to_group("invalid-user", "group-id")


class TestRemoveUserFromGroup:
    """Test remove_user_from_group method."""

    @pytest.mark.asyncio
    async def test_removes_user_successfully(self, mock_keycloak_admin):
        """Test removes user from group."""
        mock_keycloak_admin.a_group_user_remove = AsyncMock()
        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        await service.remove_user_from_group("user-sub-123", "kc-group-id-456")

        mock_keycloak_admin.a_group_user_remove.assert_called_once_with("user-sub-123", "kc-group-id-456")

    @pytest.mark.asyncio
    async def test_raises_on_keycloak_error(self, mock_keycloak_admin):
        """Test raises KeycloakSyncError on Keycloak API errors."""
        mock_keycloak_admin.a_group_user_remove.side_effect = KeycloakError("User not in group", response_code=404)

        service = KeycloakAdminService(
            issuer="https://login.alloy.ch/realms/a2a",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="web-client",
            group_name_prefix="",
        )

        with pytest.raises(KeycloakSyncError, match="Failed to remove user"):
            await service.remove_user_from_group("user-sub-123", "group-id")
