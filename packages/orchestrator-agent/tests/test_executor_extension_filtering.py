"""Unit tests for extension filtering in _handle_stream_item().

Verifies that activity-log, work-plan, and intermediate-output events
are correctly suppressed or emitted based on the client's requested
extensions (X-A2A-Extensions header).
"""

from unittest.mock import AsyncMock, Mock

import pytest
from a2a.types import TaskState
from ringier_a2a_sdk.models import TodoItem

from app.core.a2a_extensions import (
    ACTIVITY_LOG_EXTENSION,
    INTERMEDIATE_OUTPUT_EXTENSION,
    WORK_PLAN_EXTENSION,
)
from app.core.executor import OrchestratorDeepAgentExecutor
from app.models.responses import AgentStreamResponse


@pytest.fixture
def executor(dynamodb_table):
    return OrchestratorDeepAgentExecutor()


@pytest.fixture
def updater():
    m = Mock()
    m.update_status = AsyncMock()
    m.add_artifact = AsyncMock()
    m.complete = AsyncMock()
    return m


@pytest.fixture
def task():
    t = Mock()
    t.id = "task-1"
    t.context_id = "ctx-1"
    return t


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

ALL_EXTENSIONS = {ACTIVITY_LOG_EXTENSION, WORK_PLAN_EXTENSION, INTERMEDIATE_OUTPUT_EXTENSION}


# ===========================================================================
# Activity Log extension filtering
# ===========================================================================


class TestActivityLogExtensionFiltering:
    """Activity-log events should only be emitted when the extension is active."""

    @pytest.mark.asyncio
    async def test_suppressed_when_no_extensions_header(self, executor, updater, task):
        """No X-A2A-Extensions header → activity log suppressed."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Calling tool X",
            metadata={"activity_log": True, "source": "orchestrator"},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            active_extensions=None,
        )
        updater.update_status.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_suppressed_when_other_extensions_requested(self, executor, updater, task):
        """Client requested other extensions but not activity-log → suppressed."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Calling tool X",
            metadata={"activity_log": True},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            active_extensions={WORK_PLAN_EXTENSION},
        )
        updater.update_status.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_emitted_when_extension_active(self, executor, updater, task):
        """Client requested activity-log extension → event emitted."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Calling tool X",
            metadata={"activity_log": True, "source": "orchestrator"},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            active_extensions={ACTIVITY_LOG_EXTENSION},
        )
        updater.update_status.assert_awaited_once()
        call_args = updater.update_status.call_args
        assert call_args[0][0] == TaskState.working
        msg = call_args[0][1]
        assert ACTIVITY_LOG_EXTENSION in msg.extensions
        assert result is False  # first_chunk_sent unchanged

    @pytest.mark.asyncio
    async def test_preserves_first_chunk_sent_flag(self, executor, updater, task):
        """Activity log must not alter first_chunk_sent regardless of current value."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Calling tool Y",
            metadata={"activity_log": True},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            first_chunk_sent=True,
            active_extensions={ACTIVITY_LOG_EXTENSION},
        )
        assert result is True  # preserved


# ===========================================================================
# Work Plan extension filtering
# ===========================================================================


