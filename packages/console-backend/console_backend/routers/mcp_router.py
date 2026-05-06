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
    output_schema: dict | None = None  # JSON Schema for tool output validation
    server: str | None = None  # MCP server name if provided by gateway


class MCPToolsResponse(BaseModel):
    """Response model for MCP tools list."""

    tools: list[MCPTool]


class MCPServer(BaseModel):
    """MCP server information."""

    name: str
    description: str | None = None


class MCPServersResponse(BaseModel):
    """Response model for MCP servers list."""

    servers: list[MCPServer]


class MCPToolsByServer(BaseModel):
    """Tools grouped by server."""

    server: str
    tools: list[str]  # Just tool names for compact listing


class MCPToolsByServerResponse(BaseModel):
    """Response model for tools grouped by server."""

    servers: list[MCPToolsByServer]


@router.get("/tools/search", response_model=MCPToolsResponse, tags=["MCP"], operation_id="console_grep_mcp_tools")
async def grep_mcp_tools(
    request: Request,
    query: str,
    user: User = Depends(require_auth_or_bearer_token),
    server_slug: str | None = None,
    top_k: int = 10,
) -> MCPToolsResponse:
    """Search/grep available MCP tools by name, description, and schema fields.

    Uses MCP JSON-RPC standard to fetch tools list, then performs full-text search with relevance scoring.
    Performs token exchange to obtain a token for the gatana client.

    Scoring algorithm:
    - Exact phrase match in name: +100 points
    - Exact phrase match in description: +50 points
    - Each word match in name: +10 points
    - Each word match in description: +5 points
    - Each word match in input/output schema: +2 points
    - Matches in server name: +3 points

    Args:
        query: Search term to filter tools (case-insensitive, searches across all fields)
        server_slug: Optional server slug to filter tools by specific server
        top_k: Maximum number of results to return (default: 5)

    Returns:
        List of matching MCP tools sorted by relevance score.

    Raises:
        401 Unauthorized: If token exchange fails
        503 Service Unavailable: If MCP gateway is unreachable
    """
    # Fetch all tools using the main list logic
    all_tools = await _list_mcp_tools(request, user, server_slug)

    # Tokenize query and filter stop words
    query_lower = query.lower().strip()
    stop_words = {"the", "a", "an", "of", "in", "on", "for", "with", "and", "or", "to", "from"}
    query_words = [word for word in query_lower.split() if word not in stop_words]

    # If all words were stop words, use the original query
    if not query_words:
        query_words = query_lower.split()

    # Score each tool (using list of tuples instead of dict since Pydantic models aren't hashable)
    tool_scores = []

    for tool in all_tools.tools:
        score = 0

        # Prepare searchable text fields
        name_lower = tool.name.lower()
        description_lower = tool.description.lower() if tool.description else ""
        input_schema_str = json.dumps(tool.input_schema).lower() if tool.input_schema else ""
        output_schema_str = json.dumps(tool.output_schema).lower() if tool.output_schema else ""
        server_lower = tool.server.lower() if tool.server else ""

        # Exact phrase match (highest priority)
        if query_lower in name_lower:
            score += 100
        if query_lower in description_lower:
            score += 50

        # Individual word matches
        for word in query_words:
            # Name matches (high weight)
            if word in name_lower:
                # Bonus for word boundary matches (whole word)
                if f" {word} " in f" {name_lower} " or name_lower.startswith(word) or name_lower.endswith(word):
                    score += 15
                else:
                    score += 10

            # Description matches (medium weight)
            if word in description_lower:
                score += 5

            # Schema field matches (lower weight)
            if word in input_schema_str:
                score += 2
            if word in output_schema_str:
                score += 2

            # Server name matches
            if word in server_lower:
                score += 3

        # Only include tools with non-zero scores
        if score > 0:
            tool_scores.append((score, tool))

    # Sort by score (descending) and take top_k
    tool_scores.sort(key=lambda x: x[0], reverse=True)
    filtered_tools = [tool for score, tool in tool_scores[:top_k]]

    logger.info(
        f"Grep MCP tools: query='{query}', server='{server_slug}', "
        f"found {len(filtered_tools)}/{len(all_tools.tools)} matches (top {top_k})"
    )

    return MCPToolsResponse(tools=filtered_tools)


