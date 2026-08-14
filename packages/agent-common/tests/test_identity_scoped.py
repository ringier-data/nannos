"""Tests for identity-scoped tool support (ADR 0006, Gate 3)."""

import json
import types
from unittest.mock import MagicMock

import pytest
from agent_common.core.identity_scoped import (
    NANNOS_USER_IDENTITY_FIELD,
    is_identity_scoped_tool,
    is_wrapped_identity_scoped_tool,
    wrap_identity_scoped_tool,
    wrap_identity_scoped_tools,
    wrap_identity_scoped_tools_in_registry,
)
from agent_common.middleware.identity_consent import IdentityConsentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IDENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string", "description": "The note body"},
        NANNOS_USER_IDENTITY_FIELD: {"type": "string", "description": "Reserved"},
    },
    "required": ["note", NANNOS_USER_IDENTITY_FIELD],
}

SERVER_SLUG = "gatana-salesforce"

PLAIN_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


def _make_inner_tool(
    captured: dict,
    schema=None,
    server_name: str = "gatana-salesforce",
    response_format="content",
    name: str = "salesforce_create_note",
):
    async def _run(**kwargs):
        captured.update(kwargs)
        return (
            ("ok", {"structured": True})
            if response_format == "content_and_artifact"
            else "ok"
        )

    return StructuredTool(
        name=name,
        description="Create a personal note",
        args_schema=schema or IDENTITY_SCHEMA,
        coroutine=_run,
        response_format=response_format,
        metadata={"server_name": server_name},
    )


def _make_context(grants=None, email="user@example.com", tool_registry=None):
    return types.SimpleNamespace(
        email=email,
        identity_consent_grants=grants if grants is not None else {},
        _pending_identity_consents=[],
        tool_server_map={},
        tool_registry=tool_registry or {},
    )


def _make_runtime(context):
    return types.SimpleNamespace(context=context)


# ---------------------------------------------------------------------------
# Detection + schema hiding
# ---------------------------------------------------------------------------


class TestDetectionAndWrapping:
    def test_detects_identity_scoped_tool(self):
        assert is_identity_scoped_tool(_make_inner_tool({}))

    def test_ignores_plain_tool(self):
        assert not is_identity_scoped_tool(_make_inner_tool({}, schema=PLAIN_SCHEMA))

    def test_detection_never_raises(self):
        class ExplodingSchema:
            @property
            def args_schema(self):
                raise RuntimeError("boom")

        assert not is_identity_scoped_tool(ExplodingSchema())

    def test_wrapper_hides_reserved_field_from_schema(self):
        wrapper = wrap_identity_scoped_tool(_make_inner_tool({}))
        assert NANNOS_USER_IDENTITY_FIELD not in wrapper.args_schema["properties"]
        assert NANNOS_USER_IDENTITY_FIELD not in wrapper.args_schema.get("required", [])
        # Other fields survive
        assert "note" in wrapper.args_schema["properties"]
        assert wrapper.args_schema["required"] == ["note"]

    def test_wrapper_keeps_name_description_metadata_and_response_format(self):
        inner = _make_inner_tool({}, response_format="content_and_artifact")
        wrapper = wrap_identity_scoped_tool(inner)
        assert wrapper.name == inner.name
        assert wrapper.description == inner.description
        assert wrapper.metadata["server_name"] == "gatana-salesforce"
        assert wrapper.response_format == "content_and_artifact"
        assert is_wrapped_identity_scoped_tool(wrapper)

    def test_wrap_list_only_identity_tools(self):
        identity_tool = _make_inner_tool({})
        plain_tool = _make_inner_tool({}, schema=PLAIN_SCHEMA)
        plain_tool.name = "plain_tool"

        wrapped = wrap_identity_scoped_tools([identity_tool, plain_tool])

        assert is_wrapped_identity_scoped_tool(wrapped[0])
        assert wrapped[1] is plain_tool

    def test_wrap_list_is_idempotent(self):
        once = wrap_identity_scoped_tools([_make_inner_tool({})])
        twice = wrap_identity_scoped_tools(once)
        assert twice[0] is once[0]

    def test_wrap_registry_in_place(self):
        registry = {"salesforce_create_note": _make_inner_tool({})}
        assert wrap_identity_scoped_tools_in_registry(registry) == [
            "salesforce_create_note"
        ]
        assert is_wrapped_identity_scoped_tool(registry["salesforce_create_note"])
        assert wrap_identity_scoped_tools_in_registry(registry) == []


