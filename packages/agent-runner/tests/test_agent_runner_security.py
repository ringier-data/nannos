"""Tests for AgentRunner security-critical methods.

Covers:
- _fetch_user_id_from_backend(): missing token, 401 response, missing 'id' field, valid response
- _extract_message_metadata(): extracts metadata from task, handles missing/malformed data
- Watch condition short-circuit: when condition_not_met, _stream_impl yields early without sub-agent call
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def agent_runner():
    """Create an AgentRunner instance with minimal mocking.

    Patches _create_checkpointer to avoid the CHECKPOINT_DYNAMODB_TABLE_NAME
    requirement, and suppresses cost-tracking setup.
    """
    mock_checkpointer = MagicMock(name="checkpointer")

    with patch("agent.core._create_checkpointer", return_value=mock_checkpointer):
        with patch.dict(os.environ, {"CHECKPOINT_DYNAMODB_TABLE_NAME": "test-table"}, clear=False):
            from agent.core import AgentRunner

            runner = AgentRunner()
            return runner


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


class TestWatchConditionNotMet:
    """When watch condition is not met, _stream_impl returns early with condition_not_met status."""

    @pytest.mark.asyncio
    async def test_condition_not_met_yields_condition_not_met_response(self, agent_runner):
        """If _evaluate_watch returns (False, check_result), stream yields condition_not_met."""

        check_result = {"value": 0, "threshold": 5}
        agent_runner._evaluate_watch = AsyncMock(return_value=(False, check_result))
        agent_runner._execute_sub_agent = AsyncMock()

        # Build a minimal Task and UserConfig
        task = MagicMock()
        task.context_id = "ctx-1"
        task.history = [
            MagicMock(
                metadata={
                    "job_type": "watch",
                    "watch": {
                        "check_tool": "ping",
                        "check_args": {},
                    },
                    "sub_agent_id": 5,
                    "scheduled_job_id": 10,
                }
            )
        ]

        user_config = MagicMock()
        user_config.user_sub = "sub-1"
        user_config.access_token = MagicMock()
        user_config.access_token.get_secret_value.return_value = "bearer-token"

        # Patch _fetch_user_id_from_backend so we don't hit the real HTTP
        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-1")

        responses = []
        async for response in agent_runner._stream_impl("any query", user_config, task):
            responses.append(response)

        # First response is the "working" status, second is condition_not_met
        content_items = [json.loads(r.content) for r in responses if r.content.startswith("{")]
        assert any(item.get("scheduler_status") == "condition_not_met" for item in content_items)
        condition_not_met_item = next(i for i in content_items if i.get("scheduler_status") == "condition_not_met")
        assert condition_not_met_item["last_check_result"] == check_result

        # Sub-agent must NOT have been called
        agent_runner._execute_sub_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_condition_met_does_not_short_circuit(self, agent_runner):
        """When watch condition IS met, _execute_sub_agent is called."""
        check_result = {"value": 10}
        agent_runner._evaluate_watch = AsyncMock(return_value=(True, check_result))
        agent_runner._execute_sub_agent = AsyncMock(return_value="Agent completed task.")
        agent_runner._generate_watch_message = AsyncMock(return_value="Watch triggered.")

        task = MagicMock()
        task.context_id = "ctx-2"
        task.history = [
            MagicMock(
                metadata={
                    "job_type": "watch",
                    "watch": {
                        "check_tool": "ping",
                        "check_args": {},
                    },
                    "sub_agent_id": 5,
                    "scheduled_job_id": 10,
                }
            )
        ]

        user_config = MagicMock()
        user_config.user_sub = "sub-2"
        user_config.access_token = MagicMock()
        user_config.access_token.get_secret_value.return_value = "bearer-token"

        agent_runner._fetch_user_id_from_backend = AsyncMock(return_value="user-uuid-2")

        responses = []
        async for response in agent_runner._stream_impl("", user_config, task):
            responses.append(response)

        # Sub-agent was called
        agent_runner._execute_sub_agent.assert_awaited_once()

        # Final response is success
        final_content = json.loads(responses[-1].content)
        assert final_content["scheduler_status"] == "success"
