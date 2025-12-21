"""Agent discovery services for dynamic sub-agent and tool discovery.

This module handles the discovery of available sub-agents and tools,
including caching and error handling.
"""

import logging
from typing import Any, List, Optional

import httpx
from a2a.types import AgentCard
from deepagents import CompiledSubAgent
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..a2a import make_a2a_async_runnable
from ..middleware import A2ATaskTrackingMiddleware
from ..models import AgentSettings

logger = logging.getLogger(__name__)


class AgentDiscoveryService:
    """Service for discovering available sub-agents and tools.

    Handles fetching agent cards, registering streaming runnables,
    and caching discovery results.
    """

    def __init__(
        self,
        config: AgentSettings,
        oauth2_client: OidcOAuth2Client,
    ):
        """Initialize the discovery service.

        Args:
            config: AgentSettings instance containing configuration
        """
        self.config = config
        self.oauth2_client = oauth2_client

    async def register_agents(
        self,
        agent_urls: List[str],
        token: str,
        user_context: Optional[dict[str, Any]] = None,
        streaming_middleware: Optional[A2ATaskTrackingMiddleware] = None,
    ) -> List[CompiledSubAgent]:
        """Discover available sub-agents by fetching their agent cards.

        Args:
            agent_urls: List of agent URLs to discover
            token: User's access token for authentication and token exchange
            user_context: Optional user context dict with user_id, email, name
            client_credentials_auth: Optional OidcClientCredentialsAuth for client credentials flow
            streaming_middleware: Optional middleware for registering streaming runnables

        Returns:
            List of discovered sub-agents
        """

        logger.debug("Starting agent discovery...")

        sub_agents = []
        for base_url in agent_urls:
            try:
                agent = await self._discover_single_agent(
                    base_url,
                    streaming_middleware,
                    token,
                    user_context,
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
        streaming_middleware: Optional[A2ATaskTrackingMiddleware] = None,
        user_token: Optional[str] = None,
        user_context: Optional[dict[str, Any]] = None,
    ) -> Optional[CompiledSubAgent]:
        """Discover a single agent from the given URL.

        Args:
            base_url: Base URL of the agent
            streaming_middleware: Optional middleware for registering streaming runnables
            user_token: User's access token for authentication
            user_context: Optional user context dict with user_id, email, name

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
        base_runnable = make_a2a_async_runnable(
            agent_card,
            self.oauth2_client,
            user_token=user_token,
            user_context=user_context,
        )
        logger.debug(f"A2A runnable created successfully for {agent_card.url}")

        # Create the sub-agent (middleware will be applied by create_deep_agent)
        agent_name = agent_card.name.replace(" ", "")  # Remove spaces for tool name

        # Register streaming runnable with middleware if provided
        if streaming_middleware and hasattr(base_runnable, "_streaming_runnable"):
            if hasattr(streaming_middleware, "register_streaming_runnable"):
                streaming_middleware.register_streaming_runnable(
                    agent_name,
                    base_runnable._streaming_runnable,  # type: ignore
                )
                logger.debug(f"Registered streaming runnable for {agent_name}")
        elif hasattr(base_runnable, "_streaming_runnable"):
            logger.warning(f"No streaming middleware provided for {agent_name}")

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

    def __init__(self, config: AgentSettings, oauth2_client: OidcOAuth2Client):
        """Initialize the tool discovery service.

        Args:
            config: AgentSettings instance containing configuration
        """
        self.config = config
        self.oauth2_client = oauth2_client

    async def discover_tools(
        self,
        token: str,
        white_list: Optional[List[str]] = None,
    ) -> List[BaseTool]:
        """Discover available MCP tools with token exchange for mcp-gateway.

        Performs token exchange to obtain a token for the mcp-gateway client
        in the same Keycloak realm, then uses that token to authenticate with
        MCP services.

        Args:
            token: User's access token from the orchestrator

        Returns:
            List of discovered tools
        """
        logger.debug("Discovering tools for orchestrator deep agent")
        try:
            # Exchange user token for mcp-gateway token
            # The target client is 'mcp-gateway' in the same Keycloak realm
            mcp_gateway_token = await self.oauth2_client.exchange_token(
                subject_token=token,
                target_client_id="mcp-gateway",
                requested_scopes=["openid", "profile", "offline_access"],
            )
            logger.info("Successfully exchanged token for mcp-gateway")

            # Use the exchanged token for MCP connection
            # logger.debug(f"Gatana MCP gateway token: {mcp_gateway_token}")
            client = MultiServerMCPClient(
                connections={
                    "gatana": StreamableHttpConnection(
                        transport="streamable_http",
                        url="https://alloych.gatana.ai/mcp",
                        headers={"Authorization": f"Bearer {mcp_gateway_token}"},
                    )
                }
            )

            tools = await client.get_tools()
            logger.debug(f"Discovered {len(tools)} MCP tools")
            tools = [tool for tool in tools if tool.name in (white_list or [])]
            logger.debug(f"Filtered tools based on white list: {len(tools)} tools remain")
            return tools

        except Exception as e:
            logger.error(f"Failed to discover tools with token exchange: {e}", exc_info=True)
            return []
        # finally:
        #     await self.oauth2_client.close()