# ---------------------------------------------------------------------------
# Wrapper execution: force-populate + fail closed
# ---------------------------------------------------------------------------


class TestWrapperExecution:
    async def test_injects_verified_email_with_grant(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(grants={SERVER_SLUG: {"granted": True}})

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        assert result == "ok"
        assert captured[NANNOS_USER_IDENTITY_FIELD] == "user@example.com"
        assert captured["note"] == "hello"

    async def test_overwrites_model_supplied_identity(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(grants={SERVER_SLUG: {"granted": True}})

        await wrapper.coroutine(
            runtime=_make_runtime(context),
            note="hello",
            **{NANNOS_USER_IDENTITY_FIELD: "attacker@evil.example"},
        )

        assert captured[NANNOS_USER_IDENTITY_FIELD] == "user@example.com"

    async def test_preserves_content_and_artifact_tuple(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(
            _make_inner_tool(captured, response_format="content_and_artifact")
        )
        context = _make_context(grants={SERVER_SLUG: {"granted": True}})

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        assert result == ("ok", {"structured": True})

    async def test_fails_closed_without_grant(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(grants={})

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        payload = json.loads(result)
        assert payload["error"] is True
        assert payload["error_code"] == "auth_required"
        assert captured == {}  # inner never called

    async def test_fails_closed_tuple_shape_for_artifact_tools(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(
            _make_inner_tool(captured, response_format="content_and_artifact")
        )
        context = _make_context(grants={})

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        content, artifact = result
        assert json.loads(content)["error_code"] == "auth_required"
        assert artifact is None
        assert captured == {}

    async def test_fails_closed_on_remembered_denial(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(grants={SERVER_SLUG: {"granted": False}})

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        payload = json.loads(result)
        assert payload["error_code"] == "auth_required"
        assert "declined" in payload["message"]
        assert captured == {}

    async def test_fails_closed_without_email(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(grants={SERVER_SLUG: {"granted": True}}, email=None)

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        assert json.loads(result)["error_code"] == "auth_required"
        assert captured == {}

    async def test_fails_closed_without_runtime_context(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))

        result = await wrapper.coroutine(runtime=None, note="hello")

        assert json.loads(result)["error_code"] == "auth_required"
        assert captured == {}

    async def test_grants_reachable_from_subagent_style_context(self):
        """Sub-agent contexts (plain SimpleNamespace) honor remembered grants."""
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        subagent_context = types.SimpleNamespace(
            identity_consent_grants={SERVER_SLUG: {"granted": True}},
            email="user@example.com",
        )

        result = await wrapper.coroutine(
            runtime=_make_runtime(subagent_context), note="hello"
        )

        assert result == "ok"
        assert captured[NANNOS_USER_IDENTITY_FIELD] == "user@example.com"


# ---------------------------------------------------------------------------
# Consent middleware (Gate 3)
# ---------------------------------------------------------------------------


def _make_state_and_runtime(
    grants, tool_calls, email="user@example.com", server_name=SERVER_SLUG
):
    registry = {
        "salesforce_create_note": wrap_identity_scoped_tool(
            _make_inner_tool({}, server_name=server_name)
        )
    }
    context = _make_context(grants=grants, tool_registry=registry, email=email)
    ai_msg = AIMessage(content="", tool_calls=tool_calls)
    state = {"messages": [ai_msg]}
    runtime = _make_runtime(context)
    return state, runtime, ai_msg, context


IDENTITY_CALL = {
    "name": "salesforce_create_note",
    "args": {"note": "hi"},
    "id": "call-1",
}
IDENTITY_CALL_2 = {
    "name": "salesforce_create_note",
    "args": {"note": "again"},
    "id": "call-2",
}
PLAIN_CALL = {"name": "other_tool", "args": {}, "id": "call-3"}


def _call_ids(ai_msg):
    return [tc["id"] for tc in ai_msg.tool_calls]


class TestIdentityConsentMiddleware:
    async def test_pass_through_with_grant(self):
        state, runtime, ai_msg, _ = _make_state_and_runtime(
            {SERVER_SLUG: {"granted": True}},
            [IDENTITY_CALL],
        )
        result = await IdentityConsentMiddleware().aafter_model(state, runtime)
        assert result is None
        assert _call_ids(ai_msg) == ["call-1"]

    async def test_pass_through_without_identity_calls(self):
        state, runtime, _, _ = _make_state_and_runtime({}, [PLAIN_CALL])
        result = await IdentityConsentMiddleware().aafter_model(state, runtime)
        assert result is None

    async def test_remembered_denial_blocks_without_prompt_and_keeps_pairing(
        self, monkeypatch
    ):
        state, runtime, ai_msg, context = _make_state_and_runtime(
            {SERVER_SLUG: {"granted": False}},
            [IDENTITY_CALL, PLAIN_CALL],
        )
        interrupt_mock = MagicMock(side_effect=AssertionError("must not prompt again"))
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        # Tool calls are KEPT — the artificial ToolMessage answers the blocked
        # call so the tool_use/tool_result pairing survives for the provider.
        assert _call_ids(ai_msg) == ["call-1", "call-3"]
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 1
        payload = json.loads(tool_messages[0].content)
        assert payload["error_code"] == "auth_required"
        assert tool_messages[0].tool_call_id == "call-1"
        assert context._pending_identity_consents == []  # nothing new to persist

    async def test_first_use_approve_records_grant_and_keeps_call(self, monkeypatch):
        state, runtime, ai_msg, context = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "approve"}]})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        # Consent prompt raised with the HITL payload shape, metadata riding in
        # the _risk_metadata envelope clients already hide.
        hitl_request = interrupt_mock.call_args[0][0]
        request_args = hitl_request["action_requests"][0]["args"]
        assert hitl_request["action_requests"][0]["name"] == SERVER_SLUG
        assert request_args["_call_id"] == "call-1"
        assert request_args["_risk_metadata"]["source"] == "identity_consent"
        assert "_consent_metadata" not in request_args
        assert hitl_request["review_configs"][0]["allowed_decisions"] == [
            "approve",
            "reject",
        ]
        # Grant remembered in-session and queued for persistence (keyed by server slug)
        assert context.identity_consent_grants[SERVER_SLUG] == {"granted": True}
        assert context._pending_identity_consents == [
            {"server_slug": SERVER_SLUG, "granted": True}
        ]
        # Tool call survives, nothing artificial
        assert _call_ids(ai_msg) == ["call-1"]
        assert result is None

    async def test_first_use_reject_records_denial_and_blocks(self, monkeypatch):
        state, runtime, ai_msg, context = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "reject"}]})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert context.identity_consent_grants[SERVER_SLUG] == {"granted": False}
        assert context._pending_identity_consents == [
            {"server_slug": SERVER_SLUG, "granted": False}
        ]
        # Call kept, answered artificially
        assert _call_ids(ai_msg) == ["call-1"]
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert json.loads(tool_messages[0].content)["error_code"] == "auth_required"

    async def test_same_tool_twice_asks_once_and_applies_to_both(self, monkeypatch):
        """Consent is per (user, server): repeat calls share one question."""
        state, runtime, ai_msg, context = _make_state_and_runtime(
            {}, [IDENTITY_CALL, IDENTITY_CALL_2]
        )
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "reject"}]})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        hitl_request = interrupt_mock.call_args[0][0]
        assert len(hitl_request["action_requests"]) == 1  # one question per tool
        # A single decision covers both calls: one denial recorded, both blocked
        assert context._pending_identity_consents == [
            {"server_slug": SERVER_SLUG, "granted": False}
        ]
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert sorted(m.tool_call_id for m in tool_messages) == ["call-1", "call-2"]

    async def test_missing_email_blocks_without_prompt_or_grant(self, monkeypatch):
        """No verified email → no prompt, no remembered answer, call blocked."""
        state, runtime, ai_msg, context = _make_state_and_runtime(
            {}, [IDENTITY_CALL], email=None
        )
        interrupt_mock = MagicMock(
            side_effect=AssertionError("must not prompt without email")
        )
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert context.identity_consent_grants == {}
        assert context._pending_identity_consents == []
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        payload = json.loads(tool_messages[0].content)
        assert payload["error_code"] == "auth_required"
        assert "declined" not in payload["message"]  # must not claim a denial exists

    async def test_self_slug_blocks_without_prompt_or_grant(self, monkeypatch):
        """Tool with no resolvable server → slug '_self': a grant under it would
        lump every server-less tool into one answer. Block without prompting
        or remembering."""
        state, runtime, ai_msg, context = _make_state_and_runtime(
            {}, [IDENTITY_CALL], server_name=None
        )
        interrupt_mock = MagicMock(
            side_effect=AssertionError("must not prompt under the _self slug")
        )
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert context.identity_consent_grants == {}
        assert context._pending_identity_consents == []
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        payload = json.loads(tool_messages[0].content)
        assert payload["error_code"] == "auth_required"
        assert "declined" not in payload["message"]

    async def test_unknown_decision_blocks_without_remembering(self, monkeypatch):
        """A malformed decision blocks this call but records no denial — and says so."""
        state, runtime, ai_msg, context = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "unknown"}]})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert context.identity_consent_grants == {}
        assert context._pending_identity_consents == []
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        payload = json.loads(tool_messages[0].content)
        assert payload["error_code"] == "auth_required"
        assert "declined" not in payload["message"]  # no durable denial was recorded

    async def test_defaulted_reject_blocks_without_remembering(self, monkeypatch):
        """The executor's synthesized safe reject (missing/stale call_id) must not
        record a durable denial — the user never actually answered."""
        state, runtime, ai_msg, context = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(
            return_value={"decisions": [{"type": "reject", "_defaulted": True}]}
        )
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert context.identity_consent_grants == {}
        assert context._pending_identity_consents == []
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        payload = json.loads(tool_messages[0].content)
        assert payload["error_code"] == "auth_required"
        assert "declined" not in payload["message"]  # no durable denial was recorded

    async def test_decision_count_mismatch_raises(self, monkeypatch):
        state, runtime, _, _ = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": []})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        with pytest.raises(ValueError, match="does not match"):
            await IdentityConsentMiddleware().aafter_model(state, runtime)


