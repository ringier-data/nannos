"""Pydantic models for MCP gateway server access management."""

from typing import Literal

from pydantic import BaseModel


class McpGatewayStatusResponse(BaseModel):
    """Response for checking if a group is managed by the MCP gateway."""

    managed: bool
    team_id: str | None = None


class McpGatewayServerPermission(BaseModel):
    """A single server permission entry."""

    server_slug: str
    server_name: str
    role: str


class McpGatewayServerPermissionsResponse(BaseModel):
    """Response listing server access permissions for a group."""

    permissions: list[McpGatewayServerPermission]


class McpGatewayGrantServerAccessRequest(BaseModel):
    """Request body for granting or updating server access."""

    role: Literal["admin", "maintainer", "member"]
