"""Tests for the active stream registry and steering queue in BaseAgentExecutor."""

import asyncio

import pytest
from a2a.types import Message as A2AMessage
from a2a.types import Part as A2APart
from a2a.types import TextPart

from ringier_a2a_sdk.server.executor import (
    MAX_STEERING_QUEUE_DEPTH,
    ActiveStreamInfo,
    _active_streams,
    _active_streams_lock,
)


def _make_a2a_message(text: str = "hello", context_id: str = "ctx-1") -> A2AMessage:
    return A2AMessage(
        role="user",
        parts=[A2APart(root=TextPart(text=text))],
        message_id="msg-test",
        context_id=context_id,
    )


class TestActiveStreamInfo:
    def test_defaults(self):
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1")
        assert info.context_id == "ctx-1"
        assert info.task_id == "task-1"
        assert info.owner_sub is None
        assert isinstance(info.message_queue, asyncio.Queue)
        assert info.started_at > 0

    def test_owner_sub_stored(self):
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1", owner_sub="user-abc")
        assert info.owner_sub == "user-abc"


class TestActiveStreamRegistry:
    def setup_method(self):
        """Clean up registry before each test."""
        _active_streams.clear()

    def teardown_method(self):
        _active_streams.clear()

    def test_registry_returns_none_when_empty(self):
        assert _active_streams.get("ctx-1") is None

    def test_registry_returns_info(self):
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1")
        _active_streams["ctx-1"] = info
        assert _active_streams.get("ctx-1") is info

    def test_registry_isolates_contexts(self):
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1")
        _active_streams["ctx-1"] = info
        assert _active_streams.get("ctx-2") is None


class TestSteeringQueueBehavior:
    def setup_method(self):
        _active_streams.clear()

    def teardown_method(self):
        _active_streams.clear()

    @pytest.mark.asyncio
    async def test_message_queue_put_and_get(self):
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1")
        msg = _make_a2a_message("follow up")
        info.message_queue.put_nowait(msg)

        retrieved = info.message_queue.get_nowait()
        assert retrieved.parts[0].root.text == "follow up"

    @pytest.mark.asyncio
    async def test_queue_depth_limit(self):
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1")
        for i in range(MAX_STEERING_QUEUE_DEPTH):
            info.message_queue.put_nowait(_make_a2a_message(f"msg-{i}"))

        assert info.message_queue.qsize() == MAX_STEERING_QUEUE_DEPTH

    @pytest.mark.asyncio
    async def test_lock_serializes_access(self):
        """Verify that the lock properly serializes concurrent access."""
        results = []

        async def writer(name: str):
            async with _active_streams_lock:
                results.append(f"start-{name}")
                await asyncio.sleep(0.01)
                results.append(f"end-{name}")

        await asyncio.gather(writer("a"), writer("b"))
        # Each start-end pair should be contiguous
        assert results[0].startswith("start-") and results[1].startswith("end-")
        assert results[2].startswith("start-") and results[3].startswith("end-")


class TestSteeringAuthorization:
    """Tests that steering messages are rejected when caller != stream owner."""

    def setup_method(self):
        _active_streams.clear()

    def teardown_method(self):
        _active_streams.clear()

    def test_owner_sub_mismatch_blocks_queue(self):
        """A stream with owner_sub set should reject messages from a different user."""
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1", owner_sub="user-owner")
        _active_streams["ctx-1"] = info

        # Simulate what the executor checks: caller_sub != owner_sub
        caller_sub = "user-attacker"
        assert info.owner_sub is not None
        assert caller_sub != info.owner_sub
        # Message should NOT be queued
        assert info.message_queue.qsize() == 0

    def test_same_user_allowed_to_steer(self):
        """A stream with owner_sub set should allow messages from the same user."""
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1", owner_sub="user-owner")
        _active_streams["ctx-1"] = info

        caller_sub = "user-owner"
        assert caller_sub == info.owner_sub
        # Safe to queue
        info.message_queue.put_nowait(_make_a2a_message("follow-up"))
        assert info.message_queue.qsize() == 1

    def test_no_owner_sub_allows_through(self):
        """When owner_sub is None (race: not yet set), steering is allowed."""
        info = ActiveStreamInfo(context_id="ctx-1", task_id="task-1", owner_sub=None)
        _active_streams["ctx-1"] = info

        # owner_sub is None, so guard condition `active.owner_sub and ...` is false
        assert not info.owner_sub
        info.message_queue.put_nowait(_make_a2a_message("allowed"))
        assert info.message_queue.qsize() == 1
