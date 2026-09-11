"""Shared test support for the orchestrator agent.

Not a test package — nothing here is collected. These modules are imported by
both the normal suite and the (currently ignored) integration suite:

- ``extraction``: read orchestrator behaviour out of a finished LangGraph turn.
- ``mock_subagents``: dispatchable sub-agent doubles for routing tests.
"""