class TestSubAgentConsentGate:
    """The gate must also work on sub-agent graphs (ADR 0006).

    A sub-agent's runtime context carries no ``tool_registry`` (outside GP's
    catalog mode) and its own pending-consent list is the orchestrator's, so the
    middleware is constructed with an explicit registry and records answers onto
    the shared objects.
    """

    def _subagent_state_and_runtime(self, grants, pending):
        """A sub-agent-shaped context: no tool_registry, shared grants/pending objects."""
        context = types.SimpleNamespace(
            identity_consent_grants=grants,
            _pending_identity_consents=pending,
            email="user@example.com",
        )
        ai_msg = AIMessage(content="", tool_calls=[IDENTITY_CALL])
        return {"messages": [ai_msg]}, _make_runtime(context), context

    def _registry(self):
        return {
            "salesforce_create_note": wrap_identity_scoped_tool(_make_inner_tool({}))
        }

    async def test_prompts_with_injected_registry_when_context_has_none(
        self, monkeypatch
    ):
        grants, pending = {}, []
        state, runtime, context = self._subagent_state_and_runtime(grants, pending)
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "approve"}]})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        middleware = IdentityConsentMiddleware(tool_registry=self._registry())
        result = await middleware.aafter_model(state, runtime)

        interrupt_mock.assert_called_once()
        assert result is None  # approved: the call runs, nothing is blocked
        # Recorded on the orchestrator's own objects, so the turn persists it.
        assert grants == {SERVER_SLUG: {"granted": True}}
        assert pending == [{"server_slug": SERVER_SLUG, "granted": True}]
        assert context.identity_consent_grants is grants

    async def test_without_registry_no_prompt(self, monkeypatch):
        """Unknown tool → not gated here; the wrapper still fails closed at execution."""
        state, runtime, _ = self._subagent_state_and_runtime({}, [])
        interrupt_mock = MagicMock()
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        interrupt_mock.assert_not_called()
        assert result is None

    async def test_grant_from_orchestrator_passes_through(self):
        state, runtime, _ = self._subagent_state_and_runtime(
            {SERVER_SLUG: {"granted": True}}, []
        )
        middleware = IdentityConsentMiddleware(tool_registry=self._registry())
        assert await middleware.aafter_model(state, runtime) is None


