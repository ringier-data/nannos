"""Tests for AgentRunner security-critical methods.

Covers:
- _fetch_user_id_from_backend(): missing token, 401 response, missing 'id' field, valid response
- _extract_message_metadata(): extracts metadata from task, handles missing/malformed data
- Watch condition short-circuit: when condition_not_met, _stream_impl yields early without sub-agent call
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import Message, Part, Role, TaskState
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

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
        agent_runner._execute_sub_agent = AsyncMock(return_value=core.SubAgentRun(message="Handled it.", task_state="completed"))
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
        agent_runner._execute_sub_agent = AsyncMock(return_value=core.SubAgentRun(message="Investigated.", task_state="completed"))
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
        agent_runner._execute_sub_agent = AsyncMock(return_value=core.SubAgentRun(message="done", task_state="completed"))
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

        async def fake_collect(runnable, input_data, config=None):
            captured["input_data"] = input_data
            return core.SubAgentRun(message="done", task_state="completed")

        agent_runner._get_oauth2_client = MagicMock()

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("agent.core.make_a2a_async_runnable", return_value=MagicMock()),
            patch("agent.core._collect_sub_agent_run", side_effect=fake_collect),
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

        assert result == core.SubAgentRun(message="done", task_state="completed")
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
        agent_runner._execute_sub_agent = AsyncMock(return_value=core.SubAgentRun(message="done", task_state="completed"))
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

        async def fake_collect(runnable, input_data, config=None):
            captured["input_data"] = input_data
            return core.SubAgentRun(message="done", task_state="completed")

        agent_runner._get_oauth2_client = MagicMock()

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("agent.core.make_a2a_async_runnable", return_value=MagicMock()),
            patch("agent.core._collect_sub_agent_run", side_effect=fake_collect),
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

        async def fake_collect(runnable, input_data, config=None):
            captured["input_data"] = input_data
            return core.SubAgentRun(message="done", task_state="completed")

        agent_runner._get_oauth2_client = MagicMock()

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("agent.core.make_a2a_async_runnable", return_value=MagicMock()),
            patch("agent.core._collect_sub_agent_run", side_effect=fake_collect),
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


class TestParkedOnAuthorization:
    """A run blocked on the OWNER's credential asks, instead of lying either way.

    Before this, the gateway's `need-credentials` reached the model as an ordinary tool
    message and the model's paraphrase decided the run's fate: `failed` spent the job's
    max_failures budget on a condition no retry can fix, `completed` left a green run
    that did nothing. See
    docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.
    """

    AUTH_PAYLOAD = {
        "requires_auth": True,
        "auth_requirement": {
            "service": "github",
            "auth_methods": [{"method": "oauth2", "auth_url": "https://github.example/authorize"}],
        },
    }

    @staticmethod
    def _task(task_id: str = "outer-task-1") -> MagicMock:
        task = MagicMock()
        task.id = task_id
        task.context_id = "ctx-parked"
        task.history = [
            MagicMock(metadata={"sub_agent_id": 5, "scheduled_job_id": 10, "scheduled_job_run_id": 77})
        ]
        return task

    @staticmethod
    def _user_config() -> MagicMock:
        user_config = MagicMock()
        user_config.user_sub = "sub-1"
        user_config.access_token = MagicMock()
        user_config.access_token.get_secret_value.return_value = "bearer-token"
        return user_config

    async def _run(self, agent_runner, task, parts) -> tuple[list[dict], list]:
        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "triage", "sub_agent_id": 5}
        )
        responses = []
        async for response in agent_runner._stream_impl(
            [Message(role=Role.ROLE_USER, parts=parts, message_id="msg-p")],
            self._user_config(),
            task,
        ):
            responses.append(response)
        return [json.loads(r.content) for r in responses if r.content.startswith("{")], responses

    @pytest.mark.asyncio
    async def test_a_parked_run_reports_the_ask_and_leaves_its_task_open(self, agent_runner):
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(
                message="I need access to GitHub.",
                task_state="auth_required",
                auth_payload=self.AUTH_PAYLOAD,
            )
        )
        items, responses = await self._run(agent_runner, self._task(), [Part(text="Do the thing.")])

        parked = next(i for i in items if i.get("scheduler_status") == "auth_required")
        assert parked["auth_payload"] == self.AUTH_PAYLOAD
        # The OUTER task id: the only one anything outside this process can address.
        assert parked["parked_task_id"] == "outer-task-1"
        # Logical, never a URL — the client resolves its own console-backend base, so
        # nothing off a webhook is ever trusted as an address.
        assert parked["reply_to"]["service"] == "console-backend"
        assert parked["reply_to"]["endpoint"] == "scheduled_run_resume"
        assert parked["reply_to"]["scheduled_job_run_id"] == 77

        # Non-terminal on purpose: the A2A handler accepts the owner's answer only
        # while the task it is addressed to has not reached a terminal state.
        assert responses[-1].state == TaskState.TASK_STATE_AUTH_REQUIRED

    @pytest.mark.asyncio
    async def test_the_ask_still_parses_as_the_payload_every_client_already_reads(self, agent_runner):
        """The ask rides INSIDE the scheduler payload, never replacing it.

        `getSchedulerPayload` in each chat client does `JSON.parse` on part zero and
        gives up silently when that fails. A status message shaped only by the
        in-task-auth extension would put prose there and the whole notification would
        vanish in three clients at once.
        """
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(
                message="I need access to GitHub.",
                task_state="auth_required",
                auth_payload=self.AUTH_PAYLOAD,
            )
        )
        items, _ = await self._run(agent_runner, self._task(), [Part(text="Do the thing.")])
        parked = next(i for i in items if i.get("scheduler_status") == "auth_required")
        # The fields a client that never learned any of this still reads.
        assert parked["agent_message"] == "I need access to GitHub."
        assert parked["user_sub"] == "sub-1"
        assert parked["scheduled_job_id"] == 10

    @pytest.mark.asyncio
    async def test_a_park_with_no_ask_is_not_a_park(self, agent_runner):
        """Parking with nothing to ask would stop the job on an unanswerable question.

        It is reported as a FAILURE, not as a success. The owner has nothing to click,
        so holding the schedule would stop the job on a question nobody was asked — but
        recording it green would reset ``consecutive_failures`` on a run that did
        nothing, which is the silently-green run this ADR exists to abolish. A failure
        at least retries, and counts.
        """
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(message="blocked", task_state="auth_required", auth_payload=None)
        )
        items, responses = await self._run(agent_runner, self._task(), [Part(text="Do the thing.")])
        assert not any(i.get("scheduler_status") == "auth_required" for i in items)
        assert responses[-1].state == TaskState.TASK_STATE_FAILED
        failed = next(i for i in items if i.get("scheduler_status") == "failed")
        assert failed["error_message"] == "Stopped for authorization but produced no ask to answer"

    @pytest.mark.asyncio
    async def test_an_authorization_answer_resumes_the_task_the_run_parked(self, agent_runner):
        """The answer must find the parked task, not open a second one beside it.

        The sub-agent's task id is derived from (context, run) rather than stored, so
        the resume recomputes the same id the original run proposed. Opening a new task
        on a parked thread is what the executor rejects.
        """
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(message="Done.", task_state="completed")
        )
        answer = Part(data=ParseDict({"authorization": {"decision": "approved"}}, Value()))
        await self._run(agent_runner, self._task(), [answer, Part(text="I authorized it.")])

        resume_task_id = agent_runner._execute_sub_agent.await_args.kwargs["resume_task_id"]
        assert resume_task_id == core.scheduled_run_task_id("ctx-parked")

    @pytest.mark.asyncio
    async def test_ordinary_work_opens_a_task_rather_than_resuming_one(self, agent_runner):
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(message="Done.", task_state="completed")
        )
        await self._run(agent_runner, self._task(), [Part(text="Do the thing.")])
        assert agent_runner._execute_sub_agent.await_args.kwargs["resume_task_id"] is None


class TestAFailedSubAgentIsNotGreen:
    """A sub-agent that blew up must not be recorded as a successful run.

    Regression: moving the LangGraph path behind ``LocalA2ARunnable.astream`` changed how
    a crash arrives. That method catches every exception and yields an ErrorEvent rather
    than raising, so ``_stream_impl``'s except branch — the only thing that used to write
    `failed` — stopped being reached for a local agent. The run was then recorded green
    with the traceback sitting in the result text, which is the exact failure mode
    ADR-0009 exists to remove. Seen in production as a "Success" row reading
    "Error: the greenlet library is required to use this function".
    """

    @pytest.mark.asyncio
    async def test_an_errored_sub_agent_run_is_recorded_failed(self, agent_runner):
        task = MagicMock()
        task.id = "outer-task-2"
        task.context_id = "ctx-failed"
        task.history = [MagicMock(metadata={"sub_agent_id": 5, "scheduled_job_id": 10})]

        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "triage", "sub_agent_id": 5}
        )
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(
                message="Error: No module named 'greenlet'", task_state="failed"
            )
        )

        responses = []
        async for r in agent_runner._stream_impl(
            [Message(role=Role.ROLE_USER, parts=[Part(text="Do the thing.")], message_id="m")],
            TestParkedOnAuthorization._user_config(),
            task,
        ):
            responses.append(r)

        items = [json.loads(r.content) for r in responses if r.content.startswith("{")]
        result = items[-1]
        assert result["scheduler_status"] == "failed"
        # Under the key the scheduler reads a failure from, not only in the prose.
        assert "greenlet" in result["error_message"]
        assert responses[-1].state == TaskState.TASK_STATE_FAILED

    @pytest.mark.asyncio
    async def test_a_completed_run_is_still_success(self, agent_runner):
        task = MagicMock()
        task.id = "outer-task-3"
        task.context_id = "ctx-ok"
        task.history = [MagicMock(metadata={"sub_agent_id": 5, "scheduled_job_id": 10})]

        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "triage", "sub_agent_id": 5}
        )
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(message="All done.", task_state="completed")
        )

        responses = []
        async for r in agent_runner._stream_impl(
            [Message(role=Role.ROLE_USER, parts=[Part(text="Do the thing.")], message_id="m")],
            TestParkedOnAuthorization._user_config(),
            task,
        ):
            responses.append(r)

        result = [json.loads(r.content) for r in responses if r.content.startswith("{")][-1]
        assert result["scheduler_status"] == "success"
        assert "error_message" not in result


class TestEmptyWhitelistMeaning:
    """An empty tool list means "everything" for general-purpose, and ONLY for it.

    The general-purpose agent is configured with no whitelist, and in a conversation the
    orchestrator reads that as the whole registry (handed over as a lazy catalog). A
    scheduled run used to read the same empty list as "no tools", so the agent replied
    that the tools it was asked to use do not exist — same agent, same configuration,
    opposite capability depending on who started it.

    Every OTHER agent keeps the existing meaning: an empty list is an empty list. A
    purpose-built sub-agent must not silently acquire the whole gateway because nobody
    filled its whitelist in.
    """

    def test_general_purpose_takes_the_full_catalogue(self):
        assert core._is_full_catalogue_agent({"name": "general-purpose", "mcp_tools": []}) is True

    def test_an_ordinary_agent_does_not(self):
        assert core._is_full_catalogue_agent({"name": "qa-github-check", "mcp_tools": []}) is False
        assert core._is_full_catalogue_agent({"name": "triage"}) is False

    def test_the_backend_can_still_open_the_list_explicitly(self):
        """Mirrors the orchestrator's ``config.all_tools`` (ADR-0006, embed-bound agents)."""
        assert core._is_full_catalogue_agent({"name": "cockpit", "all_tools": True}) is True


