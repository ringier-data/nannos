"""
Core module for the Orchestrator Agent.

This module contains the core business logic and orchestration components
that form the heart of the agent functionality.

Key Components:
- OrchestratorDeepAgent: Main orchestrator agent with personalized configuration
- AgentExecutor: A2A executor wrapper for task execution
- AgentDiscoveryService: Dynamic sub-agent and tool discovery
- GraphFactory: Centralized graph creation and management
- create_model: Utility function for creating LangChain models

Usage:
    from app.core.agent import OrchestratorDeepAgent
    from app.core.executor import AgentExecutor
    from app.core.discovery import AgentDiscoveryService
    from app.core.graph_factory import GraphFactory
    from app.core.model_factory import create_model
"""

# DO NOT import OrchestratorDeepAgent, AgentExecutor, or GraphFactory here
# They create circular imports. Import them directly from their modules where needed.
from .discovery import AgentDiscoveryService, ToolDiscoveryService
from .model_factory import create_model

__all__ = [
    "AgentDiscoveryService",
    "ToolDiscoveryService",
    "create_model",
]
