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
)
from ringier_a2a_sdk.cost_tracking.attribution import (
    current_conversation_id,
    current_scheduled_job_id,
    current_sub_agent_id,
    current_user_sub,
    parse_attribution_tags,
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
        fields = parse_attribution_tags(
            [
                "user_sub:u1",
                "conversation:c1",
                "sub_agent:42",
                "sub_agent_config_version:99",
                "scheduled_job:7",
                "other:x",
            ]
        )
        assert fields == {
            "user_sub": "u1",
            "conversation_id": "c1",
            "sub_agent_id": 42,
            "sub_agent_config_version_id": 99,
            "scheduled_job_id": 7,
        }

    def test_config_version_tag_not_confused_with_sub_agent_tag(self):
        # "sub_agent_config_version:99" must NOT be parsed as sub_agent_id.
        fields = parse_attribution_tags(["sub_agent_config_version:99"])
        assert fields == {"sub_agent_config_version_id": 99}
        assert "sub_agent_id" not in fields

    def test_empty_and_none(self):
        assert parse_attribution_tags(None) == {}
        assert parse_attribution_tags([]) == {}

    def test_non_integer_ids_dropped(self):
        fields = parse_attribution_tags(["sub_agent:not-an-int", "user_sub:u1"])
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

    async def test_config_version_derived_from_tag(self):
        from ringier_a2a_sdk.cost_tracking.attribution import current_sub_agent_config_version_id

        capture = _CaptureMiddleware()

        class _CaptureCfgVer(_CaptureMiddleware):
            async def awrap_model_call(self, request, handler):
                self.seen = {"cfg_ver": current_sub_agent_config_version_id.get()}
                return await handler(request)

        cap = _CaptureCfgVer()
        from langchain_core.messages import AIMessage
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

        model = FakeMessagesListChatModel(responses=[AIMessage(content="done")])
        agent = create_agent(model=model, tools=[], middleware=[GatewayAttributionMiddleware(), cap])
        prev = current_sub_agent_config_version_id.set(None)
        try:
            await agent.ainvoke(
                {"messages": [("user", "hi")]},
                config={"tags": ["sub_agent:5", "sub_agent_config_version:99"]},
            )
            assert cap.seen["cfg_ver"] == 99
            assert current_sub_agent_config_version_id.get() is None  # restored
        finally:
            current_sub_agent_config_version_id.reset(prev)

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


def test_extra_middlewares_do_not_displace_attribution_outermost(monkeypatch):
    """build_sub_agent_graph must keep GatewayAttributionMiddleware outermost even
    when extra_middlewares are supplied. An extra middleware that makes its own
    gateway model call in wrap_model_call (e.g. ToolsetSelectorMiddleware) would
    otherwise run before the attribution scope is established and misattribute its
    tokens to the caller instead of this sub-agent."""
    from unittest.mock import MagicMock

    import agent_common.core.graph_utils as gu

    captured: dict = {}

    def _fake_create_agent(model, **kwargs):
        captured["middleware"] = kwargs["middleware"]
        return MagicMock()

    monkeypatch.setattr(gu, "create_agent", _fake_create_agent)

    extra = AgentMiddleware()
    gu.build_sub_agent_graph(
        model=MagicMock(model_name="claude-sonnet-4.6"),
        tools=[],
        system_prompt="",
        checkpointer=None,
        backend_factory=MagicMock(),
        exclude_deep_agents_middlewares=True,  # keep the stack light; attribution still runs
        extra_middlewares=[extra],
    )

    mw = captured["middleware"]
    assert isinstance(mw[0], GatewayAttributionMiddleware), "attribution must stay outermost"
    assert extra in mw and mw.index(extra) > 0, "extra middleware must sit inside the attribution scope"