class TestSchedulerMetadataOnAResume:
    """The ids come from the message being handled, not from the end of task history.

    Reading ``task.history[-1]`` was correct exactly while every dispatch opened a fresh
    task. The moment one CONTINUES a task — which is how an authorization answer reaches
    a parked run — the last history entry is the AGENT's own message (the auth_required
    payload it published), not the user's new one. Every id then came back None, the run
    took the no-sub-agent branch, and it echoed the authorization answer back as its
    result: a "successful" run that did none of the work it was resumed to do.
    """

    @staticmethod
    def _continued_task() -> MagicMock:
        task = MagicMock()
        task.id = "outer-task-9"
        task.context_id = "ctx-resume"
        # What a continued task looks like: the original dispatch, then the agent's own
        # parked status message, which carries no scheduler metadata.
        task.history = [
            MagicMock(metadata={"sub_agent_id": 5, "scheduled_job_id": 10, "scheduled_job_run_id": 77}),
            MagicMock(metadata=None),
        ]
        return task

    @pytest.mark.asyncio
    async def test_the_resume_finds_its_job_run_and_parked_task(self, agent_runner):
        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "general-purpose", "sub_agent_id": 5}
        )
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(message="Retried and done.", task_state="completed")
        )

        answer = Message(
            role=Role.ROLE_USER,
            parts=[
                Part(data=ParseDict({"authorization": {"decision": "approved"}}, Value())),
                Part(text="I have completed the authorization."),
            ],
            message_id="msg-resume",
        )
        # The metadata rides the message being handled, exactly as the scheduler sends it.
        answer.metadata.update({"sub_agent_id": 5, "scheduled_job_id": 10, "scheduled_job_run_id": 77})

        responses = []
        async for r in agent_runner._stream_impl([answer], TestParkedOnAuthorization._user_config(), self._continued_task()):
            responses.append(r)

        # The sub-agent actually ran, addressed to the task the earlier run parked.
        agent_runner._execute_sub_agent.assert_awaited_once()
        kwargs = agent_runner._execute_sub_agent.await_args.kwargs
        assert kwargs["resume_task_id"] == core.scheduled_run_task_id("ctx-resume")
        assert kwargs["scheduled_job_id"] == 10

        result = [json.loads(r.content) for r in responses if r.content.startswith("{")][-1]
        # Not the answer echoed back at the user.
        assert result["agent_message"] == "Retried and done."
        assert result["scheduled_job_id"] == 10


