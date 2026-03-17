"""Agent runnables for different sub-agent types.

Provides factory functions to create CompiledSubAgent instances
for dynamic local (LangGraph), foundry, and remote A2A agents.

Usage:
    from agent_common.agents.dynamic_agent import (
        DynamicLocalAgentRunnable,
        create_dynamic_local_subagent,
    )
    from agent_common.agents.foundry_agent import (
        FoundryLocalAgentRunnable,
        create_foundry_local_subagent,
    )
"""