class TestWorkPlanExtensionFiltering:
    """Work-plan events should only be emitted when the extension is active."""

    @pytest.mark.asyncio
    async def test_suppressed_when_no_extensions_header(self, executor, updater, task):
        """No header → work plan suppressed."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="",
            metadata={"work_plan": True, "todos": [TodoItem(name="step 1", state="submitted")]},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            active_extensions=None,
        )
        updater.update_status.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_suppressed_when_other_extensions_requested(self, executor, updater, task):
        """Client requested other extensions but not work-plan → suppressed."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="",
            metadata={"work_plan": True, "todos": []},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            active_extensions={ACTIVITY_LOG_EXTENSION},
        )
        updater.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_emitted_when_extension_active(self, executor, updater, task):
        """Client requested work-plan extension → event emitted."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="",
            metadata={"work_plan": True, "todos": [TodoItem(name="step 1", state="submitted")]},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            active_extensions={WORK_PLAN_EXTENSION},
        )
        updater.update_status.assert_awaited_once()
        call_args = updater.update_status.call_args
        msg = call_args[0][1]
        assert WORK_PLAN_EXTENSION in msg.extensions


# ===========================================================================
# Intermediate Output extension filtering
# ===========================================================================


class TestIntermediateOutputExtensionFiltering:
    """Intermediate-output (reasoning/thinking) must be suppressed when extension not active."""

    @pytest.mark.asyncio
    async def test_suppressed_when_no_extensions_header(self, executor, updater, task):
        """No header → intermediate output suppressed entirely (the Slack bug fix)."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Let me think about this...",
            metadata={
                "streaming_chunk": True,
                "intermediate_output": True,
                "agent_name": "orchestrator",
            },
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            streaming_artifact_id="art-1",
            active_extensions=None,
        )
        updater.add_artifact.assert_not_called()
        assert result is False  # first_chunk_sent unchanged

    @pytest.mark.asyncio
    async def test_suppressed_when_other_extensions_requested(self, executor, updater, task):
        """Client requested other extensions but not intermediate-output → suppressed."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Reasoning text",
            metadata={
                "streaming_chunk": True,
                "intermediate_output": True,
                "agent_name": "sub-agent",
            },
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            streaming_artifact_id="art-1",
            active_extensions={ACTIVITY_LOG_EXTENSION, WORK_PLAN_EXTENSION},
        )
        updater.add_artifact.assert_not_called()
        assert result is False

    @pytest.mark.asyncio
    async def test_emitted_when_extension_active(self, executor, updater, task):
        """Client requested intermediate-output extension → artifact emitted with extension tag."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Sub-agent thinking...",
            metadata={
                "streaming_chunk": True,
                "intermediate_output": True,
                "agent_name": "research-agent",
            },
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            streaming_artifact_id="art-1",
            active_extensions={INTERMEDIATE_OUTPUT_EXTENSION},
        )
        updater.add_artifact.assert_awaited_once()
        call_kwargs = updater.add_artifact.call_args[1]
        assert call_kwargs["artifact_id"] == "art-1-thought"
        assert call_kwargs["extensions"] == [INTERMEDIATE_OUTPUT_EXTENSION]
        assert call_kwargs["metadata"]["agent_name"] == "research-agent"
        # intermediate output should NOT set first_chunk_sent
        assert result is False

    @pytest.mark.asyncio
    async def test_does_not_set_first_chunk_sent(self, executor, updater, task):
        """Even when emitted, intermediate output must not claim first_chunk_sent."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="thinking...",
            metadata={
                "streaming_chunk": True,
                "intermediate_output": True,
                "agent_name": "orchestrator",
            },
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            streaming_artifact_id="art-1",
            first_chunk_sent=False,
            active_extensions=ALL_EXTENSIONS,
        )
        assert result is False  # must remain False

    @pytest.mark.asyncio
    async def test_preserves_first_chunk_sent_true(self, executor, updater, task):
        """If first_chunk_sent was already True, intermediate output preserves it."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="more thinking...",
            metadata={
                "streaming_chunk": True,
                "intermediate_output": True,
                "agent_name": "orchestrator",
            },
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            streaming_artifact_id="art-1",
            first_chunk_sent=True,
            active_extensions=ALL_EXTENSIONS,
        )
        assert result is True  # preserved


# ===========================================================================
# Main streaming chunks (no extension gating)
# ===========================================================================


