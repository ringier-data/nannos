"""What a scheduled run executes: the agent's approved default version, skills included.

Two divergences from the orchestrator, one cause. The orchestrator reads
``/sub-agents/activated`` (joined on ``default_version``) and passes the version's
``skills`` into ``LocalLangGraphSubAgentConfig``. agent-runner read ``/sub-agents/{id}``
with no ``version`` (which answers with ``current_version`` — the newest draft) and copied
named fields out of it, ``skills`` not among them. So the same agent behaved differently
depending on whether a person or the scheduler called it, and once someone saved a draft,
the next unattended run used it before anyone approved it.

The parity these tests pin cannot be asserted against the orchestrator's projection
directly — the two live in separate packages with separate environments — so they pin
agent-runner's side of it: the version fetched, the skills carried, and the fields the
built config ends up with.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.core import UnapprovedSubAgentError, _local_sub_agent_config

APPROVED_SKILL = {
    "name": "weekly-digest",
    "description": "How the digest is laid out.",
    "body": "# Weekly digest\n\nLead with the number that moved.",
    "files": [{"path": "template.md", "content": "..."}],
    "inline": True,
    # The console's resolved shape carries registry bookkeeping the runtime does not model.
    "registry_id": 12,
    "scope": "standalone",
}


def _record(*, version: int, default_version: int | None, skills: list | None = None, **cfg) -> dict:
    """A ``GET /api/v1/sub-agents/{id}`` answer whose embedded config is *version*."""
    return {
        "id": 7,
        "type": "local",
        "name": "digest-writer",
        "current_version": max(version, default_version or 0),
        "default_version": default_version,
        "config_version": {
            "id": 100 + version,
            "version": version,
            "description": f"v{version}",
            "system_prompt": f"Prompt of v{version}.",
            "model": "claude-sonnet-4.6",
            "mcp_tools": ["gcal_list_events"],
            "skills": skills or [],
            **cfg,
        },
    }


@pytest.fixture
def agent_runner():
    mock_checkpointer = MagicMock(name="checkpointer")
    with patch("agent.core._create_checkpointer", return_value=(mock_checkpointer, None)):
        from agent.core import AgentRunner

        return AgentRunner()


def _console(responses: list[dict]) -> tuple[MagicMock, AsyncMock]:
    """An httpx client that answers successive GETs from *responses*, in order."""
    answers = []
    for body in responses:
        resp = MagicMock()
        resp.json.return_value = body
        resp.raise_for_status = MagicMock()
        answers.append(resp)
    client = AsyncMock()
    client.get = AsyncMock(side_effect=answers)
    cls = MagicMock()
    cls.return_value.__aenter__ = AsyncMock(return_value=client)
    cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return cls, client


class TestApprovedVersionIsFetched:
    async def test_a_draft_newer_than_the_default_is_not_what_runs(self, agent_runner):
        """current_version=3 is a draft; default_version=2 is approved. The run gets v2."""
        cls, client = _console(
            [
                _record(version=3, default_version=2),
                _record(version=2, default_version=2, skills=[APPROVED_SKILL]),
            ]
        )
        with patch("httpx.AsyncClient", cls):
            cfg = await agent_runner._fetch_sub_agent_config(7, "tok")

        assert cfg["sub_agent_config_version_id"] == 102
        assert cfg["system_prompt"] == "Prompt of v2."
        # The second read asked for the approved version by number.
        second = client.get.call_args_list[1]
        assert second.kwargs["params"] == {"version": 2}
        assert second.kwargs["headers"] == {"Authorization": "Bearer tok"}

    async def test_one_read_when_the_current_version_is_the_approved_one(self, agent_runner):
        cls, client = _console([_record(version=2, default_version=2)])
        with patch("httpx.AsyncClient", cls):
            cfg = await agent_runner._fetch_sub_agent_config(7, "tok")

        assert cfg["sub_agent_config_version_id"] == 102
        assert client.get.await_count == 1

    async def test_an_agent_with_no_approved_version_does_not_run(self, agent_runner):
        """Only drafts exist: nothing a reviewer signed off, so nothing to execute."""
        cls, client = _console([_record(version=1, default_version=None)])
        with (
            patch("httpx.AsyncClient", cls),
            pytest.raises(UnapprovedSubAgentError, match="no approved version") as exc,
        ):
            await agent_runner._fetch_sub_agent_config(7, "tok")
        assert client.get.await_count == 1
        # The failure record is built before a record exists to take the name from.
        assert exc.value.sub_agent_name == "digest-writer"
        assert "'digest-writer' (id 7)" in str(exc.value)

    async def test_a_null_name_falls_back_to_the_id(self, agent_runner):
        record = _record(version=1, default_version=None)
        record["name"] = None
        cls, _ = _console([record])
        with patch("httpx.AsyncClient", cls), pytest.raises(UnapprovedSubAgentError, match="'sub-agent-7'"):
            await agent_runner._fetch_sub_agent_config(7, "tok")

    async def test_an_approved_version_that_cannot_be_read_does_not_run(self, agent_runner):
        """The console answers a version it cannot join with ``config_version: null`` and a
        200. Taken at face value that would be an agent with an empty prompt and no tools."""
        unreadable = _record(version=3, default_version=2)
        unreadable["config_version"] = None
        cls, client = _console([_record(version=3, default_version=2), unreadable])
        with (
            patch("httpx.AsyncClient", cls),
            pytest.raises(UnapprovedSubAgentError, match="version 2 .* could not be read"),
        ):
            await agent_runner._fetch_sub_agent_config(7, "tok")
        assert client.get.await_count == 2

    async def test_a_re_read_answering_with_the_wrong_version_does_not_run(self, agent_runner):
        """A console that ignores the query parameter hands the draft back again."""
        cls, _ = _console([_record(version=3, default_version=2), _record(version=3, default_version=2)])
        with patch("httpx.AsyncClient", cls), pytest.raises(UnapprovedSubAgentError, match="could not be read"):
            await agent_runner._fetch_sub_agent_config(7, "tok")


class TestSkillsAreCarried:
    async def test_the_versions_skills_reach_the_fetched_record(self, agent_runner):
        cls, _ = _console([_record(version=2, default_version=2, skills=[APPROVED_SKILL])])
        with patch("httpx.AsyncClient", cls):
            cfg = await agent_runner._fetch_sub_agent_config(7, "tok")

        assert cfg["skills"] == [APPROVED_SKILL]

    def test_the_built_config_carries_them_as_skill_definitions(self):
        """The same field the orchestrator sets (``skills=cv.skills``), from the same wire shape."""
        cfg = {
            "type": "local",
            "name": "digest-writer",
            "description": "v2",
            "system_prompt": "Prompt of v2.",
            "mcp_tools": ["gcal_list_events"],
            "sub_agent_id": 7,
            "sub_agent_config_version_id": 102,
            "skills": [APPROVED_SKILL],
            "enable_thinking": True,
            "thinking_level": "medium",
            "sandbox_enabled": True,
        }
        config = _local_sub_agent_config(cfg, model_name="claude-sonnet-4.6", message_formatting="slack")

        assert [s.name for s in config.skills] == ["weekly-digest"]
        skill = config.skills[0]
        assert skill.inline is True
        assert skill.body.startswith("# Weekly digest")
        assert [f.path for f in skill.files] == ["template.md"]
        assert config.sub_agent_config_version_id == 102
        assert config.sub_agent_id == 7
        assert config.mcp_tools == ["gcal_list_events"]
        assert config.enable_thinking is True
        assert config.thinking_level == "medium"
        assert config.sandbox_enabled is True
        assert config.interactive is True
        assert config.all_tools is False
        assert config.system_prompt.startswith("Prompt of v2.")

    def test_no_skills_is_an_empty_list_not_a_failure(self):
        cfg = {"name": "plain", "system_prompt": "Do the thing.", "sub_agent_id": 1}
        config = _local_sub_agent_config(cfg, model_name="claude-sonnet-4.6", message_formatting="markdown")
        assert config.skills == []
        assert config.thinking_level is None


class TestOtherFieldsTheOrchestratorSets:
    """``_to_user`` in the orchestrator's registry is the reference projection."""

    def test_thinking_fields_pass_through_as_stored(self):
        """The runnable picks its response-format strategy from ``thinking_level`` alone,
        so a stored level with thinking off must read the same for both callers."""
        cfg = {"name": "plain", "system_prompt": "x", "enable_thinking": None, "thinking_level": "high"}
        config = _local_sub_agent_config(cfg, model_name="claude-sonnet-4.6", message_formatting="markdown")
        assert config.enable_thinking is None
        assert config.thinking_level == "high"

    def test_an_automated_agent_is_not_interactive(self):
        cfg = {"type": "automated", "name": "nightly", "system_prompt": "x"}
        config = _local_sub_agent_config(cfg, model_name="claude-sonnet-4.6", message_formatting="markdown")
        assert config.interactive is False

    async def test_an_embed_bound_agent_with_no_tool_list_takes_the_whole_catalogue(self, agent_runner):
        """ADR-0006. The record's ``all_tools`` is what ``_is_full_catalogue_agent`` reads."""
        from agent.core import _is_full_catalogue_agent

        bound = _record(version=1, default_version=1, mcp_tools=[])
        bound["embed_binding"] = {"authority": "https://embed.example"}
        cls, _ = _console([bound])
        with patch("httpx.AsyncClient", cls):
            cfg = await agent_runner._fetch_sub_agent_config(7, "tok")

        assert cfg["all_tools"] is True
        assert _is_full_catalogue_agent(cfg)
        assert _local_sub_agent_config(cfg, model_name="m", message_formatting="markdown").all_tools is True

    async def test_a_tool_list_or_no_binding_keeps_the_whitelist(self, agent_runner):
        bound_with_list = _record(version=1, default_version=1)
        bound_with_list["embed_binding"] = {"authority": "https://embed.example"}
        unbound_empty = _record(version=1, default_version=1, mcp_tools=[])
        cls, _ = _console([bound_with_list, unbound_empty])
        with patch("httpx.AsyncClient", cls):
            first = await agent_runner._fetch_sub_agent_config(7, "tok")
            second = await agent_runner._fetch_sub_agent_config(7, "tok")
        assert first["all_tools"] is False
        assert second["all_tools"] is False
