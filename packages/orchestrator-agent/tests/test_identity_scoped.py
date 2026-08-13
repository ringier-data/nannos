"""Tests for identity-scoped tool support (ADR 0006, Gate 3)."""

import json
import types
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from app.middleware.identity_scoped import (
    NANNOS_USER_IDENTITY_FIELD,
    IdentityConsentMiddleware,
    is_identity_scoped_tool,
    is_wrapped_identity_scoped_tool,
    wrap_identity_scoped_tool,
    wrap_identity_scoped_tools,
)

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

PLAIN_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


def _make_inner_tool(
    captured: dict, schema=None, server_name: str = "gatana-salesforce"
):
    async def _run(**kwargs):
        captured.update(kwargs)
        return "ok"

    return StructuredTool(
        name="salesforce_create_note",
        description="Create a personal note",
        args_schema=schema or IDENTITY_SCHEMA,
        coroutine=_run,
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

    def test_wrapper_hides_reserved_field_from_schema(self):
        wrapper = wrap_identity_scoped_tool(_make_inner_tool({}))
        assert NANNOS_USER_IDENTITY_FIELD not in wrapper.args_schema["properties"]
        assert NANNOS_USER_IDENTITY_FIELD not in wrapper.args_schema.get("required", [])
        # Other fields survive
        assert "note" in wrapper.args_schema["properties"]
        assert wrapper.args_schema["required"] == ["note"]

    def test_wrapper_keeps_name_description_and_metadata(self):
        inner = _make_inner_tool({})
        wrapper = wrap_identity_scoped_tool(inner)
        assert wrapper.name == inner.name
        assert wrapper.description == inner.description
        assert wrapper.metadata["server_name"] == "gatana-salesforce"
        assert is_wrapped_identity_scoped_tool(wrapper)

    def test_wrap_registry_in_place_only_identity_tools(self):
        identity_tool = _make_inner_tool({})
        plain_tool = _make_inner_tool({}, schema=PLAIN_SCHEMA)
        plain_tool.name = "plain_tool"
        registry = {"salesforce_create_note": identity_tool, "plain_tool": plain_tool}

        wrapped = wrap_identity_scoped_tools(registry)

        assert wrapped == ["salesforce_create_note"]
        assert is_wrapped_identity_scoped_tool(registry["salesforce_create_note"])
        assert registry["plain_tool"] is plain_tool

    def test_wrap_registry_is_idempotent(self):
        registry = {"salesforce_create_note": _make_inner_tool({})}
        wrap_identity_scoped_tools(registry)
        once = registry["salesforce_create_note"]
        assert wrap_identity_scoped_tools(registry) == []
        assert registry["salesforce_create_note"] is once


# ---------------------------------------------------------------------------
# Wrapper execution: force-populate + fail closed
# ---------------------------------------------------------------------------


class TestWrapperExecution:
    async def test_injects_verified_email_with_grant(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(
            grants={"salesforce_create_note::gatana-salesforce": {"granted": True}}
        )

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        assert result == "ok"
        assert captured[NANNOS_USER_IDENTITY_FIELD] == "user@example.com"
        assert captured["note"] == "hello"

    async def test_overwrites_model_supplied_identity(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(
            grants={"salesforce_create_note::gatana-salesforce": {"granted": True}}
        )

        await wrapper.coroutine(
            runtime=_make_runtime(context),
            note="hello",
            **{NANNOS_USER_IDENTITY_FIELD: "attacker@evil.example"},
        )

        assert captured[NANNOS_USER_IDENTITY_FIELD] == "user@example.com"

    async def test_fails_closed_without_grant(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(grants={})

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        payload = json.loads(result)
        assert payload["error"] is True
        assert payload["error_code"] == "auth_required"
        assert captured == {}  # inner never called

    async def test_fails_closed_on_remembered_denial(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(
            grants={"salesforce_create_note::gatana-salesforce": {"granted": False}}
        )

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        payload = json.loads(result)
        assert payload["error_code"] == "auth_required"
        assert "declined" in payload["message"]
        assert captured == {}

    async def test_fails_closed_without_email(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))
        context = _make_context(
            grants={"salesforce_create_note::gatana-salesforce": {"granted": True}},
            email=None,
        )

        result = await wrapper.coroutine(runtime=_make_runtime(context), note="hello")

        assert json.loads(result)["error_code"] == "auth_required"
        assert captured == {}

    async def test_fails_closed_without_runtime_context(self):
        captured: dict = {}
        wrapper = wrap_identity_scoped_tool(_make_inner_tool(captured))

        result = await wrapper.coroutine(runtime=None, note="hello")

        assert json.loads(result)["error_code"] == "auth_required"
        assert captured == {}


# ---------------------------------------------------------------------------
# Consent middleware (Gate 3)
# ---------------------------------------------------------------------------


def _make_state_and_runtime(grants, tool_calls):
    registry = {
        "salesforce_create_note": wrap_identity_scoped_tool(_make_inner_tool({}))
    }
    context = _make_context(grants=grants, tool_registry=registry)
    ai_msg = AIMessage(content="", tool_calls=tool_calls)
    state = {"messages": [ai_msg]}
    runtime = _make_runtime(context)
    return state, runtime, ai_msg, context


IDENTITY_CALL = {
    "name": "salesforce_create_note",
    "args": {"note": "hi"},
    "id": "call-1",
}
PLAIN_CALL = {"name": "other_tool", "args": {}, "id": "call-2"}


class TestIdentityConsentMiddleware:
    async def test_pass_through_with_grant(self):
        state, runtime, ai_msg, _ = _make_state_and_runtime(
            {"salesforce_create_note::gatana-salesforce": {"granted": True}},
            [IDENTITY_CALL],
        )
        result = await IdentityConsentMiddleware().aafter_model(state, runtime)
        assert result is None
        assert [tc["id"] for tc in ai_msg.tool_calls] == ["call-1"]

    async def test_pass_through_without_identity_calls(self):
        state, runtime, _, _ = _make_state_and_runtime({}, [PLAIN_CALL])
        result = await IdentityConsentMiddleware().aafter_model(state, runtime)
        assert result is None

    async def test_remembered_denial_blocks_without_prompt(self, monkeypatch):
        state, runtime, ai_msg, context = _make_state_and_runtime(
            {"salesforce_create_note::gatana-salesforce": {"granted": False}},
            [IDENTITY_CALL, PLAIN_CALL],
        )
        interrupt_mock = MagicMock(side_effect=AssertionError("must not prompt again"))
        monkeypatch.setattr("app.middleware.identity_scoped.interrupt", interrupt_mock)

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert [tc["id"] for tc in ai_msg.tool_calls] == [
            "call-2"
        ]  # only the identity call dropped
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 1
        payload = json.loads(tool_messages[0].content)
        assert payload["error_code"] == "auth_required"
        assert tool_messages[0].tool_call_id == "call-1"
        assert context._pending_identity_consents == []  # nothing new to persist

    async def test_first_use_approve_records_grant_and_keeps_call(self, monkeypatch):
        state, runtime, ai_msg, context = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "approve"}]})
        monkeypatch.setattr("app.middleware.identity_scoped.interrupt", interrupt_mock)

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        # Consent prompt raised with the HITL payload shape
        hitl_request = interrupt_mock.call_args[0][0]
        assert hitl_request["action_requests"][0]["name"] == "salesforce_create_note"
        assert hitl_request["action_requests"][0]["args"]["_call_id"] == "call-1"
        assert hitl_request["review_configs"][0]["allowed_decisions"] == [
            "approve",
            "reject",
        ]
        # Grant remembered in-session and queued for persistence
        key = "salesforce_create_note::gatana-salesforce"
        assert context.identity_consent_grants[key] == {"granted": True}
        assert context._pending_identity_consents == [
            {"key": key, "grant": {"granted": True}}
        ]
        # Tool call survives
        assert [tc["id"] for tc in ai_msg.tool_calls] == ["call-1"]
        assert [m for m in result["messages"] if isinstance(m, ToolMessage)] == []

    async def test_first_use_reject_records_denial_and_blocks(self, monkeypatch):
        state, runtime, ai_msg, context = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "reject"}]})
        monkeypatch.setattr("app.middleware.identity_scoped.interrupt", interrupt_mock)

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        key = "salesforce_create_note::gatana-salesforce"
        assert context.identity_consent_grants[key] == {"granted": False}
        assert context._pending_identity_consents == [
            {"key": key, "grant": {"granted": False}}
        ]
        assert ai_msg.tool_calls == []
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert json.loads(tool_messages[0].content)["error_code"] == "auth_required"

    async def test_unknown_decision_blocks_without_remembering(self, monkeypatch):
        """A defaulted/missing decision blocks this call but records no denial."""
        state, runtime, ai_msg, context = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": [{"type": "unknown"}]})
        monkeypatch.setattr("app.middleware.identity_scoped.interrupt", interrupt_mock)

        result = await IdentityConsentMiddleware().aafter_model(state, runtime)

        assert context.identity_consent_grants == {}
        assert context._pending_identity_consents == []
        assert ai_msg.tool_calls == []
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 1

    async def test_decision_count_mismatch_raises(self, monkeypatch):
        state, runtime, _, _ = _make_state_and_runtime({}, [IDENTITY_CALL])
        interrupt_mock = MagicMock(return_value={"decisions": []})
        monkeypatch.setattr("app.middleware.identity_scoped.interrupt", interrupt_mock)

        with pytest.raises(ValueError, match="does not match"):
            await IdentityConsentMiddleware().aafter_model(state, runtime)