def _get_console_mcp_tools(request: Request, user: User | None = None) -> list[MCPTool]:
    """Extract console backend's own MCP tools from FastAPI routes tagged 'MCP'.

    Introspects the app's routes to find endpoints tagged with 'MCP' and
    returns them as MCPTool objects. Excludes discovery endpoints (this router).
    Triage-only tools (e.g. set external link) are hidden from users without
    the ``triage`` or ``triage.admin`` capability on ``bug_reports``.
    """
    from ..authorization import check_capability

    # Tools that require triage capability to be visible
    triage_only_tools = {
        "console_set_bug_report_external_link",
    }

    has_triage = False
    if user is not None:
        has_triage = (
            user.is_administrator
            or check_capability(user.role.value, "bug_reports", "triage")
            or check_capability(user.role.value, "bug_reports", "triage.admin")
        )

    app = request.app
    tools = []
    for route in app.routes:
        if not hasattr(route, "tags") or "MCP" not in route.tags:
            continue
        # Skip MCP discovery/search endpoints (this router's own endpoints)
        operation_id = getattr(route, "operation_id", None) or ""
        if operation_id in ("console_list_mcp_tools", "console_grep_mcp_tools", "console_list_mcp_servers"):
            continue
        name = operation_id or route.name or ""
        if not name:
            continue
        # Hide triage-only tools from users without triage capability
        if name in triage_only_tools and not has_triage:
            continue
        summary = getattr(route, "summary", None) or ""
        description = getattr(route, "description", None) or ""
        tools.append(
            MCPTool(
                name=name,
                description=f"{summary} {description}".strip() if summary or description else None,
                server="console",
            )
        )
    return tools


async def _list_mcp_tools(
    request: Request,
    user: User,
    server_slug: str | None = None,
) -> MCPToolsResponse:
    # When impersonating, return empty tools list since we don't have impersonated user's token
    # MCP tools are tied to the user's access token, not their user object
    if hasattr(request.state, "original_user") and request.state.original_user:
        logger.info(
            f"Admin {request.state.original_user.email} is impersonating {user.email}. "
            "Returning empty MCP tools list (impersonated user's token not available)"
        )
        return MCPToolsResponse(tools=[])

    # If filtering by "console" server, return only console backend's own MCP tools
    if server_slug == "console":
        console_tools = _get_console_mcp_tools(request, user)
        return MCPToolsResponse(tools=console_tools)

    # Get Gatana token (handles both session-based and Bearer token authentication)
    mcp_gateway_token = await _get_gatana_token(request, user)

    if server_slug:
        logger.info(f"Fetching tools for server '{server_slug}' from Gatana MCP gateway")
        gatana_url = f"{config.mcp_gateway.url}?includeOnlyServerSlugs={server_slug}"
    else:
        logger.info("Fetching all tools from Gatana MCP gateway")
        gatana_url = config.mcp_gateway.url
    async with httpx.AsyncClient(timeout=10.0) as client:
        # MCP standard JSON-RPC request for tools/list with authentication
        # Gatana requires both application/json and text/event-stream in Accept header
        response = await client.post(
            gatana_url,
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
                output_schema=tool.get("outputSchema"),  # MCP standard field for output validation
                server=tool.get("server") or tool.get("serverName"),  # Check both possible fields
            )
            for tool in mcp_tools
            if tool.get("name")
        ]

        logger.info(f"Fetched {len(tools)} MCP tools from Gatana gateway")

        # Log sample tool for debugging (helps understand available fields)
        if tools:
            logger.debug(f"Sample MCP tool structure: {tools[0]}")

        # Append console backend's own MCP tools (tagged "MCP" in FastAPI routes)
        # These are tool endpoints served by this backend (e.g., bug report management)
        console_tools = _get_console_mcp_tools(request, user)
        if console_tools:
            tools.extend(console_tools)
            logger.info(f"Added {len(console_tools)} console backend MCP tools")

        return MCPToolsResponse(tools=tools)


