"""Tests for the tool-call summarizer's resume guard.

The summary is drawn on the approval card, so it is computed immediately before
``interrupt()``. LangGraph replays a resumed task from the top of the node,
which used to pay for the whole LLM call a second time — sitting between the
user's click and the tool actually running (15.6 s on a measured embedded
``client_action`` apply). The guard must skip that pass, and must never consume
the resume value the real ``interrupt()`` needs.
"""

import types
from unittest.mock import AsyncMock, patch

import pytest

from agent_common.core import tool_call_summarizer as tcs


def _scratchpad(resume: list | None = None, null_resume=None) -> types.SimpleNamespace:
    """A stand-in for LangGraph's PregelScratchpad, recording peek vs. consume."""
    calls: list[bool] = []

    def get_null_resume(consume: bool):
        calls.append(consume)
        return null_resume

    return types.SimpleNamespace(
        resume=resume if resume is not None else [],
        get_null_resume=get_null_resume,
        null_resume_calls=calls,
    )


def _config(scratchpad) -> dict:
    from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD

    return {"configurable": {CONFIG_KEY_SCRATCHPAD: scratchpad}}


class TestResumePending:
    def test_first_pass_is_not_a_resume(self):
        pad = _scratchpad()
        with patch("langgraph.config.get_config", return_value=_config(pad)):
            assert tcs._resume_pending() is False

    def test_a_queued_resume_value_is_a_resume(self):
        pad = _scratchpad(null_resume={"decisions": [{"type": "approve"}]})
        with patch("langgraph.config.get_config", return_value=_config(pad)):
            assert tcs._resume_pending() is True

    def test_peeking_never_consumes_the_resume_value(self):
        # Consuming it here would starve the real interrupt() and hang the turn.
        pad = _scratchpad(null_resume={"decisions": []})
        with patch("langgraph.config.get_config", return_value=_config(pad)):
            tcs._resume_pending()
        assert pad.null_resume_calls == [False]

    def test_a_recorded_resume_value_is_a_resume(self):
        pad = _scratchpad(resume=[{"decisions": [{"type": "approve"}]}])
        with patch("langgraph.config.get_config", return_value=_config(pad)):
            assert tcs._resume_pending() is True
        assert pad.null_resume_calls == []  # answered without touching the queue

    def test_outside_a_graph_it_answers_not_resuming(self):
        # No LangGraph config in scope: summarize exactly as before, never crash.
        with patch("langgraph.config.get_config", side_effect=RuntimeError("no config")):
            assert tcs._resume_pending() is False


class TestAttachSummaries:
    @pytest.mark.asyncio
    async def test_stamps_a_summary_on_the_first_pass(self):
        requests = [{"name": "ls", "args": {"path": "/group_memories/"}}]
        with (
            patch.object(tcs, "_resume_pending", return_value=False),
            patch.object(
                tcs, "summarize_action_requests", AsyncMock(return_value=["Lists the shared memory folder."])
            ) as summarize,
        ):
            await tcs.attach_summaries(requests, language="en")
        summarize.assert_awaited_once()
        assert requests[0]["args"]["_summary"] == "Lists the shared memory folder."

    @pytest.mark.asyncio
    async def test_skips_the_llm_entirely_while_resuming(self):
        requests = [{"name": "ls", "args": {"path": "/tmp"}}]
        with (
            patch.object(tcs, "_resume_pending", return_value=True),
            patch.object(tcs, "summarize_action_requests", AsyncMock()) as summarize,
        ):
            await tcs.attach_summaries(requests, language="en")
        summarize.assert_not_awaited()
        assert "_summary" not in requests[0]["args"]

    @pytest.mark.asyncio
    async def test_a_failed_summary_leaves_the_request_untouched(self):
        requests = [{"name": "ls", "args": {"path": "/tmp"}}]
        with (
            patch.object(tcs, "_resume_pending", return_value=False),
            patch.object(tcs, "summarize_action_requests", AsyncMock(return_value=None)),
        ):
            await tcs.attach_summaries(requests, language="en")
        assert requests[0]["args"] == {"path": "/tmp"}


class TestSelfEvidentTools:
    """``client_action`` explains itself: `kind` is a closed enum and the embed
    SDK renders a localized sentence per kind. Paying a fast-LLM call to
    paraphrase it cost 3.35 s in front of the approval card."""

    @pytest.mark.asyncio
    async def test_a_client_action_batch_never_reaches_the_model(self):
        requests = [{"name": "client_action", "args": {"kind": "apply", "target_type": "Campaign"}}]
        with (
            patch.object(tcs, "_resume_pending", return_value=False),
            patch.object(tcs, "summarize_action_requests", AsyncMock()) as summarize,
        ):
            await tcs.attach_summaries(requests, language="en")
        summarize.assert_not_awaited()
        assert "_summary" not in requests[0]["args"]

    @pytest.mark.asyncio
    async def test_a_mixed_batch_still_summarizes_the_rest(self):
        requests = [
            {"name": "client_action", "args": {"kind": "apply"}},
            {"name": "send_email", "args": {"to": "sales@example.com"}},
        ]
        with (
            patch.object(tcs, "_resume_pending", return_value=False),
            patch.object(
                tcs, "summarize_action_requests", AsyncMock(return_value=["Sends an email to sales."])
            ) as summarize,
        ):
            await tcs.attach_summaries(requests, language="en")
        # Only the one that needs prose is prompted for, and it gets its own summary.
        assert summarize.await_args.args[0] == [("send_email", {"to": "sales@example.com"}, "")]
        assert "_summary" not in requests[0]["args"]
        assert requests[1]["args"]["_summary"] == "Sends an email to sales."
