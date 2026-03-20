"""Agent discovery services for dynamic sub-agent and tool discovery.

This module handles the discovery of available sub-agents and tools,
including caching and error handling.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
from a2a.types import AgentCard
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.factory import make_a2a_async_runnable
from deepagents import CompiledSubAgent
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..models.config import AgentSettings

logger = logging.getLogger(__name__)


class AgentDiscoveryService:
    """Service for discovering available sub-agents and tools.

    Handles fetching agent cards, registering streaming runnables,
    and caching discovery results.
    """

    def __init__(
        self,
        config: AgentSettings,
        oauth2_client: OidcOAuth2Client | None = None,
    ):
        """Initialize the discovery service.

        Args:
            config: AgentSettings instance containing configuration
            oauth2_client: OAuth2 client for agent-to-agent auth (None in local dev mode)
        """
        self.config = config
        self.oauth2_client = oauth2_client

    async def register_agents(
        self,
        agent_metadata: dict[str, dict[str, Any]],
        token: str,
    ) -> List[CompiledSubAgent]:
        """Discover available sub-agents by fetching their agent cards.

        Args:
            agent_metadata: Metadata map from agent_url -> {sub_agent_id, name, description}
            token: User's access token for authentication and token exchange

        Returns:
            List of discovered sub-agents
        """

        logger.debug("Starting agent discovery...")

        sub_agents = []
        for base_url in agent_metadata.keys():
            try:
                # Get metadata for this agent URL
                metadata = agent_metadata.get(base_url, {})
                sub_agent_id = metadata.get("sub_agent_id")

                agent = await self._discover_single_agent(
                    base_url,
                    token,
                    sub_agent_id=sub_agent_id,  # Pass sub_agent_id to discovery
                )
                if agent:
                    sub_agents.append(agent)
            except Exception as e:
                logger.warning(f"Failed to discover agent at {base_url}: {type(e).__name__}: {e}")
                self._log_discovery_error(e, base_url)

        logger.debug(f"Agent discovery complete. Found {len(sub_agents)} agents")

        return sub_agents

    async def _discover_single_agent(
        self,
        base_url: str,
        user_token: Optional[str] = None,
        sub_agent_id: Optional[int] = None,
    ) -> Optional[CompiledSubAgent]:
        """Discover a single agent from the given URL.

        Args:
            base_url: Base URL of the agent
            user_token: User's access token for authentication

        Returns:
            CompiledSubAgent if discovery succeeds, None otherwise
        """
        logger.debug(f"Fetching agent card from: {base_url}")

        agent_card_url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
        logger.debug(f"Agent card URL: {agent_card_url}")

        async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
            response = await client.get(agent_card_url)
            response.raise_for_status()
            agent_card_data = response.json()
            logger.debug(f"Agent card data: {agent_card_data}")

            # Create AgentCard from the fetched data
            agent_card = AgentCard(**agent_card_data)
            logger.debug(f"Agent card parsed: name={agent_card.name}, url={agent_card.url}")

        # Create the A2A runnable with the proper agent card and authentication
        # Pass sub_agent_id via config for cost tracking attribution
        config = A2AClientConfig(sub_agent_id=sub_agent_id)
        base_runnable = make_a2a_async_runnable(
            agent_card,
            self.oauth2_client,
            user_token=user_token,
            config=config,
        )
        logger.debug(f"A2A runnable created successfully for {agent_card.url} with sub_agent_id={sub_agent_id}")

        # Create the sub-agent (middleware will be applied by create_deep_agent)
        agent_name = agent_card.name.replace(" ", "")  # Remove spaces for tool name

        agent = CompiledSubAgent(
            name=agent_name,
            description=agent_card.description,
            runnable=base_runnable,
        )
        logger.debug(f"Sub-agent created: name={agent['name']}, description={agent['description']}")

        return agent

    def _log_discovery_error(self, error: Exception, base_url: str) -> None:
        """Log appropriate warning message based on error type.

        Args:
            error: The exception that occurred
            base_url: URL where the error occurred
        """
        if isinstance(error, httpx.ConnectError):
            logger.warning(f"Agent at {base_url} is not reachable (connection refused). The agent may be offline.")
        elif isinstance(error, httpx.TimeoutException):
            logger.warning(f"Agent at {base_url} timed out. The agent may be slow or offline.")
        elif isinstance(error, httpx.HTTPStatusError):
            logger.warning(f"Agent at {base_url} returned HTTP error: {error.response.status_code}")
        elif isinstance(error, httpx.ReadError):
            logger.warning(
                f"Agent at {base_url} connection was interrupted (ReadError). The agent may have crashed or be offline."
            )
        else:
            # Only show full traceback for unexpected errors
            import traceback

            logger.debug(f"Traceback: {traceback.format_exc()}")


class ToolDiscoveryService:
    """Service for discovering available MCP tools.

    Handles connecting to MCP servers and retrieving available tools.
    """

    def __init__(self, config: AgentSettings, oauth2_client: OidcOAuth2Client | None = None):
        """Initialize the tool discovery service.

        Args:
            config: AgentSettings instance containing configuration
            oauth2_client: OAuth2 client for MCP gateway auth (None in local dev mode)
        """
        self.config = config
        self.oauth2_client = oauth2_client

    async def fetch_available_servers(self, token: str) -> List[Dict[str, str]]:
        """Fetch list of available MCP servers from the gateway.

        Args:
            token: User's access token for authentication

        Returns:
            List of server metadata dicts with 'slug' and 'description' keys

        Raises:
            httpx.HTTPError: If API request fails
        """
        logger.debug("Fetching available MCP servers from gateway")
        try:
            # Call the MCP gateway API to get server list
            # Extract base URL from MCP_GATEWAY_URL (remove /mcp path)
            base_url = self.config.MCP_GATEWAY_URL.rstrip("/mcp").rstrip("/")
            servers_url = f"{base_url}/api/v1/mcp-servers"

            logger.debug(f"Fetching servers from: {servers_url}")

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    servers_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                data = response.json()

                servers = data.get("servers", [])
                logger.debug(f"Discovered {len(servers)} MCP servers")
                return servers

        except Exception as e:
            logger.error(f"Failed to fetch MCP servers: {e}", exc_info=True)
            return []

    async def discover_tools(
        self,
        token: str,
        white_list: Optional[List[str]] = None,
        include_server_slugs: Optional[List[str]] = None,
    ) -> List[BaseTool]:
        """Discover available MCP tools with optional server filtering.

        Performs token exchange to obtain a token for the gatana client
        in the same Keycloak realm, then uses that token to authenticate with
        MCP services.

        Args:
            token: User's access token from the orchestrator
            white_list: Optional list of tool names to filter to (post-discovery filtering)
            include_server_slugs: Optional list of server slugs to include tools from

        Returns:
            List of discovered tools with server_name in metadata
        """
        logger.debug("Discovering tools for orchestrator deep agent")
        try:
            # Exchange user token for gatana token
            # The target client is 'gatana' in the same Keycloak realm
            mcp_gateway_token = await self.oauth2_client.exchange_token(
                subject_token=token,
                target_client_id="gatana",
                requested_scopes=["openid", "profile", "offline_access"],
            )
            logger.info("Successfully exchanged token for gatana")

            # Fetch available servers to create per-server connections
            servers = await self.fetch_available_servers(mcp_gateway_token)

            if not servers:
                logger.warning("No MCP servers discovered, returning empty tool list")
                return []

            # Filter servers if include_server_slugs is provided
            if include_server_slugs:
                servers = [s for s in servers if s.get("slug") in include_server_slugs]
                logger.debug(f"Filtered to {len(servers)} servers: {[s.get('slug') for s in servers]}")

            # Create one connection per MCP server
            # This allows MultiServerMCPClient to naturally track which tools come from which server
            connections = {}
            for server in servers:
                server_slug = server.get("slug")
                if not server_slug:
                    continue

                # Each connection uses the gateway URL but filtered to one server
                connections[server_slug] = StreamableHttpConnection(
                    transport="streamable_http",
                    url=f"{self.config.MCP_GATEWAY_URL}?includeOnlyServerSlugs={server_slug}",
                    headers={"Authorization": f"Bearer {mcp_gateway_token}"},
                )

            if not connections:
                logger.warning("No valid server connections created, returning empty tool list")
                return []

            # Add playground-backend as an additional MCP server
            # Playground-backend MCP endpoints require Gatana token for calling Gatana gateway
            if self.config.PLAYGROUND_BACKEND_URL:
                playground_mcp_url = f"{self.config.PLAYGROUND_BACKEND_URL}/mcp"
                connections["playground"] = StreamableHttpConnection(
                    transport="streamable_http",
                    url=playground_mcp_url,
                    headers={"Authorization": f"Bearer {mcp_gateway_token}"},
                )
                logger.debug(f"Added playground MCP connection: {playground_mcp_url}")

            logger.debug(f"Created {len(connections)} MCP server connections: {list(connections.keys())}")

            # Create client with per-server connections
            # Discover tools per-server in parallel and tag each tool's metadata
            # with server_name (langchain_mcp_adapters does NOT store server_name
            # in tool.metadata automatically — it only captures it in call_tool closures)
            client = MultiServerMCPClient(connections=connections)

            async def _get_tagged_tools(slug: str) -> list:
                server_tools = await client.get_tools(server_name=slug)
                for tool in server_tools:
                    if tool.metadata is None:
                        tool.metadata = {}
                    tool.metadata["server_name"] = slug
                return server_tools

            results = await asyncio.gather(*[_get_tagged_tools(slug) for slug in connections])
            tools = [tool for server_tools in results for tool in server_tools]
            logger.debug(f"Discovered {len(tools)} MCP tools from {len(connections)} servers")

            # Apply whitelist filtering if provided
            if white_list:
                tools = [tool for tool in tools if tool.name in white_list]
                logger.debug(f"Filtered tools based on white list: {len(tools)} tools remain")

            return tools

        except Exception as e:
            logger.error(f"Failed to discover tools with token exchange: {e}", exc_info=True)
            return []
        # finally:
        #     await self.oauth2_client.close()