@router.get("/tools", response_model=MCPToolsResponse, operation_id="console_list_mcp_tools")
async def list_mcp_tools(
    request: Request,
    user: User = Depends(require_auth_or_bearer_token),
    server_slug: str | None = None,
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
        return await _list_mcp_tools(request, user, server_slug)

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


async def _get_gatana_token(request: Request, user: User) -> str:
    """
    Get Gatana MCP gateway token from request.

    Supports two authentication patterns:
    1. Session-based (frontend): Extracts user token from request.state, refreshes if needed, exchanges for Gatana token
    2. Bearer token (orchestrator/A2A): Extracts already-exchanged Gatana token from Authorization header

    Args:
        request: FastAPI request object
        user: Authenticated user

    Returns:
        Gatana MCP gateway token (already exchanged)

    Raises:
        HTTPException: If token is missing, expired, or exchange fails
    """
    # Check if Authorization header is present (Bearer token from orchestrator)
    auth_header = request.headers.get("Authorization")

    if auth_header:
        # Bearer token path: orchestrator/A2A already exchanged the token
        if not auth_header.startswith("Bearer "):
            logger.error("Invalid Authorization header format")
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
            )
        return auth_header[len("Bearer ") :].strip()

    # Session-based path: need to get user token and exchange it for Gatana token
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

    try:
        mcp_gateway_token = await oauth2_client.exchange_token(
            subject_token=access_token,
            target_client_id=config.mcp_gateway.client_id,
            requested_scopes=["openid", "profile", "offline_access"],
        )
        logger.info(f"Successfully exchanged token for {config.mcp_gateway.client_id} for user {user.email}")
        return mcp_gateway_token
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        raise HTTPException(
            status_code=401,
            detail=f"Token exchange failed: {str(e)}",
        )


@router.get(
    "/servers",
    response_model=MCPServersResponse,
    tags=["MCP"],
    summary="List available MCP servers",
    description=(
        "Returns a list of available MCP servers (e.g., GitHub, Jira) with tool counts. "
        "Use this endpoint first to discover which servers are available, then use "
        "console_list_mcp_tools using the `server_slug` parameter to explore tools within a specific server."
    ),
    operation_id="console_list_mcp_servers",
)
async def list_mcp_servers(
    request: Request,
    user: User = Depends(require_auth_or_bearer_token),
) -> MCPServersResponse:
    """
    List available MCP servers with tool counts.

    This provides a high-level overview of integration servers available through
    the Gatana MCP gateway. Each server represents an integration (GitHub, Jira, etc.)
    and contains multiple tools for interacting with that service.

    Authorization:
        Requires Gatana MCP gateway token in Authorization header (orchestrator exchanges user token).

    Returns:
        MCPServersResponse: List of servers with tool counts for navigation
    """
    gatana_token = await _get_gatana_token(request, user)

    try:
        # Call the Gatana gateway's /api/v1/mcp-servers endpoint to get the server list
        # Extract base URL from mcp_gateway.url (remove /mcp suffix if present)
        base_url = config.mcp_gateway.url.rstrip("/mcp").rstrip("/")
        servers_url = f"{base_url}/api/v1/mcp-servers"

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {gatana_token}"}

            logger.info(f"Fetching MCP servers from Gatana gateway: {servers_url}")
            response = await client.get(servers_url, headers=headers)
            response.raise_for_status()

            data = response.json()
            servers_data = data.get("servers", [])

            # Convert to our response model with name and tool_count
            # Note: Gateway returns 'slug' and doesn't include tool counts
            # We'll set tool_count to 0 for now (could fetch tools per server if needed)
            servers = [
                MCPServer(name=server.get("slug", "unknown"), description=server.get("description"))
                for server in servers_data
                if server.get("slug") and server.get("isEnabled", False)
            ]

            # Append console backend as a virtual MCP server
            servers.append(MCPServer(name="console", description="Console backend tools (bug reports, sub-agents)"))

            logger.info(f"Discovered {len(servers)} MCP servers from gateway")

            return MCPServersResponse(servers=servers)

    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"MCP gateway returned HTTP error: {e.response.status_code}: {e.response.text}")
        raise HTTPException(status_code=503, detail=f"Gatana MCP gateway returned error: HTTP {e.response.status_code}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching MCP servers: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {type(e).__name__}")