def test_common_middleware_stack_gates_identity_after_hitl():
    """The shared stack always carries the gate, positioned after the risk HITL.

    after_model hooks run in reverse list order, so "after in the list" means the
    consent gate runs *first* and the risk HITL then skips calls it already
    answered with an auth_required ToolMessage.
    """
    from agent_common.core.graph_utils import build_common_middleware_stack

    stack = build_common_middleware_stack(
        MagicMock(),
        MagicMock(),
        exclude_deep_agents_middlewares=True,
        hitl_guarded_tools={"salesforce_create_note": {}},
    )
    names = [m.__class__.__name__ for m in stack]
    assert (
        names.index("IdentityConsentMiddleware")
        == names.index("ConditionalHumanInTheLoopMiddleware") + 1
    )


def test_common_middleware_stack_gates_identity_without_hitl():
    from agent_common.core.graph_utils import build_common_middleware_stack

    stack = build_common_middleware_stack(
        MagicMock(), MagicMock(), exclude_deep_agents_middlewares=True
    )
    assert "IdentityConsentMiddleware" in [m.__class__.__name__ for m in stack]


class TestPtcConsentRequest:
    """Inside ``eval`` the wrapper queues a consent question instead of dead-ending.

    ``interrupt()`` is impossible on the PTC bridge's task, so the question goes
    onto the same per-turn collector the risk guard uses; the code interpreter's
    ``awrap_tool_call`` drains it, interrupts once, and re-runs the code.
    """

    def _runtime(self, grants, thread_id="ptc-t1", email="user@example.com"):
        context = types.SimpleNamespace(
            identity_consent_grants=grants,
            _pending_identity_consents=[],
            email=email,
            tool_server_map={"salesforce_create_note": "gatana-salesforce"},
        )
        runtime = _make_runtime(context)
        runtime.config = {"configurable": {"thread_id": thread_id}}
        return runtime, context

    async def test_unasked_call_records_pending_consent(self):
        from agent_common.middleware import ptc_guard

        captured: dict = {}
        wrapped = wrap_identity_scoped_tool(_make_inner_tool(captured))
        runtime, _ = self._runtime({})
        ptc_guard.begin_ptc_turn("ptc-t1")
        try:
            out = await wrapped.coroutine(runtime=runtime, note="hi")
            assert json.loads(out)["error_code"] == "auth_required"
            assert captured == {}  # inner never ran
            pending = ptc_guard.take_ptc_pending("ptc-t1")
            assert len(pending) == 1
            assert pending[0].identity_consent is True
            assert pending[0].tool_name == "salesforce_create_note"
            assert pending[0].server_slug == "gatana-salesforce"
            assert pending[0].allowed_actions == ["approve", "reject"]
        finally:
            ptc_guard.end_ptc_turn("ptc-t1")

    async def test_self_slug_never_queues_consent(self):
        """Tool with no resolvable server → slug '_self': the wrapper fails
        closed without queueing a consent question (a grant under '_self'
        would lump every server-less tool into one answer)."""
        from agent_common.middleware import ptc_guard

        captured: dict = {}
        wrapped = wrap_identity_scoped_tool(
            _make_inner_tool(captured, server_name=None)
        )
        runtime, _ = self._runtime({}, thread_id="ptc-t-noauth")
        runtime.context.tool_server_map = {}
        ptc_guard.begin_ptc_turn("ptc-t-noauth")
        try:
            out = await wrapped.coroutine(runtime=runtime, note="hi")
            assert json.loads(out)["error_code"] == "auth_required"
            assert captured == {}
            assert ptc_guard.take_ptc_pending("ptc-t-noauth") == []
        finally:
            ptc_guard.end_ptc_turn("ptc-t-noauth")

    async def test_repeated_calls_ask_once(self):
        from agent_common.middleware import ptc_guard

        wrapped = wrap_identity_scoped_tool(_make_inner_tool({}))
        runtime, _ = self._runtime({}, thread_id="ptc-t2")
        ptc_guard.begin_ptc_turn("ptc-t2")
        try:
            await wrapped.coroutine(runtime=runtime, note="one")
            await wrapped.coroutine(runtime=runtime, note="two")
            # Consent is per tool, so differing args must not produce two questions.
            assert len(ptc_guard.take_ptc_pending("ptc-t2")) == 1
        finally:
            ptc_guard.end_ptc_turn("ptc-t2")

    async def test_no_ptc_turn_fails_closed_without_pending(self):
        captured: dict = {}
        wrapped = wrap_identity_scoped_tool(_make_inner_tool(captured))
        runtime, _ = self._runtime({}, thread_id="ptc-absent")
        out = await wrapped.coroutine(runtime=runtime, note="hi")
        assert json.loads(out)["error_code"] == "auth_required"
        assert captured == {}

    async def test_remembered_denial_does_not_re_ask(self):
        from agent_common.middleware import ptc_guard

        wrapped = wrap_identity_scoped_tool(_make_inner_tool({}))
        runtime, _ = self._runtime(
            {SERVER_SLUG: {"granted": False}}, thread_id="ptc-t3"
        )
        ptc_guard.begin_ptc_turn("ptc-t3")
        try:
            out = await wrapped.coroutine(runtime=runtime, note="hi")
            assert "declined" in json.loads(out)["message"]
            assert ptc_guard.take_ptc_pending("ptc-t3") == []
        finally:
            ptc_guard.end_ptc_turn("ptc-t3")

    async def test_missing_email_fails_closed_without_pending(self):
        from agent_common.middleware import ptc_guard

        wrapped = wrap_identity_scoped_tool(_make_inner_tool({}))
        runtime, _ = self._runtime({}, thread_id="ptc-t4", email=None)
        ptc_guard.begin_ptc_turn("ptc-t4")
        try:
            out = await wrapped.coroutine(runtime=runtime, note="hi")
            assert json.loads(out)["error_code"] == "auth_required"
            assert ptc_guard.take_ptc_pending("ptc-t4") == []
        finally:
            ptc_guard.end_ptc_turn("ptc-t4")

    async def test_granted_after_resume_executes_with_forced_email(self):
        """The re-run after approval: the grant is on the context, so the call runs."""
        from agent_common.middleware import ptc_guard

        captured: dict = {}
        wrapped = wrap_identity_scoped_tool(_make_inner_tool(captured))
        runtime, _ = self._runtime({SERVER_SLUG: {"granted": True}}, thread_id="ptc-t5")
        ptc_guard.begin_ptc_turn("ptc-t5")
        try:
            assert await wrapped.coroutine(runtime=runtime, note="hi") == "ok"
            assert captured[NANNOS_USER_IDENTITY_FIELD] == "user@example.com"
            assert ptc_guard.take_ptc_pending("ptc-t5") == []
        finally:
            ptc_guard.end_ptc_turn("ptc-t5")


