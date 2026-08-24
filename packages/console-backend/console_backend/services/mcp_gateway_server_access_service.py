"""MCP Gateway server access management service.

Manages server access permissions in the MCP gateway (Gatana) for groups
that are synced via outbound SCIM. A group is "managed" if it has a SCIM
remote_id for an outbound endpoint flagged as the MCP gateway
(outbound_scim_endpoints.is_mcp_gateway).
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import config
from ..models.mcp_gateway_server_access import McpGatewayServerPermission

logger = logging.getLogger(__name__)


class McpGatewayServerAccessService:
    """Manages MCP gateway server access permissions for groups."""

    def __init__(self) -> None:
        self._db_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._api_base_url: str | None = None

    def set_db_session_factory(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._db_session_factory = factory

    @property
    def db_session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._db_session_factory is None:
            raise RuntimeError("db_session_factory not set on McpGatewayServerAccessService")
        return self._db_session_factory

    @property
    def api_base_url(self) -> str:
        """Gatana API base URL derived from MCP_GATEWAY_URL (strip /mcp, append /api/v1)."""
        if self._api_base_url is None:
            base = config.mcp_gateway.url.rstrip("/")
            if base.endswith("/mcp"):
                base = base[: -len("/mcp")]
            self._api_base_url = f"{base}/api/v1"
        return self._api_base_url

    def _make_client(self, gatana_token: str):
        """Create an authenticated Gatana client using the user's exchanged token."""
        from gatana_client import GatanaClient

        return GatanaClient(token=gatana_token, base_url=self.api_base_url)

    async def resolve_gateway_team_id(self, group_id: int) -> str | None:
        """Resolve the MCP gateway team ID for a group.

        Looks up the outbound SCIM sync state to find a remote_id for this group
        at an active endpoint flagged as the MCP gateway (is_mcp_gateway).

        Returns:
            The remote_id (Gatana team ID) if found, None otherwise.
        """
        async with self.db_session_factory() as db:
            result = await db.execute(
                text("""
                    SELECT ss.remote_id, ss.endpoint_id, ep.endpoint_url
                    FROM outbound_scim_sync_state ss
                    JOIN outbound_scim_endpoints ep
                        ON ep.id = ss.endpoint_id
                    WHERE ss.entity_type = 'group'
                      AND ss.entity_id = :entity_id
                      AND ss.remote_id IS NOT NULL
                      AND ep.is_mcp_gateway = true
                      AND ep.enabled = true
                      AND ep.deleted_at IS NULL
                    ORDER BY ss.endpoint_id
                """),
                {"entity_id": str(group_id)},
            )
            rows = result.fetchall()

        if not rows:
            logger.warning(
                f"resolve_gateway_team_id: no sync state for group_id={group_id} at any "
                f"active endpoint flagged is_mcp_gateway. Check that the MCP gateway's "
                f"outbound SCIM endpoint has the flag set."
            )
            return None

        if len(rows) > 1:
            logger.warning(
                f"resolve_gateway_team_id: group_id={group_id} is synced to "
                f"{len(rows)} endpoints flagged is_mcp_gateway "
                f"({[(r.endpoint_id, r.endpoint_url) for r in rows]}); "
                f"using endpoint_id={rows[0].endpoint_id}"
            )

        row = rows[0]
        logger.info(
            f"resolve_gateway_team_id: group_id={group_id} -> team_id='{row.remote_id}' "
            f"(endpoint_id={row.endpoint_id})"
        )
        return row.remote_id

    async def list_server_access(
        self, gatana_token: str, group_id: int
    ) -> list[McpGatewayServerPermission]:
        """List MCP server access permissions for a group.

        Args:
            gatana_token: User's exchanged Gatana token
            group_id: Internal group ID

        Returns:
            List of server permissions

        Raises:
            ValueError: If group is not managed by the MCP gateway
        """
        team_id = await self.resolve_gateway_team_id(group_id)
        if not team_id:
            raise ValueError(f"Group {group_id} is not managed by the MCP gateway")

        from gatana_client.api.teams import get_teams_team_id_servers

        client = self._make_client(gatana_token)
        response = await get_teams_team_id_servers.asyncio(team_id=team_id, client=client)

        if response is None:
            return []

        permissions: list[McpGatewayServerPermission] = []
        for item in response.permissions:
            permissions.append(
                McpGatewayServerPermission(
                    server_slug=item.server_slug,
                    role=str(item.permission.role),
                )
            )

        return permissions

    async def grant_server_access(
        self, gatana_token: str, group_id: int, server_slug: str, role: str
    ) -> None:
        """Grant or update server access for a group.

        Args:
            gatana_token: User's exchanged Gatana token
            group_id: Internal group ID
            server_slug: MCP server slug
            role: Access role ("admin", "maintainer", or "member")

        Raises:
            ValueError: If group is not managed by the MCP gateway
        """
        team_id = await self.resolve_gateway_team_id(group_id)
        if not team_id:
            raise ValueError(f"Group {group_id} is not managed by the MCP gateway")

        from gatana_client.api.mcp_servers import (
            put_mcp_servers_server_slug_members_member_type_member_id,
        )
        from gatana_client.models.put_mcp_servers_server_slug_members_member_type_member_id_body import (
            PutMcpServersServerSlugMembersMemberTypeMemberIdBody,
        )
        from gatana_client.models.put_mcp_servers_server_slug_members_member_type_member_id_body_role import (
            PutMcpServersServerSlugMembersMemberTypeMemberIdBodyRole,
        )
        from gatana_client.models.schema_75 import Schema75

        client = self._make_client(gatana_token)
        body = PutMcpServersServerSlugMembersMemberTypeMemberIdBody(
            role=PutMcpServersServerSlugMembersMemberTypeMemberIdBodyRole(role),
        )

        await put_mcp_servers_server_slug_members_member_type_member_id.asyncio(
            server_slug=server_slug,
            member_type=Schema75.TEAMS,
            member_id=team_id,
            client=client,
            body=body,
        )

        logger.info(
            f"MCP gateway: granted {role} access on server '{server_slug}' "
            f"to group {group_id} (team_id={team_id})"
        )

    async def revoke_server_access(
        self, gatana_token: str, group_id: int, server_slug: str
    ) -> None:
        """Revoke server access for a group.

        Args:
            gatana_token: User's exchanged Gatana token
            group_id: Internal group ID
            server_slug: MCP server slug

        Raises:
            ValueError: If group is not managed by the MCP gateway
        """
        team_id = await self.resolve_gateway_team_id(group_id)
        if not team_id:
            raise ValueError(f"Group {group_id} is not managed by the MCP gateway")

        from gatana_client.api.mcp_servers import (
            delete_mcp_servers_server_slug_members_member_type_member_id,
        )
        from gatana_client.models.schema_75 import Schema75

        client = self._make_client(gatana_token)

        await delete_mcp_servers_server_slug_members_member_type_member_id.asyncio(
            server_slug=server_slug,
            member_type=Schema75.TEAMS,
            member_id=team_id,
            client=client,
        )

        logger.info(
            f"MCP gateway: revoked access on server '{server_slug}' "
            f"for group {group_id} (team_id={team_id})"
        )
