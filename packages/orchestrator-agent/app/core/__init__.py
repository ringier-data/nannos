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
- Model configuration utilities: get_available_models, is_valid_model, get_default_model

Usage:
    from app.core.agent import OrchestratorDeepAgent
    from app.core.executor import AgentExecutor
    from app.core.discovery import AgentDiscoveryService
    from app.core.graph_factory import GraphFactory
    from agent_common.core.model_factory import create_model, get_available_models, is_valid_model
"""

# DO NOT import OrchestratorDeepAgent, AgentExecutor, or GraphFactory here
# They create circular imports. Import them directly from their modules where needed.
# DO NOT import from discovery here either - causes circular import via models
#
# Shared steering state (queues, active dispatch tracking) lives in
# steering_state.py — a lightweight module with no heavy dependencies,
# specifically designed to be imported from both executor.py and
# graph_factory.py without creating circular chains.
# Import model_factory utilities directly when needed

from agent_common.core.model_factory import (
    MODEL_CONFIG,
    _has_aws_credentials as has_aws_credentials,
    create_model,
    get_available_models,
    get_default_model,
    is_valid_model,
)

__all__ = [
    "create_model",
    "get_available_models",
    "has_aws_credentials",
    "is_valid_model",
    "get_default_model",
    "MODEL_CONFIG",
]
