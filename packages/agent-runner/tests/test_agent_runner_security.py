"""Tests for AgentRunner security-critical methods.

Covers:
- _fetch_user_id_from_backend(): missing token, 401 response, missing 'id' field, valid response
- _extract_message_metadata(): extracts metadata from task, handles missing/malformed data
- Watch condition short-circuit: when condition_not_met, _stream_impl yields early without sub-agent call
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import Message, Part, Role

import agent.core as core


@pytest.fixture
def agent_runner():
    """Create an AgentRunner instance with minimal mocking."""
    mock_checkpointer = MagicMock(name="checkpointer")

    with patch("agent.core._create_checkpointer", return_value=(mock_checkpointer, None)):
        from agent.core import AgentRunner

        runner = AgentRunner()
        return runner


class TestFetchSubAgentConfig:
    """The job's stored tool whitelist enters the run here, in the catalogue's name space."""

    @pytest.mark.asyncio
    async def test_whitelist_is_sanitised_to_exposed_tool_names(self, agent_runner):
        """A stored wire name (dots and all) arrives as the name the catalogue exposes it under."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "type": "automated",
            "name": "okr-watch",
            "config_version": {
                "id": 7,
                "system_prompt": "You read OKRs.",
                "model": "claude-sonnet-4.6",
                "mcp_tools": ["authrion-atp-v1_okrs.v1.search_okrs", "gcal_list_events"],
            },
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            cfg = await agent_runner._fetch_sub_agent_config(7, "my-token")

        assert cfg["mcp_tools"] == ["authrion-atp-v1_okrs_v1_search_okrs", "gcal_list_events"]


