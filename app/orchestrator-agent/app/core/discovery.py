"""Agent discovery services for dynamic sub-agent and tool discovery.

This module handles the discovery of available sub-agents and tools,
including caching and error handling.
"""

import logging
from typing import List, Optional

import httpx
from pydantic import SecretStr
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from a2a.types import AgentCard
from deepagents import CompiledSubAgent

from ..subagents import make_a2a_async_runnable, A2ATaskTrackingMiddleware
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
    ):
        """Initialize the discovery service.
        
        Args:
            config: AgentSettings instance containing configuration
        """
        self.config = config

    async def _get_agents_from_registry(self, token: SecretStr) -> List[str]:
        """Fetch agents from a service registry using the provided token."""
        # TODO: implement actual registry call
        logger.debug("Fetching agent URLs from service registry")
        return ["http://localhost:10000", "http://localhost:9999"]

    async def discover_agents(
        self,
        token: SecretStr,
        streaming_middleware: Optional[A2ATaskTrackingMiddleware] = None,
    ) -> List[CompiledSubAgent]:
        """Discover available sub-agents by fetching their agent cards.
        
        Args:
            token: Authentication token (currently unused but reserved for future use)
            streaming_middleware: Optional middleware for registering streaming runnables
            
        Returns:
            List of discovered sub-agents
        """
        
        logger.debug("Starting agent discovery...")
        
        sub_agents = []
        agent_urls = await self._get_agents_from_registry(token)
        for base_url in agent_urls:
            try:
                agent = await self._discover_single_agent(base_url, streaming_middleware)
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
        streaming_middleware: Optional[A2ATaskTrackingMiddleware] = None
    ) -> Optional[CompiledSubAgent]:
        """Discover a single agent from the given URL.
        
        Args:
            base_url: Base URL of the agent
            streaming_middleware: Optional middleware for registering streaming runnables
            
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
        
        # Create the A2A runnable with the proper agent card
        base_runnable = make_a2a_async_runnable(agent_card=agent_card)
        logger.debug(f"A2A runnable created successfully for {agent_card.url}")
        
        # Create the sub-agent (middleware will be applied by create_deep_agent)
        agent_name = agent_card.name.replace(' ', '')  # Remove spaces for tool name
        
        # Register streaming runnable with middleware if provided
        if streaming_middleware and hasattr(base_runnable, '_streaming_runnable'):
            if hasattr(streaming_middleware, 'register_streaming_runnable'):
                streaming_middleware.register_streaming_runnable(
                    agent_name,
                    base_runnable._streaming_runnable  # type: ignore
                )
                logger.debug(f"Registered streaming runnable for {agent_name}")
        elif hasattr(base_runnable, '_streaming_runnable'):
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
            logger.warning(f"Agent at {base_url} connection was interrupted (ReadError). The agent may have crashed or be offline.")
        else:
            # Only show full traceback for unexpected errors
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
    

class ToolDiscoveryService:
    """Service for discovering available MCP tools.
    
    Handles connecting to MCP servers and retrieving available tools.
    """
    
    def __init__(self, config: AgentSettings):
        """Initialize the tool discovery service.
        
        Args:
            config: AgentSettings instance containing configuration
        """
        self.config = config
    
    async def discover_tools(self, token: SecretStr) -> List[BaseTool]:
        """Discover available MCP tools.
        
        Args:
            token: Authentication token (currently unused but reserved for future use)
            
        Returns:
            List of discovered tools
        """
        logger.debug("Discovering tools for orchestrator deep agent")
        
        client = MultiServerMCPClient(connections={
            "gatana": StreamableHttpConnection(
                transport="streamable_http",
                url="https://alloych.gatana.ai/mcp",
                headers={"Authorization": f"Bearer {self.config.get_gatana_api_key()}"}
            )
        })
        
        return await client.get_tools()
