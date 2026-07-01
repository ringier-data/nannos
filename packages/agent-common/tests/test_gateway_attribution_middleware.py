"""Tests for GatewayAttributionMiddleware.

Verifies that gateway cost-attribution ContextVars are derived from each model
call's own LangGraph tags, set for the duration of the call, and restored after —
so in-process sub-agent LLM calls are billed to the sub-agent regardless of which
dispatch path invoked them.
"""

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agent_common.middleware.gateway_attribution_middleware import (
    GatewayAttributionMiddleware,
    _parse_attribution_from_tags,
)
from ringier_a2a_sdk.cost_tracking.attribution import (
    current_conversation_id,
    current_scheduled_job_id,
    current_sub_agent_id,
    current_user_sub,
)


class _CaptureMiddleware(AgentMiddleware):
    """Inner middleware that records the attribution ContextVars seen mid-call."""

    def __init__(self):
        super().__init__()
        self.seen: dict = {}

    async def awrap_model_call(self, request, handler):
        self.seen = {
            "sub_agent_id": current_sub_agent_id.get(),
            "user_sub": current_user_sub.get(),
            "conversation_id": current_conversation_id.get(),
            "scheduled_job_id": current_scheduled_job_id.get(),
        }
        return await handler(request)


def _agent(capture: _CaptureMiddleware):
    model = FakeMessagesListChatModel(responses=[AIMessage(content="done")])
    # GatewayAttributionMiddleware must be outermost so the capture middleware
    # (inner) runs inside its attribution scope.
    return create_agent(model=model, tools=[], middleware=[GatewayAttributionMiddleware(), capture])


class TestParseAttributionFromTags:
    def test_parses_all_fields(self):
        fields = _parse_attribution_from_tags(
            ["user_sub:u1", "conversation:c1", "sub_agent:42", "scheduled_job:7", "other:x"]
        )
        assert fields == {
            "user_sub": "u1",
            "conversation_id": "c1",
            "sub_agent_id": 42,
            "scheduled_job_id": 7,
        }

    def test_empty_and_none(self):
        assert _parse_attribution_from_tags(None) == {}
        assert _parse_attribution_from_tags([]) == {}

    def test_non_integer_ids_dropped(self):
        fields = _parse_attribution_from_tags(["sub_agent:not-an-int", "user_sub:u1"])
        assert fields == {"user_sub": "u1"}


class TestGatewayAttributionMiddleware:
    async def test_sets_sub_agent_id_during_call_and_restores_after(self):
        capture = _CaptureMiddleware()
        agent = _agent(capture)
        prev = current_sub_agent_id.set(None)  # caller (orchestrator) has none
        try:
            await agent.ainvoke(
                {"messages": [("user", "hi")]},
                config={"tags": ["user_sub:u1", "conversation:c1", "sub_agent:42"]},
            )
            # Derived from the call's own tags, visible to inner middleware:
            assert capture.seen["sub_agent_id"] == 42
            assert capture.seen["user_sub"] == "u1"
            assert capture.seen["conversation_id"] == "c1"
            # Restored to the caller's value after the call:
            assert current_sub_agent_id.get() is None
        finally:
            current_sub_agent_id.reset(prev)

    async def test_no_sub_agent_tag_leaves_caller_value(self):
        """Orchestrator calls (no sub_agent tag) must not inherit a stale id, and
        must not clobber whatever the caller set."""
        capture = _CaptureMiddleware()
        agent = _agent(capture)
        prev = current_sub_agent_id.set(99)  # e.g. outer sub-agent active
        try:
            await agent.ainvoke(
                {"messages": [("user", "hi")]},
                config={"tags": ["user_sub:u1", "conversation:c1"]},
            )
            assert capture.seen["sub_agent_id"] == 99  # falls through to caller
            assert current_sub_agent_id.get() == 99
        finally:
            current_sub_agent_id.reset(prev)

    async def test_scheduled_job_and_restore(self):
        capture = _CaptureMiddleware()
        agent = _agent(capture)
        prev = current_scheduled_job_id.set(None)
        try:
            await agent.ainvoke(
                {"messages": [("user", "hi")]},
                config={"tags": ["user_sub:u1", "sub_agent:5", "scheduled_job:7"]},
            )
            assert capture.seen["scheduled_job_id"] == 7
            assert capture.seen["sub_agent_id"] == 5
            assert current_scheduled_job_id.get() is None  # restored
        finally:
            current_scheduled_job_id.reset(prev)


def test_middleware_is_outermost_in_common_stack():
    """build_common_middleware_stack must place GatewayAttributionMiddleware first
    (outermost) so its attribution scope wraps every nested model call."""
    from unittest.mock import MagicMock

    from agent_common.core.graph_utils import build_common_middleware_stack

    stack = build_common_middleware_stack(model=MagicMock(model_name="claude-sonnet-4.6"), backend=MagicMock())
    assert isinstance(stack[0], GatewayAttributionMiddleware)