class TestASecondAuthorizationInOneRun:
    """One authorization can lead straight to another, and the two run ids diverge.

    Every link is a new run row on the SAME context, so there is ONE sub-agent task for
    the whole chain — which is why the task id is keyed on the context and not on a run.
    Keying it per run gave the second answer an id nothing had ever opened, and the
    resume died on "Task ... not found".

    The run ids still diverge for correlation: the payload, and above all the reply
    target of a follow-up ask, must name the run that is waiting NOW. Sending the parked
    run's id there made the second card address a run already answered, refused with
    "That scheduled run is no longer waiting".
    """

    @staticmethod
    def _resume_task(parked_run_id: int, new_run_id: int) -> MagicMock:
        task = MagicMock()
        task.id = "outer-task-chain"
        task.context_id = "ctx-chain"
        task.history = [MagicMock(metadata=None)]
        return task

    @pytest.mark.asyncio
    async def test_the_follow_up_ask_points_at_the_run_that_is_now_waiting(self, agent_runner):
        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")
        agent_runner._fetch_sub_agent_config = AsyncMock(
            return_value={"type": "automated", "name": "general-purpose", "sub_agent_id": 5}
        )
        # The resumed run parks AGAIN on a second service.
        agent_runner._execute_sub_agent = AsyncMock(
            return_value=core.SubAgentRun(
                message="Now I also need Jira.",
                task_state="auth_required",
                auth_payload=TestParkedOnAuthorization.AUTH_PAYLOAD,
            )
        )

        answer = Message(
            role=Role.ROLE_USER,
            parts=[Part(data=ParseDict({"authorization": {"decision": "approved"}}, Value()))],
            message_id="msg-chain",
        )
        answer.metadata.update(
            {
                "sub_agent_id": 5,
                "scheduled_job_id": 10,
                "scheduled_job_run_id": 653,  # the NEW run carrying the continued work
            }
        )

        responses = []
        async for r in agent_runner._stream_impl(
            [answer], TestParkedOnAuthorization._user_config(), self._resume_task(652, 653)
        ):
            responses.append(r)

        # Continued the task run 652 parked...
        assert agent_runner._execute_sub_agent.await_args.kwargs["resume_task_id"] == (
            core.scheduled_run_task_id("ctx-chain")
        )

        # ...and the new ask points at 653, the run that is waiting now.
        parked = [json.loads(r.content) for r in responses if r.content.startswith("{")][-1]
        assert parked["scheduler_status"] == "auth_required"
        assert parked["reply_to"]["scheduled_job_run_id"] == 653
        assert parked["scheduled_job_run_id"] == 653
