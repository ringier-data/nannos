"""Tests for DynamicToolDispatchMiddleware agent-list enhancement.

Covers:
- _build_agent_list: shared helper
- _enhance_system_prompt_agents: system-prompt replacement & fallback
- _enhance_task_tool_schema: tool-description replacement & fallback
- wrap_model_call / awrap_model_call: both pass enhanced system prompt
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import SystemMessage

from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
from app.models.config import GraphRuntimeContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(**overrides) -> GraphRuntimeContext:
    """Create a minimal GraphRuntimeContext for testing."""
    defaults = dict(
        user_id="u1",
        user_sub="sub1",
        name="Test",
        email="test@example.com",
        subagent_registry={},
    )
    defaults.update(overrides)
    return GraphRuntimeContext(**defaults)


def _make_system_message_with_agents(agent_lines: str) -> SystemMessage:
    """Build a SystemMessage whose last block contains the TASK_SYSTEM_PROMPT marker."""
    marker = DynamicToolDispatchMiddleware._SYSTEM_PROMPT_AGENT_MARKER
    text = f"## `task` (subagent spawner)\n\nYou have access to a `task` tool...\n\n{marker}{agent_lines}"
    return SystemMessage(
        content_blocks=[
            {"type": "text", "text": "Base system prompt here."},
            {"type": "text", "text": f"\n\n{text}"},
        ]
    )


def _make_task_tool_dict(agent_lines: str) -> dict:
    """Build a minimal OpenAI-format task tool dict with agent lines baked in."""
    marker = DynamicToolDispatchMiddleware._TOOL_DESC_AGENT_MARKER
    desc = f"Launch an ephemeral subagent...\n\n{marker}{agent_lines}\n\nWhen using the Task tool..."
    return {
        "type": "function",
        "function": {
            "name": "task",
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "subagent_type": {
                        "type": "string",
                        "description": "The agent type.",
                    },
                },
                "required": ["description", "subagent_type"],
            },
        },
    }


SAMPLE_REGISTRY = {
    "general-purpose": {"name": "general-purpose", "description": "GP agent", "runnable": MagicMock()},
    "file-analyzer": {"name": "file-analyzer", "description": "Analyzes files", "runnable": MagicMock()},
    "jira-agent": {"name": "jira-agent", "description": "Manages Jira tickets", "runnable": MagicMock()},
}


# ===========================================================================
# _build_agent_list
# ===========================================================================


class TestBuildAgentList:
    def test_builds_descriptions_and_names(self):
        descs, names = DynamicToolDispatchMiddleware._build_agent_list(SAMPLE_REGISTRY)
        assert names == ["general-purpose", "file-analyzer", "jira-agent"]
        assert descs == [
            '<agent name="general-purpose">\nGP agent\n</agent>',
            '<agent name="file-analyzer">\nAnalyzes files\n</agent>',
            '<agent name="jira-agent">\nManages Jira tickets\n</agent>',
        ]

    def test_empty_registry(self):
        descs, names = DynamicToolDispatchMiddleware._build_agent_list({})
        assert descs == []
        assert names == []

    def test_missing_description_uses_fallback(self):
        registry = {"my-agent": {"name": "my-agent", "runnable": MagicMock()}}
        descs, names = DynamicToolDispatchMiddleware._build_agent_list(registry)
        assert descs == ['<agent name="my-agent">\nAgent: my-agent\n</agent>']
        assert names == ["my-agent"]


# ===========================================================================
# _enhance_system_prompt_agents
# ===========================================================================


class TestEnhanceSystemPromptAgents:
    @pytest.fixture
    def middleware(self):
        return DynamicToolDispatchMiddleware()

    def test_replaces_general_purpose_with_all_agents(self, middleware):
        original = _make_system_message_with_agents("- general-purpose: GP agent")
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)

        result = middleware._enhance_system_prompt_agents(original, ctx)

        # The last block should now list all three agents in XML format
        last_text = result.content_blocks[-1]["text"]
        assert '<agent name="general-purpose">' in last_text
        assert '<agent name="file-analyzer">' in last_text
        assert '<agent name="jira-agent">' in last_text
        # Should NOT have duplicate marker entries
        marker = DynamicToolDispatchMiddleware._SYSTEM_PROMPT_AGENT_MARKER
        assert last_text.count(marker) == 1

    def test_returns_none_when_system_message_is_none(self, middleware):
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)
        assert middleware._enhance_system_prompt_agents(None, ctx) is None

    def test_returns_original_when_registry_empty(self, middleware):
        original = _make_system_message_with_agents("- general-purpose: GP agent")
        ctx = _make_context(subagent_registry={})
        result = middleware._enhance_system_prompt_agents(original, ctx)
        assert result is original

    def test_fallback_appends_when_marker_missing(self, middleware):
        """When the marker is absent, should append as a new block."""
        original = SystemMessage(
            content_blocks=[
                {"type": "text", "text": "No marker here — just a normal prompt."},
            ]
        )
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)

        result = middleware._enhance_system_prompt_agents(original, ctx)

        combined = "".join(b["text"] for b in result.content_blocks if isinstance(b, dict))
        assert '<agent name="file-analyzer">' in combined
        assert '<agent name="jira-agent">' in combined

    def test_handles_multiple_agent_lines(self, middleware):
        """Replace when the original already had multiple lines."""
        original = _make_system_message_with_agents("- general-purpose: GP agent\n- old-agent: Old description")
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)

        result = middleware._enhance_system_prompt_agents(original, ctx)
        last_text = result.content_blocks[-1]["text"]
        assert "old-agent" not in last_text
        assert '<agent name="jira-agent">' in last_text


# ===========================================================================
# _enhance_task_tool_schema
# ===========================================================================


class TestEnhanceTaskToolSchema:
    @pytest.fixture
    def middleware(self):
        return DynamicToolDispatchMiddleware()

    def test_replaces_agent_list_in_description(self, middleware):
        original = _make_task_tool_dict("- general-purpose: GP agent")
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)

        result = middleware._enhance_task_tool_schema(original, ctx)

        desc = result["function"]["description"]
        # All three agents should be listed in XML format
        assert '<agent name="general-purpose">' in desc
        assert '<agent name="file-analyzer">' in desc
        assert '<agent name="jira-agent">' in desc
        # Only one occurrence of the marker (no duplication)
        marker = DynamicToolDispatchMiddleware._TOOL_DESC_AGENT_MARKER
        assert desc.count(marker) == 1

    def test_updates_enum(self, middleware):
        original = _make_task_tool_dict("- general-purpose: GP agent")
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)

        result = middleware._enhance_task_tool_schema(original, ctx)
        enum = result["function"]["parameters"]["properties"]["subagent_type"]["enum"]
        assert enum == ["general-purpose", "file-analyzer", "jira-agent"]

    def test_returns_original_when_registry_empty(self, middleware):
        original = _make_task_tool_dict("- general-purpose: GP agent")
        ctx = _make_context(subagent_registry={})
        result = middleware._enhance_task_tool_schema(original, ctx)
        assert result is original

    def test_fallback_appends_when_marker_missing(self, middleware):
        """When the marker is absent, append 'Available agents:' section."""
        tool = {
            "type": "function",
            "function": {
                "name": "task",
                "description": "Some custom task tool with no marker.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "subagent_type": {"type": "string"},
                    },
                },
            },
        }
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)

        result = middleware._enhance_task_tool_schema(tool, ctx)
        desc = result["function"]["description"]
        assert "Available agents:" in desc
        assert '<agent name="file-analyzer">' in desc

    def test_replaces_multi_line_agent_block(self, middleware):
        """Verify replacement works when original has multiple agent lines."""
        original = _make_task_tool_dict("- general-purpose: GP agent\n- old-agent: Old description")
        ctx = _make_context(subagent_registry=SAMPLE_REGISTRY)

        result = middleware._enhance_task_tool_schema(original, ctx)
        desc = result["function"]["description"]
        assert "old-agent" not in desc
        assert '<agent name="jira-agent">' in desc


# ===========================================================================
# wrap_model_call / awrap_model_call integration
# ===========================================================================


class TestWrapModelCallSystemPrompt:
    """Verify that both sync and async wrap_model_call pass the enhanced system prompt."""

    @pytest.fixture
    def middleware(self):
        return DynamicToolDispatchMiddleware()

    @pytest.fixture
    def user_context(self):
        return _make_context(subagent_registry=SAMPLE_REGISTRY)

    @pytest.fixture
    def system_message(self):
        return _make_system_message_with_agents("- general-purpose: GP agent")

    def _make_request(self, system_message, user_context):
        request = MagicMock()
        request.runtime.context = user_context
        request.system_message = system_message
        request.messages = []
        request.tools = []

        # override should return a new mock that echoes kwargs
        def _override(**kwargs):
            new_req = MagicMock()
            for k, v in kwargs.items():
                setattr(new_req, k, v)
            return new_req

        request.override = _override
        return request

    def test_sync_passes_enhanced_system_prompt(self, middleware, user_context, system_message):
        request = self._make_request(system_message, user_context)
        captured = {}

        def handler(req):
            captured["system_message"] = req.system_message
            return MagicMock()

        middleware.wrap_model_call(request, handler)

        sm = captured["system_message"]
        combined = "".join(b["text"] for b in sm.content_blocks if isinstance(b, dict) and b.get("type") == "text")
        assert '<agent name="file-analyzer">' in combined
        assert '<agent name="jira-agent">' in combined

    @pytest.mark.asyncio
    async def test_async_passes_enhanced_system_prompt(self, middleware, user_context, system_message):
        request = self._make_request(system_message, user_context)
        captured = {}

        async def handler(req):
            captured["system_message"] = req.system_message
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        sm = captured["system_message"]
        combined = "".join(b["text"] for b in sm.content_blocks if isinstance(b, dict) and b.get("type") == "text")
        assert '<agent name="file-analyzer">' in combined
        assert '<agent name="jira-agent">' in combined