class TestMainStreamingChunks:
    """Main content streaming chunks are always emitted regardless of extensions."""

    @pytest.mark.asyncio
    async def test_emitted_when_no_extensions_header(self, executor, updater, task):
        """Main content chunks are always emitted even without extensions header."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Here is the answer...",
            metadata={"streaming_chunk": True},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            streaming_artifact_id="art-1",
            active_extensions=None,
        )
        updater.add_artifact.assert_awaited_once()
        call_kwargs = updater.add_artifact.call_args[1]
        assert call_kwargs["artifact_id"] == "art-1"
        assert call_kwargs["extensions"] is None
        assert result is True  # first_chunk_sent becomes True

    @pytest.mark.asyncio
    async def test_emitted_when_all_extensions_active(self, executor, updater, task):
        """Main content chunks are always emitted when all extensions active."""
        item = AgentStreamResponse(
            state=TaskState.working,
            content="Answer text",
            metadata={"streaming_chunk": True},
        )
        result = await executor._handle_stream_item(
            item,
            updater,
            task,
            is_final=False,
            streaming_artifact_id="art-1",
            active_extensions=ALL_EXTENSIONS,
        )
        updater.add_artifact.assert_awaited_once()
        assert result is True


# ===========================================================================
# Combined: Slack-like scenario (no extensions header)
# ===========================================================================


class TestSlackScenario:
    """End-to-end scenario: client sends no X-A2A-Extensions header (e.g. Slack).

    Only main content should reach the client; all extension events are suppressed.
    """

    @pytest.mark.asyncio
    async def test_only_main_content_reaches_client(self, executor, updater, task):
        """Simulate a mixed stream of events — only main content artifacts should be emitted."""
        artifact_id = "art-slack"
        no_extensions = None  # Slack sends no header

        # 1. Activity log → suppressed
        await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="Delegating to research agent",
                metadata={"activity_log": True, "source": "orchestrator"},
            ),
            updater,
            task,
            is_final=False,
            active_extensions=no_extensions,
        )

        # 2. Work plan → suppressed
        await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="",
                metadata={"work_plan": True, "todos": [TodoItem(name="research", state="working")]},
            ),
            updater,
            task,
            is_final=False,
            active_extensions=no_extensions,
        )

        # 3. Intermediate output (sub-agent reasoning) → suppressed
        await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="Let me analyze the data...",
                metadata={
                    "streaming_chunk": True,
                    "intermediate_output": True,
                    "agent_name": "research-agent",
                },
            ),
            updater,
            task,
            is_final=False,
            streaming_artifact_id=artifact_id,
            active_extensions=no_extensions,
        )

        # 4. Orchestrator thinking → suppressed
        await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="I need to consider...",
                metadata={
                    "streaming_chunk": True,
                    "intermediate_output": True,
                    "agent_name": "orchestrator",
                },
            ),
            updater,
            task,
            is_final=False,
            streaming_artifact_id=artifact_id,
            active_extensions=no_extensions,
        )

        # Nothing should have reached the client yet
        updater.update_status.assert_not_called()
        updater.add_artifact.assert_not_called()

        # 5. Main content chunk → emitted
        result = await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="Based on my analysis, here is the answer.",
                metadata={"streaming_chunk": True},
            ),
            updater,
            task,
            is_final=False,
            streaming_artifact_id=artifact_id,
            active_extensions=no_extensions,
        )
        assert result is True
        updater.add_artifact.assert_awaited_once()
        call_kwargs = updater.add_artifact.call_args[1]
        assert call_kwargs["artifact_id"] == artifact_id
        assert call_kwargs["extensions"] is None


# ===========================================================================
# Combined: Playground scenario (all extensions active)
# ===========================================================================


class TestPlaygroundScenario:
    """End-to-end scenario: playground client sends all extensions."""

    @pytest.mark.asyncio
    async def test_all_events_reach_client(self, executor, updater, task):
        """All event types should be emitted when all extensions are active."""
        artifact_id = "art-playground"
        all_ext = ALL_EXTENSIONS

        # 1. Activity log → emitted
        await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="Delegating to research agent",
                metadata={"activity_log": True, "source": "orchestrator"},
            ),
            updater,
            task,
            is_final=False,
            active_extensions=all_ext,
        )
        assert updater.update_status.await_count == 1

        # 2. Work plan → emitted
        await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="",
                metadata={"work_plan": True, "todos": [TodoItem(name="research", state="working")]},
            ),
            updater,
            task,
            is_final=False,
            active_extensions=all_ext,
        )
        assert updater.update_status.await_count == 2

        # 3. Intermediate output → emitted as thought artifact
        await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="Analyzing...",
                metadata={
                    "streaming_chunk": True,
                    "intermediate_output": True,
                    "agent_name": "research-agent",
                },
            ),
            updater,
            task,
            is_final=False,
            streaming_artifact_id=artifact_id,
            active_extensions=all_ext,
        )
        assert updater.add_artifact.await_count == 1
        thought_kwargs = updater.add_artifact.call_args[1]
        assert thought_kwargs["artifact_id"] == f"{artifact_id}-thought"
        assert thought_kwargs["extensions"] == [INTERMEDIATE_OUTPUT_EXTENSION]

        # 4. Main content → emitted as main artifact
        result = await executor._handle_stream_item(
            AgentStreamResponse(
                state=TaskState.working,
                content="Here is the answer.",
                metadata={"streaming_chunk": True},
            ),
            updater,
            task,
            is_final=False,
            streaming_artifact_id=artifact_id,
            active_extensions=all_ext,
        )
        assert result is True
        assert updater.add_artifact.await_count == 2
        main_kwargs = updater.add_artifact.call_args[1]
        assert main_kwargs["artifact_id"] == artifact_id
        assert main_kwargs["extensions"] is None
