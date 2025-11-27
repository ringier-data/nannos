"""
Core module for the Orchestrator Agent.

This module contains the core business logic and orchestration components
that form the heart of the agent functionality.

Key Components:
- OrchestratorDeepAgent: Main orchestrator agent with personalized configuration
- AgentExecutor: A2A executor wrapper for task execution
- AgentDiscoveryService: Dynamic sub-agent and tool discovery
- GraphFactory: Centralized graph creation and management

Usage:
    from app.core import (
        OrchestratorDeepAgent,
        AgentExecutor,
        AgentDiscoveryService,
        GraphFactory,
    )
"""

from .agent import OrchestratorDeepAgent
from .discovery import AgentDiscoveryService, ToolDiscoveryService
from .executor import AgentExecutor
from .graph_factory import GraphFactory

__all__ = [
    "OrchestratorDeepAgent",
    "AgentExecutor",
    "AgentDiscoveryService",
    "ToolDiscoveryService",
    "GraphFactory",
]