class TestPtcConsentDecisions:
    """``_apply_ptc_decisions`` records consent answers on the runtime context."""

    def _pending(self):
        from agent_common.middleware.ptc_guard import _PendingApproval

        return _PendingApproval(
            call_key=f"identity_consent:{SERVER_SLUG}",
            tool_name="salesforce_create_note",
            args={},
            server_slug="gatana-salesforce",
            allowed_actions=["approve", "reject"],
            score=0.0,
            threshold=0.0,
            matched_pattern=None,
            identity_consent=True,
        )

    def _apply(self, decision):
        from agent_common.core.graph_utils import _PTCToleranceCodeInterpreterMiddleware

        context = types.SimpleNamespace(
            identity_consent_grants={}, _pending_identity_consents=[]
        )
        turn = types.SimpleNamespace(decisions={})
        p = self._pending()
        _PTCToleranceCodeInterpreterMiddleware._apply_ptc_decisions(
            turn, [p], [{"id": p.call_key, **decision}], context
        )
        return context, turn

    def test_approve_records_grant(self):
        context, turn = self._apply({"type": "approve"})
        assert context.identity_consent_grants == {SERVER_SLUG: {"granted": True}}
        assert context._pending_identity_consents == [
            {"server_slug": SERVER_SLUG, "granted": True}
        ]
        # Not routed through turn.decisions — the wrapper reads the grant instead.
        assert turn.decisions == {}

    def test_reject_records_denial(self):
        context, _ = self._apply({"type": "reject"})
        assert context.identity_consent_grants == {SERVER_SLUG: {"granted": False}}

    def test_defaulted_reject_records_nothing(self):
        context, _ = self._apply({"type": "reject", "_defaulted": True})
        assert context.identity_consent_grants == {}
        assert context._pending_identity_consents == []

    def test_hitl_request_uses_identity_consent_source(self):
        from agent_common.core.graph_utils import _PTCToleranceCodeInterpreterMiddleware

        request = _PTCToleranceCodeInterpreterMiddleware._build_ptc_hitl_request(
            [self._pending()]
        )
        action = request["action_requests"][0]
        # Named after the integration, like the non-PTC gate's card.
        assert action["name"] == SERVER_SLUG
        assert request["review_configs"][0]["action_name"] == SERVER_SLUG
        assert action["args"]["_risk_metadata"]["source"] == "identity_consent"
        assert "verified email address" in action["description"]


