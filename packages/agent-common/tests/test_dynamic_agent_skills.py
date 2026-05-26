"""Tests for dynamic agent skills integration.

Verifies:
- Sub-agent with standard skills can read them at /skills/{name}/SKILL.md
- Sub-agent's /skills/ doesn't contain other agents' skills (isolation)
- Sandbox-enabled agent gets _build_graph called (not self._agent reuse)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_common.a2a.models import LocalLangGraphSubAgentConfig
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable
from agent_common.backends.skills_store import SkillsStoreBackend
from agent_common.models.skill import ResolvedSkill, SkillFile


class TestSkillsStoreIntegration:
    """Skills on virtual filesystem integration tests."""

    def test_resolved_skills_served_at_skills_path(self):
        """Sub-agent with standard skills can read them at /skills/{name}/SKILL.md."""
        skills = {
            "incident-triage": ResolvedSkill(
                name="incident-triage",
                description="Handle production incidents",
                body="# Steps\n1. Check alerts\n2. Triage",
                scope="standard",
                files=[SkillFile(path="scripts/check.py", content="print('checking')")],
            ),
        }
        backend = SkillsStoreBackend(skills)

        # Synchronously test — use asyncio.run for the async methods
        import asyncio

        # Read SKILL.md
        result = asyncio.run(backend.aread("/skills/incident-triage/SKILL.md"))
        assert result.file_data is not None
        assert "incident-triage" in result.file_data["content"]
        assert "Handle production incidents" in result.file_data["content"]
        assert "# Steps" in result.file_data["content"]

        # Read bundled file
        result = asyncio.run(backend.aread("/skills/incident-triage/scripts/check.py"))
        assert result.file_data is not None
        assert "print('checking')" in result.file_data["content"]

    def test_skills_isolation_between_agents(self):
        """Each agent only sees its own skills."""
        agent_a_skills = {
            "skill-a": ResolvedSkill(name="skill-a", description="Agent A skill", body="A body", scope="standard"),
        }
        agent_b_skills = {
            "skill-b": ResolvedSkill(name="skill-b", description="Agent B skill", body="B body", scope="standard"),
        }

        backend_a = SkillsStoreBackend(agent_a_skills)
        backend_b = SkillsStoreBackend(agent_b_skills)

        import asyncio

        # Agent A can see skill-a but not skill-b
        ls_a = asyncio.run(backend_a.als("/skills/"))
        names_a = [e["path"].strip("/") for e in ls_a.entries]
        assert "skill-a" in names_a
        assert "skill-b" not in names_a

        # Agent B can see skill-b but not skill-a
        ls_b = asyncio.run(backend_b.als("/skills/"))
        names_b = [e["path"].strip("/") for e in ls_b.entries]
        assert "skill-b" in names_b
        assert "skill-a" not in names_b

    def test_write_blocked(self):
        """Skills backend is read-only."""
        import asyncio

        backend = SkillsStoreBackend({"x": ResolvedSkill(name="x", description="X", body="X", scope="standard")})
        result = asyncio.run(backend.awrite("/skills/x/SKILL.md", "bad content"))
        assert result.error is not None
        assert "read-only" in result.error.lower()


class TestDynamicAgentEnsureAgentSkipsGraphForSandbox:
    """When sandbox_enabled=True, _ensure_agent should NOT build self._agent."""

    @pytest.mark.asyncio
    async def test_ensure_agent_skips_graph_when_sandbox_active(self):
        """_ensure_agent caches tools/prompt but skips building self._agent."""
        config = LocalLangGraphSubAgentConfig(
            name="sandboxed-agent",
            description="A sandboxed agent",
            system_prompt="You are a test agent.",
            sandbox_enabled=True,
        )

        pool = AsyncMock()  # Non-None = sandbox active

        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=MagicMock(),
            sandbox_pool=pool,
        )

        # Mock the graph building to track if it's called
        with patch(
            "agent_common.agents.dynamic_agent.build_sub_agent_graph",
            return_value=MagicMock(),
        ) as mock_build:
            await runnable._ensure_agent()

        # self._agent should NOT be set (sandbox builds per-invocation)
        assert runnable._agent is None
        # But cached state should be populated
        assert runnable._cached_tools is not None
        assert runnable._cached_system_prompt is not None
        # build_sub_agent_graph should NOT have been called
        mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_agent_builds_graph_when_no_sandbox(self):
        """Without sandbox, _ensure_agent builds self._agent normally."""
        config = LocalLangGraphSubAgentConfig(
            name="normal-agent",
            description="A normal agent",
            system_prompt="You are a test agent.",
            sandbox_enabled=False,
        )

        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=MagicMock(),
            sandbox_pool=None,
        )

        mock_graph = MagicMock()
        mock_graph.with_config = MagicMock(return_value=mock_graph)
        with patch(
            "agent_common.agents.dynamic_agent.build_sub_agent_graph",
            return_value=mock_graph,
        ) as mock_build:
            await runnable._ensure_agent()

        # self._agent should be set
        assert runnable._agent is mock_graph
        mock_build.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_agent_is_idempotent(self):
        """Second call to _ensure_agent is a no-op."""
        config = LocalLangGraphSubAgentConfig(
            name="normal-agent",
            description="A normal agent",
            system_prompt="You are a test agent.",
            sandbox_enabled=False,
        )

        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=MagicMock(),
            sandbox_pool=None,
        )

        mock_graph = MagicMock()
        with patch(
            "agent_common.agents.dynamic_agent.build_sub_agent_graph",
            return_value=mock_graph,
        ) as mock_build:
            await runnable._ensure_agent()
            await runnable._ensure_agent()

        # Only built once
        mock_build.assert_called_once()