class TestFetchUserIdFromBackend:
    """Security: user_id must come from verified backend response."""

    @pytest.mark.asyncio
    async def test_valid_response_returns_user_id(self, agent_runner):
        """A 200 response with 'id' field returns the user_id."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "user-uuid-123", "email": "a@b.com"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await agent_runner._fetch_user_id_from_backend("my-token")

        assert result == "user-uuid-123"
        mock_client.get.assert_awaited_once()
        call_kwargs = mock_client.get.call_args
        # Correct Authorization header
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer my-token"
        # Hits the /auth/me endpoint
        assert "/api/v1/auth/me" in call_kwargs.args[0]

    @pytest.mark.asyncio
    async def test_missing_id_field_returns_none(self, agent_runner):
        """If backend response is missing 'id', return None (don't trust partial data)."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"email": "a@b.com"}  # no 'id'
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await agent_runner._fetch_user_id_from_backend("my-token")

        assert result is None

    @pytest.mark.asyncio
    async def test_http_401_returns_none(self, agent_runner):
        """A 401 HTTP error returns None — token rejected."""
        import httpx as _httpx

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(
            side_effect=_httpx.HTTPStatusError("401", request=MagicMock(), response=mock_response)
        )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await agent_runner._fetch_user_id_from_backend("bad-token")

        assert result is None

    @pytest.mark.asyncio
    async def test_network_exception_returns_none(self, agent_runner):
        """Any unexpected exception during the HTTP call returns None (fail-safe)."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("network down"))

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await agent_runner._fetch_user_id_from_backend("any-token")

        assert result is None


class TestExtractMessageMetadata:
    """_extract_message_metadata pulls scheduler data out of Task.history[-1].metadata."""

    def _make_task(self, metadata=None):
        msg = MagicMock()
        msg.metadata = metadata
        task = MagicMock()
        task.history = [msg]
        return task

    def test_extracts_scheduler_metadata(self):
        from agent.core import _extract_message_metadata

        meta = {
            "sub_agent_id": 42,
            "job_type": "task",
            "scheduled_job_id": 7,
            "user_access_token": "tok",
        }
        task = self._make_task(metadata=meta)

        result = _extract_message_metadata(task)

        assert result["sub_agent_id"] == 42
        assert result["job_type"] == "task"
        assert result["scheduled_job_id"] == 7
        assert result["user_access_token"] == "tok"

    def test_empty_history_returns_empty_dict(self):
        from agent.core import _extract_message_metadata

        task = MagicMock()
        task.history = []

        result = _extract_message_metadata(task)

        assert result == {}

    def test_none_metadata_returns_empty_dict(self):
        from agent.core import _extract_message_metadata

        task = self._make_task(metadata=None)

        result = _extract_message_metadata(task)

        assert result == {}

    def test_no_metadata_attribute_returns_empty_dict(self):
        from agent.core import _extract_message_metadata

        msg = MagicMock(spec=[])  # no 'metadata' attribute
        task = MagicMock()
        task.history = [msg]

        result = _extract_message_metadata(task)

        assert result == {}

    def test_watch_metadata_included(self):
        from agent.core import _extract_message_metadata

        watch_cfg = {"check_tool": "ping", "check_args": {}, "condition_expr": "result > 0"}
        meta = {
            "job_type": "watch",
            "watch": watch_cfg,
            "scheduled_job_id": 99,
        }
        task = self._make_task(metadata=meta)

        result = _extract_message_metadata(task)

        assert result["job_type"] == "watch"
        assert result["watch"] == watch_cfg


class TestDispatchShapes:
    """Two shapes reach the runner now: run this sub-agent, or deliver this text.

    Nothing watch-specific — the scheduler decided whether to dispatch at all and wrote
    whatever needs saying before it did.
    """

    @staticmethod
    def _task(sub_agent_id: int | None) -> MagicMock:
        task = MagicMock()
        task.context_id = "ctx-shape"
        task.history = [MagicMock(metadata={"sub_agent_id": sub_agent_id, "scheduled_job_id": 10})]
        return task

    @staticmethod
    def _user_config() -> MagicMock:
        user_config = MagicMock()
        user_config.user_sub = "sub-1"
        user_config.access_token = MagicMock()
        user_config.access_token.get_secret_value.return_value = "bearer-token"
        return user_config

    async def _run(self, agent_runner, task, text: str) -> list[dict]:
        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")
        responses = []
        async for response in agent_runner._stream_impl(
            [Message(role=Role.ROLE_USER, parts=[Part(text=text)], message_id="msg-s")],
            self._user_config(),
            task,
        ):
            responses.append(response)
        return [json.loads(r.content) for r in responses if r.content.startswith("{")]

    @pytest.mark.asyncio
    async def test_no_sub_agent_delivers_the_text_it_was_given(self, agent_runner):
        agent_runner._execute_sub_agent = AsyncMock()
        items = await self._run(agent_runner, self._task(None), "Campaign 4821 stopped syncing.")

        agent_runner._execute_sub_agent.assert_not_awaited()
        success = next(i for i in items if i.get("scheduler_status") == "success")
        assert success["agent_message"] == "Campaign 4821 stopped syncing."

    @pytest.mark.asyncio
    async def test_a_sub_agent_runs_with_the_given_prompt(self, agent_runner):
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "triage", "sub_agent_id": 5}
        )
        agent_runner._execute_sub_agent = AsyncMock(return_value=("Handled it.", "completed"))
        items = await self._run(agent_runner, self._task(5), "Triage this: {}")

        prompt = agent_runner._execute_sub_agent.await_args[1]["prompt"]
        assert prompt == "Triage this: {}"  # passed through, not rebuilt here
        assert next(i for i in items if i.get("scheduler_status") == "success")["agent_message"] == ("Handled it.")

    @pytest.mark.asyncio
    async def test_a_stringified_sub_agent_id_still_runs_the_sub_agent(self, agent_runner):
        """A caller that stringifies the id must not silently produce a no-op success."""
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "debug", "sub_agent_id": 5}
        )
        agent_runner._execute_sub_agent = AsyncMock(return_value=("Investigated.", "completed"))
        items = await self._run(agent_runner, self._task("5"), "Investigate bug report abc.")

        agent_runner._fetch_sub_agent_config.assert_awaited_once()
        assert agent_runner._fetch_sub_agent_config.await_args[0][0] == 5
        assert next(i for i in items if i.get("scheduler_status") == "success")["agent_message"] == "Investigated."

    @pytest.mark.asyncio
    async def test_a_non_numeric_sub_agent_id_is_ignored(self, agent_runner):
        agent_runner._execute_sub_agent = AsyncMock()
        await self._run(agent_runner, self._task("not-an-id"), "text")
        agent_runner._execute_sub_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_dispatch_falls_back_to_the_default_instruction(self, agent_runner):
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "triage", "sub_agent_id": 5}
        )
        agent_runner._execute_sub_agent = AsyncMock(return_value=("done", "completed"))
        await self._run(agent_runner, self._task(5), "")

        assert agent_runner._execute_sub_agent.await_args[1]["prompt"] == "Execute your configured task."


class TestRemoteAgentContextPropagation:
    """Cross-service conversation adoption: the run task's contextId must ride the
    outgoing A2A message so the remote agent checkpoints the run's conversation under
    the id this side stores as scheduled_job_runs.conversation_id.

    Unchanged by the move of condition evaluation into the scheduler, but load-bearing
    and easy to drop silently: without these, an edit losing the `context_id` kwarg
    passes CI, and at runtime the remote agent checkpoints under a different id and
    orphans the run's thread.
    """

    @pytest.mark.asyncio
    async def test_remote_dispatch_carries_run_context_id(self, agent_runner):
        card_response = MagicMock()
        card_response.json.return_value = {"name": "Remote Agent"}
        card_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=card_response)

        captured = {}

        async def fake_collect(runnable, input_data):
            captured["input_data"] = input_data
            return "done", "completed"

        agent_runner._get_oauth2_client = MagicMock()

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("agent.core.make_a2a_async_runnable", return_value=MagicMock()),
            patch("agent.core._collect_stream_text", side_effect=fake_collect),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await agent_runner._run_remote_agent(
                sub_agent_cfg={
                    "name": "Remote Agent",
                    "agent_url": "https://remote.example",
                    "sub_agent_id": 5,
                },
                raw_a2a_messages=[],
                prompt="Do the thing.",
                user_access_token="tok",
                scheduled_job_id=10,
                scheduled_job_run_id=99,
                context_id="run-ctx-1",
            )

        assert result == ("done", "completed")
        assert captured["input_data"].orchestrator_conversation_id == "run-ctx-1"
        assert captured["input_data"].scheduled_job_id == 10

    @pytest.mark.asyncio
    async def test_execute_sub_agent_forwards_context_id_to_remote(self, agent_runner):
        agent_runner._run_remote_agent = AsyncMock(return_value=("ok", None))

        await agent_runner._execute_sub_agent(
            sub_agent_cfg={
                "type": "remote",
                "name": "Remote Agent",
                "agent_url": "https://remote.example",
            },
            prompt="p",
            user_access_token="tok",
            scheduled_job_id=10,
            scheduled_job_run_id=99,
            user_config=MagicMock(),
            context_id="run-ctx-1",
        )

        kwargs = agent_runner._run_remote_agent.await_args.kwargs
        assert kwargs["context_id"] == "run-ctx-1"


class TestDeliveryChannelFormatting:
    """A scheduled run is told how its delivery channel renders text.

    An interactive turn gets this from the client's `messageFormatting` metadata. A
    scheduled one has no client on the other end, so the scheduler resolves it from the
    job's delivery channel and sends it under the same key. Nothing downstream rewrites
    the agent's output, so losing this hand-off is what made Slack notifications arrive
    as literal '### heading' / '**bold**'.
    """

    @staticmethod
    def _task(formatting: str | None) -> MagicMock:
        meta: dict = {"sub_agent_id": 5, "scheduled_job_id": 10}
        if formatting is not None:
            meta["messageFormatting"] = formatting
        task = MagicMock()
        task.context_id = "ctx-fmt"
        task.history = [MagicMock(metadata=meta)]
        return task

    @staticmethod
    def _user_config() -> MagicMock:
        user_config = MagicMock()
        user_config.user_sub = "sub-1"
        user_config.access_token = MagicMock()
        user_config.access_token.get_secret_value.return_value = "bearer-token"
        return user_config

    async def _run(self, agent_runner, formatting: str | None) -> None:
        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "triage", "sub_agent_id": 5}
        )
        agent_runner._execute_sub_agent = AsyncMock(return_value=("done", "completed"))
        async for _ in agent_runner._stream_impl(
            [Message(role=Role.ROLE_USER, parts=[Part(text="Report on campaign 450.")], message_id="msg-f")],
            self._user_config(),
            self._task(formatting),
        ):
            pass

    @pytest.mark.asyncio
    async def test_the_channels_format_reaches_the_sub_agent(self, agent_runner):
        await self._run(agent_runner, "slack")
        assert agent_runner._execute_sub_agent.await_args.kwargs["message_formatting"] == "slack"

    @pytest.mark.asyncio
    async def test_an_absent_format_falls_back_to_markdown(self, agent_runner):
        await self._run(agent_runner, None)
        assert agent_runner._execute_sub_agent.await_args.kwargs["message_formatting"] == "markdown"

    @pytest.mark.asyncio
    async def test_execute_sub_agent_forwards_the_format_to_remote(self, agent_runner):
        agent_runner._run_remote_agent = AsyncMock(return_value=("ok", None))

        await agent_runner._execute_sub_agent(
            sub_agent_cfg={"type": "remote", "name": "Remote Agent", "agent_url": "https://remote.example"},
            prompt="p",
            user_access_token="tok",
            scheduled_job_id=10,
            scheduled_job_run_id=99,
            user_config=MagicMock(),
            context_id="run-ctx-1",
            message_formatting="slack",
        )

        assert agent_runner._run_remote_agent.await_args.kwargs["message_formatting"] == "slack"

    @pytest.mark.asyncio
    async def test_a_remote_agent_is_told_in_the_metadata(self, agent_runner):
        """A remote agent owns its system prompt, so the rules ride the A2A metadata.

        Not as an extra message: that would land in the remote's checkpointed
        conversation, where a later turn can read the instruction as part of the task.
        """
        card_response = MagicMock()
        card_response.json.return_value = {"name": "Remote Agent"}
        card_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=card_response)

        captured = {}

        async def fake_collect(runnable, input_data):
            captured["input_data"] = input_data
            return "done", "completed"

        agent_runner._get_oauth2_client = MagicMock()

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("agent.core.make_a2a_async_runnable", return_value=MagicMock()),
            patch("agent.core._collect_stream_text", side_effect=fake_collect),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await agent_runner._run_remote_agent(
                sub_agent_cfg={"name": "Remote Agent", "agent_url": "https://remote.example", "sub_agent_id": 5},
                raw_a2a_messages=[],
                prompt="Do the thing.",
                user_access_token="tok",
                scheduled_job_id=10,
                scheduled_job_run_id=99,
                context_id="run-ctx-1",
                message_formatting="slack",
            )

        input_data = captured["input_data"]
        assert input_data.message_formatting == "slack"
        # The dispatch text is untouched — no instruction message was appended.
        assert len(input_data.messages) == 1
        assert "Do the thing." in str(input_data.messages[0].content)

    @pytest.mark.asyncio
    async def test_markdown_is_not_worth_sending(self, agent_runner):
        card_response = MagicMock()
        card_response.json.return_value = {"name": "Remote Agent"}
        card_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=card_response)

        captured = {}

        async def fake_collect(runnable, input_data):
            captured["input_data"] = input_data
            return "done", "completed"

        agent_runner._get_oauth2_client = MagicMock()

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("agent.core.make_a2a_async_runnable", return_value=MagicMock()),
            patch("agent.core._collect_stream_text", side_effect=fake_collect),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await agent_runner._run_remote_agent(
                sub_agent_cfg={"name": "Remote Agent", "agent_url": "https://remote.example", "sub_agent_id": 5},
                raw_a2a_messages=[],
                prompt="Do the thing.",
                user_access_token="tok",
                scheduled_job_id=10,
                scheduled_job_run_id=99,
                context_id="run-ctx-1",
            )

        assert captured["input_data"].message_formatting is None

    @pytest.mark.asyncio
    async def test_execute_sub_agent_forwards_the_format_to_foundry(self, agent_runner):
        """Foundry's query API is the third writer, and it was the one left out."""
        agent_runner._run_foundry_agent = AsyncMock(return_value=("ok", None))

        await agent_runner._execute_sub_agent(
            sub_agent_cfg={"type": "foundry", "name": "Analyst", "sub_agent_id": 7},
            prompt="p",
            user_access_token="tok",
            scheduled_job_id=10,
            scheduled_job_run_id=99,
            user_config=MagicMock(),
            context_id="run-ctx-1",
            message_formatting="slack",
        )

        assert agent_runner._run_foundry_agent.await_args.kwargs["message_formatting"] == "slack"


class TestSubAgentSystemPrompt:
    """A local sub-agent is told the channel's rules through its assembled prompt.

    The stored system prompt cannot carry them: it is written once and reused, while the
    same agent may notify Slack for one job and the web console for the next.
    """

    def test_the_channels_rules_are_appended(self):
        prompt = core._build_sub_agent_system_prompt("You triage alerts.", "slack")

        assert prompt.startswith("You triage alerts.")
        assert 'format="slack"' in prompt
        assert "mrkdwn" in prompt
        # The response protocol still comes first — the rules are additive, not a swap.
        assert prompt.index("You triage alerts.") < prompt.index('format="slack"')

    def test_markdown_leaves_the_prompt_as_it_was(self):
        assert core._build_sub_agent_system_prompt("You triage alerts.", "markdown") == (
            core._build_sub_agent_system_prompt("You triage alerts.", "unknown-channel")
        )
        assert 'format=' not in core._build_sub_agent_system_prompt("You triage alerts.", "markdown")