class TestCatalogCallToolGating:
    """Catalog mode (PTC off): the model dispatches via ``call_tool({name, args})``.

    ToolCatalogMiddleware rewrites that to the real tool only at wrap_tool_call
    time — after this gate — so the gate reads the inner name itself.
    """

    def _state_and_runtime(self, grants, inner_name="salesforce_create_note"):
        registry = {
            "salesforce_create_note": wrap_identity_scoped_tool(_make_inner_tool({}))
        }
        context = _make_context(grants=grants, tool_registry=registry)
        call = {
            "name": "call_tool",
            "args": {"name": inner_name, "args": {"note": "hi"}},
            "id": "call-ct",
        }
        return (
            {"messages": [AIMessage(content="", tool_calls=[call])]},
            _make_runtime(context),
            context,
        )

    async def test_prompts_for_inner_identity_tool(self, monkeypatch):
        state, runtime, context = self._state_and_runtime({})
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "approve"}]})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert result is None
        request = interrupt_mock.call_args[0][0]
        assert request["action_requests"][0]["name"] == SERVER_SLUG
        assert context.identity_consent_grants == {SERVER_SLUG: {"granted": True}}

    async def test_denied_inner_tool_blocks_with_paired_message(self):
        state, runtime, _ = self._state_and_runtime({SERVER_SLUG: {"granted": False}})
        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        message = result["messages"][0]
        # Pairing uses the call's own name/id; the payload names the gated tool.
        assert message.name == "call_tool"
        assert message.tool_call_id == "call-ct"
        assert "salesforce_create_note" in json.loads(message.content)["message"]

    async def test_plain_call_tool_dispatch_untouched(self):
        state, runtime, _ = self._state_and_runtime({}, inner_name="some_other_tool")
        assert await IdentityConsentMiddleware().aafter_model(state, runtime) is None


