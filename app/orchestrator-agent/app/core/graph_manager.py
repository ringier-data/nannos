"""
Graph management for OrchestratorDeepAgent.

Handles graph caching, configuration signature generation, and graph lifecycle.
"""
import logging
from typing import Optional, Any
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from deepagents import create_deep_agent, CompiledSubAgent
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langchain.agents.structured_output import ToolStrategy

from ..models import FinalResponseSchema

logger = logging.getLogger(__name__)


class GraphManager:
    """Manages graph creation, caching, and retrieval based on configuration signatures."""
    
    def __init__(
        self,
        model: BaseChatModel,
        checkpointer: BaseCheckpointSaver,
        system_prompt: str,
        middleware: list[Any],
    ):
        """Initialize the graph manager.
        
        Args:
            model: The LLM model to use for graph creation
            checkpointer: Checkpoint saver for conversation persistence
            system_prompt: System instruction for the agent
            middleware: List of middleware to apply to graphs
        """
        self.model = model
        self.checkpointer = checkpointer
        self.system_prompt = system_prompt
        self.middleware = middleware
        self.graphs: dict[str, CompiledStateGraph] = {}
        self.signature = self.get_config_signature(None, None)
        
    def get_config_signature(
        self,
        tools: Optional[list[BaseTool]],
        subagents: Optional[list[CompiledSubAgent]]
    ) -> str:
        """Generate a unique signature for a configuration based on tools and subagents.
        
        This signature is used to cache graphs - the same signature means same capabilities.
        Multiple users with the same tools/subagents will share the same graph instance,
        but are isolated by thread_id in the checkpointer.
        
        Args:
            tools: List of tools available in this configuration
            subagents: List of sub-agents available in this configuration
            
        Returns:
            Unique configuration signature string
        """
        tool_sigs = []
        if tools:
            for tool in tools:
                # Use tool name and description hash
                tool_desc = getattr(tool, 'description', '')
                tool_sigs.append(f"{tool.name}:{hash(tool_desc)}")
        
        subagent_sigs = []
        if subagents:
            for subagent in subagents:
                subagent_sigs.append(f"{subagent['name']}:{hash(subagent['description'])}")
        
        tool_sigs.sort()
        subagent_sigs.sort()
        
        return f"tools:{','.join(tool_sigs)}|subagents:{','.join(subagent_sigs)}"

    def get_cached_graph(self, config_sig: str) -> Optional[CompiledStateGraph]:
        """Retrieve a cached graph by configuration signature.
        
        Args:
            config_sig: Configuration signature to look up
            
        Returns:
            Cached graph if found, None otherwise
        """
        return self.graphs.get(config_sig)
    
    def create_and_cache_graph(
        self,
        config_sig: str,
        tools: Optional[list[BaseTool]],
        subagents: Optional[list[CompiledSubAgent]]
    ) -> CompiledStateGraph:
        """Create a new graph and cache it by configuration signature.
        
        Args:
            config_sig: Configuration signature for caching
            tools: List of tools to include in the graph
            subagents: List of sub-agents to include in the graph
            
        Returns:
            Newly created and cached compiled graph
        """
        logger.info(
            f"Creating new graph for config "
            f"(tools: {len(tools or [])}, subagents: {len(subagents or [])})"
        )
        
        compiled_graph = create_deep_agent(
            model=self.model,
            tools=tools or [],
            subagents=subagents or [],  # type: ignore  # Variance issue with TypedDict
            system_prompt=self.system_prompt,
            checkpointer=self.checkpointer,
            middleware=self.middleware,  # type: ignore  # Variance issue with middleware state types
            response_format=ToolStrategy(FinalResponseSchema),  # Request structured output for task status determination
        )
        
        # Cache the graph
        self.graphs[config_sig] = compiled_graph
        logger.info("Graph created and cached for config signature")
        
        return compiled_graph
    
    def get_or_create_graph(
        self,
        tools: Optional[list[BaseTool]],
        subagents: Optional[list[CompiledSubAgent]]
    ) -> CompiledStateGraph:
        """Get an existing graph or create a new one based on configuration.
        
        Uses caching to avoid recreating graphs for the same tool/subagent combinations.
        
        Args:
            tools: List of tools for this configuration
            subagents: List of sub-agents for this configuration
            
        Returns:
            Tuple of (compiled_graph, config_signature)
        """
        self.signature = self.get_config_signature(tools, subagents)
        logger.debug(f"Config signature: {self.signature[:100]}...")  # Log first 100 chars

        # Check if we have a cached graph for this configuration
        cached_graph = self.get_cached_graph(self.signature)
        if cached_graph is not None:
            logger.debug("Found cached graph for config signature")
            return cached_graph

        # Create new graph for this configuration
        new_graph = self.create_and_cache_graph(self.signature, tools, subagents)
        return new_graph

    def clear_cache(self) -> None:
        """Clear all cached graphs.
        
        Useful for forcing recreation of graphs, e.g., when configuration changes.
        """
        logger.info("Clearing graph cache")
        self.graphs.clear()
