"""MCP tools router for discovering available tools from MCP gateway."""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..config import config
from ..dependencies import require_auth_or_bearer_token
from ..models.user import User

logger = logging.getLogger(__name__)

# Create router
router: APIRouter = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])


class MCPTool(BaseModel):
    """MCP tool information."""

    name: str
    description: str | None = None
    input_schema: dict | None = None  # JSON Schema for tool parameters
    server: str | None = None  # MCP server name if provided by gateway


class MCPToolsResponse(BaseModel):
    """Response model for MCP tools list."""

    tools: list[MCPTool]


@router.get("/tools", response_model=MCPToolsResponse, tags=["MCP"], operation_id="playground_list_mcp_tools")
async def list_mcp_tools(
    request: Request,
    user: User = Depends(require_auth_or_bearer_token),
) -> MCPToolsResponse:
    """List available MCP tools from Gatana gateway.

    Uses MCP JSON-RPC standard to fetch tools list.
    Performs token exchange to obtain a token for the gatana client.

    Returns:
        List of available MCP tools with names and descriptions.

    Raises:
        401 Unauthorized: If token exchange fails
        503 Service Unavailable: If MCP gateway is unreachable
    """
    try:
        # When impersonating, return empty tools list since we don't have impersonated user's token
        # MCP tools are tied to the user's access token, not their user object
        if hasattr(request.state, "original_user") and request.state.original_user:
            logger.info(
                f"Admin {request.state.original_user.email} is impersonating {user.email}. "
                "Returning empty MCP tools list (impersonated user's token not available)"
            )
            return MCPToolsResponse(tools=[])

        # Get user's access token from request state (session) or Authorization header (Bearer token)

        auth_header = request.headers.get("Authorization")
        # Exchange user token for gatana token
        if not auth_header:
            access_token = getattr(request.state, "access_token", None)
            if not access_token:
                logger.error(f"No access token available for user {user.email}")
                raise HTTPException(
                    status_code=401,
                    detail="No access token available. Please log in again.",
                )

            # Check if token is expired or expiring soon (within 60 seconds)
            token_expiry = getattr(request.state, "access_token_expires_at", None)
            if token_expiry:
                time_until_expiry = (token_expiry - datetime.now(timezone.utc)).total_seconds()
                is_expired = time_until_expiry < 60
            else:
                # If no expiry info, assume token might be expired
                is_expired = True
                time_until_expiry = 0

            if is_expired:
                logger.info(
                    f"User access token is expired or expiring soon (expires in {time_until_expiry:.1f}s), refreshing..."
                )
                try:
                    # Get refresh token from session
                    refresh_token = getattr(request.state, "refresh_token", None)
                    if not refresh_token:
                        logger.error(f"No refresh token available for user {user.email}")
                        raise HTTPException(
                            status_code=401,
                            detail="Session expired. Please log in again.",
                        )

                    # Create OAuth2 client for refresh
                    oauth2_client = OidcOAuth2Client(
                        client_id=config.oidc.client_id,
                        client_secret=config.oidc.client_secret.get_secret_value(),
                        issuer=config.oidc.issuer,
                    )

                    # Refresh the access token
                    refreshed_tokens = await oauth2_client.refresh_token(refresh_token)

                    # Update access token for current request
                    access_token = refreshed_tokens["access_token"]

                    # Calculate new expiration time
                    expires_in = int(refreshed_tokens["expires_in"])
                    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                    logger.info(
                        f"User access token refreshed, new expiry at {new_expires_at.isoformat()} "
                        f"({expires_in} seconds from now)"
                    )

                    # Update the session with new tokens (assuming you have a session service)
                    # Note: You'll need to import and use your session service here
                    # await session_service.update_session(
                    #     session_id=request.state.session_id,
                    #     user_id=user.id,
                    #     access_token=refreshed_tokens["access_token"],
                    #     access_token_expires_at=new_expires_at,
                    #     refresh_token=refreshed_tokens.get("refresh_token", refresh_token),
                    #     id_token=refreshed_tokens.get("id_token"),
                    #     issued_at=datetime.now(timezone.utc),
                    # )

                    logger.info(f"Successfully refreshed access token for user {user.email}")

                except Exception as e:
                    logger.error(f"Failed to refresh access token: {e}")
                    raise HTTPException(
                        status_code=401,
                        detail="Session expired: Unable to refresh access token. Please re-authenticate.",
                    )
            # Exchange user token for MCP gateway token
            oauth2_client = OidcOAuth2Client(
                client_id=config.oidc.client_id,
                client_secret=config.oidc.client_secret.get_secret_value(),
                issuer=config.oidc.issuer,
            )
            mcp_gateway_token = await oauth2_client.exchange_token(
                subject_token=access_token,
                target_client_id=config.mcp_gateway.client_id,
                requested_scopes=["openid", "profile", "offline_access"],
            )
        else:
            # Extract Bearer token from Authorization header
            # NOTE: in this case we should receive the already exchanged token for MCP gateway
            if not auth_header.startswith("Bearer "):
                logger.error("Invalid Authorization header format")
                raise HTTPException(
                    status_code=401,
                    detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
                )
            mcp_gateway_token = auth_header[len("Bearer ") :].strip()

        logger.info(f"Successfully exchanged token for {config.mcp_gateway.client_id} for user {user.email}")

        async with httpx.AsyncClient(timeout=10.0) as client:
            # MCP standard JSON-RPC request for tools/list with authentication
            # Gatana requires both application/json and text/event-stream in Accept header
            response = await client.post(
                config.mcp_gateway.url,
                headers={
                    "Authorization": f"Bearer {mcp_gateway_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                },
            )

            response.raise_for_status()

            # Parse response based on content type
            content_type = response.headers.get("content-type", "")

            if "text/event-stream" in content_type:
                # Handle Server-Sent Events format
                # SSE format: "data: {json}\n\n"
                logger.debug("Parsing SSE response from MCP gateway")
                lines = response.text.strip().split("\n")
                json_data = None

                for line in lines:
                    if line.startswith("data: "):
                        json_str = line[6:]  # Remove "data: " prefix
                        try:
                            json_data = json.loads(json_str)
                            break
                        except json.JSONDecodeError:
                            continue

                if not json_data:
                    logger.error(f"Failed to parse SSE response: {response.text[:500]}")
                    raise HTTPException(
                        status_code=503,
                        detail="Invalid SSE response from MCP gateway",
                    )

                data = json_data
            else:
                # Handle regular JSON response
                data = response.json()

            # Extract tools from MCP response
            if "result" not in data or "tools" not in data["result"]:
                logger.error(f"Unexpected MCP response format: {data}")
                raise HTTPException(
                    status_code=503,
                    detail="Invalid response from MCP gateway",
                )

            mcp_tools = data["result"]["tools"]

            # Convert to our response model
            tools = [
                MCPTool(
                    name=tool.get("name", ""),
                    description=tool.get("description"),
                    input_schema=tool.get("inputSchema"),  # MCP standard field
                    server=tool.get("server") or tool.get("serverName"),  # Check both possible fields
                )
                for tool in mcp_tools
                if tool.get("name")
            ]

            logger.info(f"Fetched {len(tools)} MCP tools from Gatana gateway")

            # Log sample tool for debugging (helps understand available fields)
            if tools:
                logger.debug(f"Sample MCP tool structure: {tools[0]}")

            return MCPToolsResponse(tools=tools)

    except HTTPException:
        # Re-raise HTTP exceptions (like 401 from missing token)
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"MCP gateway returned HTTP error: {e.response.status_code}: {e.response.text}")

        detail = f"Gatana MCP gateway returned error: HTTP {e.response.status_code}"

        raise HTTPException(
            status_code=503,
            detail=detail,
        )
    except httpx.ConnectError as e:
        logger.error(f"Cannot connect to Gatana MCP gateway: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Cannot connect to Gatana MCP gateway at {config.mcp_gateway.url}. Gateway may be offline.",
        )
    except httpx.TimeoutException as e:
        logger.error(f"Gatana MCP gateway request timed out: {e}")
        raise HTTPException(
            status_code=504,
            detail="Gatana MCP gateway request timed out. Gateway may be overloaded.",
        )
    except httpx.RequestError as e:
        logger.error(f"Network error connecting to Gatana MCP gateway: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Network error connecting to Gatana: {type(e).__name__}",
        )
    except Exception as e:
        logger.exception(f"Unexpected error fetching MCP tools: {e}")
        # Check if it's a token exchange error
        if "token" in str(e).lower() or "exchange" in str(e).lower():
            raise HTTPException(
                status_code=401,
                detail=f"Token exchange failed: {str(e)}",
            )
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {type(e).__name__}",
        )