class TestPerServerConsent:
    """Consent is keyed by MCP server: one answer covers the whole integration."""

    def _registry(self):
        return {
            name: wrap_identity_scoped_tool(_make_inner_tool({}, name=name))
            for name in ("salesforce_create_note", "salesforce_list_my_tasks")
        }

    def _state(self, grants, tool_calls, registry=None):
        context = _make_context(
            grants=grants, tool_registry=registry or self._registry()
        )
        ai_msg = AIMessage(content="", tool_calls=tool_calls)
        return {"messages": [ai_msg]}, _make_runtime(context), context

    def _call(self, name, call_id):
        return {"name": name, "args": {"note": "hi"}, "id": call_id}

    async def test_one_question_for_two_tools_of_the_same_server(self, monkeypatch):
        state, runtime, context = self._state(
            {},
            [
                self._call("salesforce_create_note", "call-a"),
                self._call("salesforce_list_my_tasks", "call-b"),
            ],
        )
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "approve"}]})
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        request = interrupt_mock.call_args[0][0]
        assert len(request["action_requests"]) == 1  # one integration, one question
        assert request["action_requests"][0]["name"] == SERVER_SLUG
        assert result is None  # both calls proceed on approval
        assert context.identity_consent_grants == {SERVER_SLUG: {"granted": True}}

    async def test_grant_covers_a_tool_never_asked_about(self):
        """The point of server-keying: a sibling tool needs no second approval."""
        state, runtime, _ = self._state(
            {SERVER_SLUG: {"granted": True}},
            [self._call("salesforce_list_my_tasks", "call-b")],
        )
        assert await IdentityConsentMiddleware().aafter_model(state, runtime) is None

    async def test_separate_servers_ask_separately(self, monkeypatch):
        registry = {
            "salesforce_create_note": wrap_identity_scoped_tool(_make_inner_tool({})),
            "github_list_my_issues": wrap_identity_scoped_tool(
                _make_inner_tool(
                    {}, server_name="gatana-github", name="github_list_my_issues"
                )
            ),
        }
        state, runtime, context = self._state(
            {},
            [
                self._call("salesforce_create_note", "call-a"),
                self._call("github_list_my_issues", "call-b"),
            ],
            registry=registry,
        )
        interrupt_mock = MagicMock(
            return_value={"decisions": [{"type": "approve"}, {"type": "reject"}]}
        )
        monkeypatch.setattr(
            "agent_common.middleware.identity_consent.interrupt", interrupt_mock
        )

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        names = [a["name"] for a in interrupt_mock.call_args[0][0]["action_requests"]]
        assert names == [SERVER_SLUG, "gatana-github"]
        assert context.identity_consent_grants == {
            SERVER_SLUG: {"granted": True},
            "gatana-github": {"granted": False},
        }
        # Only the rejected integration's call is blocked.
        blocked = [m.tool_call_id for m in result["messages"]]
        assert blocked == ["call-b"]

    async def test_context_map_wins_over_tool_metadata(self):
        """Sub-agent rediscovery mis-tags metadata with the gateway name.

        The orchestrator's tool_server_map is threaded onto the context precisely
        so the slug — and therefore the grant — matches across execution paths.
        """
        tool = wrap_identity_scoped_tool(_make_inner_tool({}, server_name="gatana"))
        context = _make_context(
            grants={SERVER_SLUG: {"granted": True}},
            tool_registry={"salesforce_create_note": tool},
        )
        context.tool_server_map = {"salesforce_create_note": SERVER_SLUG}
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[self._call("salesforce_create_note", "call-a")],
                )
            ]
        }
        result = await IdentityConsentMiddleware().aafter_model(
            state, _make_runtime(context)
        )
        assert result is None  # grant matched despite the gateway-name metadata

    async def test_wrapper_resolves_slug_from_context_map(self):
        """Same resolution at execution time: the wrapper honours the context map."""
        captured: dict = {}
        wrapped = wrap_identity_scoped_tool(
            _make_inner_tool(captured, server_name="gatana")
        )
        context = _make_context(grants={SERVER_SLUG: {"granted": True}})
        context.tool_server_map = {"salesforce_create_note": SERVER_SLUG}
        assert (
            await wrapped.coroutine(runtime=_make_runtime(context), note="hi") == "ok"
        )
        assert captured[NANNOS_USER_IDENTITY_FIELD] == "user@example.com"
