"""Behaviour we pin against upstream drift in the langchain / langchain-quickjs bump.

Each test names the upstream change it guards. They exist because each of these broke
silently: no import error, no exception, just a different outcome.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from langchain_core.messages import ToolCall

from agent_common.core.graph_utils import _quickjs_slot_of, _with_quickjs_slot
from agent_common.core.hitl_resume import CLICKED_REJECT_LEAD
from agent_common.middleware.conditional_hitl import ConditionalHumanInTheLoopMiddleware
from agent_common.middleware.tool_retry import FailureHandlingToolRetryMiddleware

_CONFIG = {"allowed_decisions": ["approve", "edit", "reject"]}
_CALL = ToolCall(type="tool_call", name="send_email", args={"to": "a@example.com"}, id="call-1")


def test_an_edit_replaces_the_models_arguments():
    """langchain >= 1.4 keeps the model's call and substitutes the edit from a state key
    only its own after_model writes. Ours replaces that hook, so the edit must land here."""
    decision = {"type": "edit", "edited_action": {"name": "send_email", "args": {"to": "b@example.com"}}}

    call, message = ConditionalHumanInTheLoopMiddleware._process_decision(decision, _CALL, _CONFIG)

    assert message is None
    assert call["args"] == {"to": "b@example.com"}
    assert call["id"] == "call-1"


def test_request_changes_without_new_arguments_is_not_run_and_reaches_the_model():
    """The card's "Request changes" sends words, not an edited_action: read as one it
    raised KeyError and ended the turn in "An unexpected error occurred"."""
    decision = {"type": "edit", "message": "use b@example.com instead"}

    _, message = ConditionalHumanInTheLoopMiddleware._process_decision(decision, _CALL, _CONFIG)

    assert message is not None and message.status == "error"
    assert "NOT executed" in message.content
    assert "use b@example.com instead" in message.content


def test_a_reject_reason_reaches_the_model_verbatim():
    """langchain >= 1.3.18 wraps a reason as "... with reason: <msg>"; our reasons are
    pre-framed and the refused-this-turn check matches on their lead."""
    reason = f"{CLICKED_REJECT_LEAD}, so this call was NOT executed."

    _, message = ConditionalHumanInTheLoopMiddleware._process_decision(
        {"type": "reject", "message": reason}, _CALL, _CONFIG
    )

    assert message.content == reason
    assert message.status == "error"


def test_a_decision_the_config_does_not_allow_is_refused():
    with pytest.raises(ValueError, match="not allowed"):
        ConditionalHumanInTheLoopMiddleware._process_decision(
            {"type": "respond", "message": "x"}, _CALL, _CONFIG
        )


@dataclasses.dataclass
class _Runtime:
    state: dict[str, Any]


class _Request:
    def __init__(self, state, runtime):
        self.state = state
        self.runtime = runtime

    def override(self, **kw):
        return _Request(kw.get("state", self.state), kw.get("runtime", self.runtime))


def test_an_eval_resumed_without_a_quickjs_slot_gets_one():
    """langchain-quickjs >= 0.3.7 writes the slot in before_agent and its eval tool raises
    without one; a thread paused mid-eval before the upgrade resumes straight into the
    tool node. The tool reads runtime.state, so the slot must reach the runtime too."""
    request = _Request({"messages": []}, _Runtime(state={"messages": []}))

    patched = _with_quickjs_slot(request)

    slot = _quickjs_slot_of(patched.state)
    assert slot is not None
    assert _quickjs_slot_of(patched.runtime.state) == slot


def test_an_existing_quickjs_slot_is_kept():
    request = _Request({"_quickjs_slot_id": "qjs_existing"}, _Runtime(state={"_quickjs_slot_id": "qjs_existing"}))

    assert _with_quickjs_slot(request) is request


class _ToolRequest:
    tool = None

    def __init__(self):
        self.tool_call = {"name": "jira_create", "id": "call-9", "args": {}}


def _retry_mw():
    return FailureHandlingToolRetryMiddleware(
        max_retries=2,
        initial_delay=0,
        retry_on=lambda exc: isinstance(exc, TimeoutError),
        on_failure=lambda exc: f"Tool call failed: {exc}",
    )


@pytest.mark.asyncio
async def test_an_error_retry_on_declines_reaches_the_model():
    """langchain >= 1.3.14 re-raises a declined error instead of calling on_failure;
    ToolNode then ends the turn. The model must see it instead."""
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        raise ValueError("400 project key required")

    result = await _retry_mw().awrap_tool_call(_ToolRequest(), handler)

    assert calls == 1, "a declined error is not retried"
    assert result.content == "Tool call failed: 400 project key required"
    assert result.status == "error"


@pytest.mark.asyncio
async def test_an_interrupt_still_propagates():
    from langgraph.errors import GraphInterrupt

    async def handler(_request):
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        await _retry_mw().awrap_tool_call(_ToolRequest(), handler)


def test_offloaded_result_names_stay_short_for_long_tool_call_ids():
    """Gemini ids from the gateway carry a multi-KB packed thought signature. An offload
    path built from one overflowed the Postgres store's index (``ProgramLimitExceeded``);
    deepagents >= 0.7 hashes any id over 128 bytes, which is all that prevents it now."""
    from deepagents.backends.utils import sanitize_tool_call_id

    long_id = "call_7acafe2be6f646eaa9562734ce9f__thought__" + "x" * 2700

    name = sanitize_tool_call_id(long_id)

    assert len(name.encode("utf-8")) <= 128
    assert name == sanitize_tool_call_id(long_id)
    assert name != sanitize_tool_call_id(long_id.replace("7acafe", "8bcafe"))
