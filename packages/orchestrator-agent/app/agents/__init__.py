"""
Concrete Agent Implementations.

This module contains specific agent implementations that use the A2A protocol:
- DynamicLocalAgentRunnable: User-configurable local agents with LangGraph
- FileAnalyzerRunnable: Specialized file analysis agent
- FoundryRunnable: Palantir Foundry integration agent

Note: Import these directly from their modules to avoid circular dependencies.
Do NOT import from this __init__.py file.

Usage:
    from app.agents.dynamic_agent import DynamicLocalAgentRunnable, create_dynamic_local_subagent
    from app.agents.file_analyzer import FileAnalyzerRunnable, create_file_analyzer_subagent
    from app.agents.foundry_agent import FoundryRunnable, create_foundry_subagent
"""

# NOTE: We do NOT eagerly import any agents here to avoid circular dependencies
# All imports must be done directly from their respective modules

__all__ = [
    # No exports - import directly from submodules
]
