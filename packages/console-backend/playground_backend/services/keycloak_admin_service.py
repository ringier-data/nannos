"""Keycloak Admin API service for group synchronization.

This service provides one-way synchronization from agent-console backend to Keycloak.
Groups and memberships created in the backend are automatically reflected in Keycloak,
enabling the MCP Gateway and other systems to trust the 'groups' claim in JWT tokens.

Required Keycloak Configuration:
    1. Service account with realm management roles:
       - manage-users (to add/remove users from groups)
       - manage-groups (to create/update/delete groups)
       - manage-clients (to configure group mapper on OIDC client)

    2. Client credentials grant enabled on admin client

    3. OIDC client must have Group Membership mapper configured:
       - Token Claim Name: groups
       - Full group path: OFF (flat structure)
       - Add to ID token: ON
       - Add to access token: ON
       - Add to userinfo: ON

Usage:
    service = KeycloakAdminService(
        issuer=os.environ["OIDC_ISSUER"],  # e.g., "https://login.p.nannos.rcplus.io/realms/nannos"
        admin_client_id="nannos-admin",
        admin_client_secret="secret",
        oidc_client_id="agent-console"
    )

    # Automatically configure group mapper on first call
    await service.ensure_group_mapper_configured()

    # Sync group operations
    group_id = await service.create_group("Engineering", "Engineering team")
    await service.add_user_to_group("user-sub-123", group_id)
"""

import logging
from typing import Any
from urllib.parse import urlparse

from keycloak import KeycloakAdmin, KeycloakError

logger = logging.getLogger(__name__)


class KeycloakSyncError(Exception):
    """Raised when Keycloak synchronization fails."""

    pass


