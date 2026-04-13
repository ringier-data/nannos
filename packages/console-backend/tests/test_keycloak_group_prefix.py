"""Test Keycloak group name prefix functionality."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from playground_backend.services.keycloak_admin_service import KeycloakAdminService


@pytest.mark.asyncio
async def test_prefix_isolation_different_environments():
    """Test that different environments create different group names."""
    with patch("playground_backend.services.keycloak_admin_service.KeycloakAdmin") as mock_admin_class:
        mock_admin = Mock()
        mock_admin_class.return_value = mock_admin
        mock_admin.a_create_group = AsyncMock(return_value="kc-group-id-123")

        # Local creates "local-sales"
        service_local = KeycloakAdminService(
            issuer="https://login.p.nannos.rcplus.io/realms/nannos",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="agent-console",
            group_name_prefix="local-",
        )
        await service_local.create_group("sales")

        # Dev creates "dev-sales"
        service_dev = KeycloakAdminService(
            issuer="https://login.p.nannos.rcplus.io/realms/nannos",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="agent-console",
            group_name_prefix="dev-",
        )
        await service_dev.create_group("sales")

        # Stg creates "stg-sales"
        service_stg = KeycloakAdminService(
            issuer="https://login.p.nannos.rcplus.io/realms/nannos",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="agent-console",
            group_name_prefix="stg-",
        )
        await service_stg.create_group("sales")

        # Prod creates "sales"
        service_prod = KeycloakAdminService(
            issuer="https://login.p.nannos.rcplus.io/realms/nannos",
            admin_client_id="nannos-admin",
            admin_client_secret="secret",
            oidc_client_id="agent-console",
            group_name_prefix="",
        )
        await service_prod.create_group("sales")

        # Verify all four created different group names
        calls = mock_admin.a_create_group.call_args_list
        assert len(calls) == 4
        assert calls[0][0][0]["name"] == "local-sales"
        assert calls[1][0][0]["name"] == "dev-sales"
        assert calls[2][0][0]["name"] == "stg-sales"
        assert calls[3][0][0]["name"] == "sales"