class KeycloakAdminService:
    """Service for synchronizing groups with Keycloak using Admin API.

    This service implements one-way sync (Backend → Keycloak) to ensure
    that group memberships in Keycloak always reflect the playground backend state.
    Other systems can then trust the 'groups' claim in JWT tokens for authorization.
    """

    def __init__(
        self,
        issuer: str,
        admin_client_id: str,
        admin_client_secret: str,
        oidc_client_id: str,
        group_name_prefix: str,
    ):
        """Initialize Keycloak Admin client.

        Args:
            issuer: OIDC issuer URL (e.g., https://login.p.nannos.rcplus.io/realms/nannos)
            admin_client_id: Client ID with admin permissions (e.g., nannos-admin)
            admin_client_secret: Client secret for admin client
            oidc_client_id: OIDC client ID to configure group mapper on (e.g., agent-console)
            group_name_prefix: Prefix for group names (e.g., "dev-", "stg-", "prod-")
        """
        self.issuer = issuer
        self.oidc_client_id = oidc_client_id
        self.group_name_prefix = group_name_prefix
        self.realm = self._extract_realm_from_issuer(issuer)
        self.server_url = self._extract_server_url_from_issuer(issuer)

        try:
            self.admin = KeycloakAdmin(
                server_url=self.server_url,
                realm_name=self.realm,
                client_id=admin_client_id,
                client_secret_key=admin_client_secret,
                verify=True,
            )
            logger.info(f"Initialized Keycloak Admin client for realm: {self.realm}")
        except Exception as e:
            logger.error(f"Failed to initialize Keycloak Admin client: {e}")
            raise KeycloakSyncError(f"Failed to initialize Keycloak Admin client: {e}") from e

    def _extract_realm_from_issuer(self, issuer_url: str) -> str:
        """Extract realm name from OIDC issuer URL.

        Example: https://login.p.nannos.rcplus.io/realms/nannos → nannos
        """
        parsed = urlparse(issuer_url)
        path_parts = parsed.path.split("/")

        try:
            realm_index = path_parts.index("realms")
            if realm_index + 1 < len(path_parts):
                return path_parts[realm_index + 1]
        except ValueError:
            pass

        raise ValueError(f"Invalid OIDC issuer URL format (missing /realms/<name>): {issuer_url}")

    def _extract_server_url_from_issuer(self, issuer_url: str) -> str:
        """Extract Keycloak server base URL from issuer.

        Example: https://login.p.nannos.rcplus.io/realms/nannos → https://login.p.nannos.rcplus.io
        """
        parsed = urlparse(issuer_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _prefixed_name(self, name: str) -> str:
        """Apply environment prefix to group name if configured.

        Examples:
            - dev env: "marketing" → "dev-marketing"
            - stg env: "marketing" → "stg-marketing"
            - prod env: "marketing" → "prod-marketing"
        """
        return f"{self.group_name_prefix}{name}" if self.group_name_prefix else name

    async def _configure_group_mapper_for_client(self, client_id_str: str) -> None:
        """Configure group mapper for a specific client.

        Args:
            client_id_str: The clientId string (e.g., "agent-console", "gatana")

        Raises:
            KeycloakSyncError: If mapper configuration fails
        """
        try:
            # Find the client
            clients = await self.admin.a_get_clients()
            client = next((c for c in clients if c.get("clientId") == client_id_str), None)

            if not client:
                logger.warning(f"Client '{client_id_str}' not found in realm '{self.realm}'")
                return

            client_id = client["id"]

            # Check if group mapper already exists
            mappers = await self.admin.a_get_mappers_from_client(client_id)
            group_mapper = next(
                (m for m in mappers if m.get("protocol") == "openid-connect" and m.get("name") == "groups"), None
            )

            if group_mapper:
                logger.info(f"Group mapper already configured on client '{client_id_str}'")
                return

            # Create group membership mapper
            mapper_config = {
                "name": "groups",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-group-membership-mapper",
                "config": {
                    "full.path": "false",  # Flat structure (group names only)
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "lightweight.claim": "true",  # CRITICAL: Include in lightweight tokens (token exchange)
                    "userinfo.token.claim": "true",
                    "claim.name": "groups",
                },
            }

            await self.admin.a_add_mapper_to_client(client_id, mapper_config)
            logger.info(f"Successfully configured group mapper on client '{client_id_str}'")

        except KeycloakError as e:
            logger.error(f"Failed to configure group mapper on '{client_id_str}': {e}")
            raise KeycloakSyncError(f"Failed to configure group mapper on '{client_id_str}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error configuring group mapper on '{client_id_str}': {e}")
            raise KeycloakSyncError(f"Unexpected error configuring group mapper on '{client_id_str}': {e}") from e

    async def ensure_group_mapper_configured(self) -> None:
        """Ensure group mapper is configured on all relevant clients.

        Configures the Group Membership mapper on:
        1. The OIDC client (agent-console) - for user login tokens
        2. The gatana client - for token exchange (critical!)
        3. The orchestrator client - for token exchange
        4. The agent-creator client - for token exchange

        Token exchange copies claims from target client mappers, not source client.
        So all clients that receive tokens (via login or exchange) need the mapper.

        Raises:
            KeycloakSyncError: If mapper configuration fails
        """
        # List of clients that need group mapper
        clients_to_configure = [
            self.oidc_client_id,  # Web client (user login)
            "gatana",  # MCP gateway (token exchange target)
            "orchestrator",  # Orchestrator (token exchange target)
            "agent-creator",  # Agent creator (token exchange target)
        ]

        for client_id in clients_to_configure:
            try:
                await self._configure_group_mapper_for_client(client_id)
            except Exception as e:
                # Log but don't fail if a client doesn't exist
                logger.warning(f"Failed to configure group mapper for '{client_id}': {e}")

    async def create_group(self, name: str, description: str | None = None) -> str:
        """Create a group in Keycloak.

        Args:
            name: Group name (will be prefixed with environment prefix if configured)
            description: Optional group description

        Returns:
            Keycloak group ID

        Raises:
            KeycloakSyncError: If group creation fails
        """
        try:
            prefixed_name = self._prefixed_name(name)
            group_payload: dict[str, Any] = {"name": prefixed_name, "description": description}

            # Create group and get ID from location header
            group_id = await self.admin.a_create_group(group_payload, parent=None, skip_exists=False)
            if not group_id:
                # should not happen, since skip_exists=False should imply that create_group raises an error when a group
                # already exists
                raise KeycloakSyncError(f"Failed to create Keycloak group '{prefixed_name}': Group already exists")
            logger.info(f"Created Keycloak group: {prefixed_name} (ID: {group_id})")
            return group_id

        except KeycloakError as e:
            logger.error(f"Failed to create Keycloak group '{name}': {e}")
            raise KeycloakSyncError(f"Failed to create Keycloak group '{name}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error creating Keycloak group '{name}': {e}")
            raise KeycloakSyncError(f"Unexpected error creating Keycloak group '{name}': {e}") from e

    async def update_group(self, keycloak_group_id: str, name: str, description: str | None = None) -> None:
        """Update a group in Keycloak.

        Args:
            keycloak_group_id: Keycloak group ID
            name: New group name (will be prefixed with environment prefix if configured)
            description: New group description (or None to clear)

        Raises:
            KeycloakSyncError: If group update fails
        """
        try:
            prefixed_name = self._prefixed_name(name)
            group_payload: dict[str, Any] = {"name": prefixed_name, "description": description}
            await self.admin.a_update_group(keycloak_group_id, group_payload)
            logger.info(f"Updated Keycloak group: {keycloak_group_id}")

        except KeycloakError as e:
            logger.error(f"Failed to update Keycloak group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(f"Failed to update Keycloak group '{keycloak_group_id}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error updating Keycloak group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(f"Unexpected error updating Keycloak group '{keycloak_group_id}': {e}") from e

    async def delete_group(self, keycloak_group_id: str) -> None:
        """Delete a group from Keycloak.

        Args:
            keycloak_group_id: Keycloak group ID

        Raises:
            KeycloakSyncError: If group deletion fails
        """
        try:
            await self.admin.a_delete_group(keycloak_group_id)
            logger.info(f"Deleted Keycloak group: {keycloak_group_id}")

        except KeycloakError as e:
            logger.error(f"Failed to delete Keycloak group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(f"Failed to delete Keycloak group '{keycloak_group_id}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error deleting Keycloak group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(f"Unexpected error deleting Keycloak group '{keycloak_group_id}': {e}") from e

    async def add_user_to_group(self, user_sub: str, keycloak_group_id: str) -> None:
        """Add a user to a Keycloak group.

        Args:
            user_sub: User's OIDC subject identifier (Keycloak user ID)
            keycloak_group_id: Keycloak group ID

        Raises:
            KeycloakSyncError: If adding user to group fails
        """
        try:
            await self.admin.a_group_user_add(user_sub, keycloak_group_id)
            logger.info(f"Added user {user_sub} to Keycloak group {keycloak_group_id}")

        except KeycloakError as e:
            logger.error(f"Failed to add user '{user_sub}' to group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(f"Failed to add user '{user_sub}' to group '{keycloak_group_id}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error adding user '{user_sub}' to group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(
                f"Unexpected error adding user '{user_sub}' to group '{keycloak_group_id}': {e}"
            ) from e

    async def remove_user_from_group(self, user_sub: str, keycloak_group_id: str) -> None:
        """Remove a user from a Keycloak group.

        Args:
            user_sub: User's OIDC subject identifier (Keycloak user ID)
            keycloak_group_id: Keycloak group ID

        Raises:
            KeycloakSyncError: If removing user from group fails
        """
        try:
            await self.admin.a_group_user_remove(user_sub, keycloak_group_id)
            logger.info(f"Removed user {user_sub} from Keycloak group {keycloak_group_id}")

        except KeycloakError as e:
            logger.error(f"Failed to remove user '{user_sub}' from group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(f"Failed to remove user '{user_sub}' from group '{keycloak_group_id}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error removing user '{user_sub}' from group '{keycloak_group_id}': {e}")
            raise KeycloakSyncError(
                f"Unexpected error removing user '{user_sub}' from group '{keycloak_group_id}': {e}"
            ) from e

    async def update_user_attribute(self, user_sub: str, attribute_name: str, attribute_value: str | None) -> None:
        """Update a user attribute in Keycloak.

        Args:
            user_sub: User's OIDC subject identifier (Keycloak user ID)
            attribute_name: Name of the attribute (e.g., "phoneNumberOverride", "phoneNumber")
            attribute_value: Value to set. If None, clears the attribute.

        Raises:
            KeycloakSyncError: If user update fails
        """
        try:
            # Keycloak PUT /users/{id} replaces the ENTIRE user representation.
            # We must fetch the current user first, merge the attribute, then PUT
            # the full representation — otherwise required fields like email are lost.
            # open issue https://github.com/keycloak/keycloak/issues/19691 to implement PATCH semantics.
            user_repr = await self.admin.a_get_user(user_sub)
            existing_attrs = user_repr.get("attributes", {})
            existing_attrs[attribute_name] = [attribute_value] if attribute_value else []
            user_repr["attributes"] = existing_attrs

            await self.admin.a_update_user(user_sub, user_repr)
            logger.info(f"Updated user {user_sub} attribute '{attribute_name}' = {attribute_value}")

        except KeycloakError as e:
            logger.error(f"Failed to update user '{user_sub}' attribute '{attribute_name}': {e}")
            raise KeycloakSyncError(f"Failed to update user '{user_sub}' attribute '{attribute_name}': {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error updating user '{user_sub}' attribute '{attribute_name}': {e}")
            raise KeycloakSyncError(
                f"Unexpected error updating user '{user_sub}' attribute '{attribute_name}': {e}"
            ) from e

    async def sync_phone_number_override(self, user_sub: str, phone_override: str | None) -> None:
        """Sync phone number override to Keycloak user attribute.

        Called when a user creates/updates/deletes a phone override in the backend.
        Ensures the 'phoneNumberOverride' attribute is synchronized.

        Args:
            user_sub: User's OIDC subject identifier (Keycloak user ID)
            phone_override: Phone override value, or None to clear

        Raises:
            KeycloakSyncError: If synchronization fails
        """
        await self.update_user_attribute(user_sub, "phoneNumberOverride", phone_override)
