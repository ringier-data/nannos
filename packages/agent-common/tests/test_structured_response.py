"""Tests for get_response_format strategy selection.

Strategy now follows from the model's gateway *provider*, not the client class —
every client is a ChatOpenAI talking to the LiteLLM gateway, so we patch
``get_model_provider`` to drive the branch.
"""

from unittest.mock import patch

import pytest
from agent_common.a2a.structured_response import (
    StructuredResponseMixin,
    SubAgentResponseSchema,
    get_response_format,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain.agents.structured_output import ToolStrategy

_PATCH_TARGET = "agent_common.a2a.structured_response.get_model_provider"
_CAPS_TARGET = "agent_common.a2a.structured_response.get_model_capabilities"


def _with_provider(provider: str, caps: dict | None = None):
    """Drive the provider branch; the alias is unprobed unless ``caps`` says otherwise."""
    outer = patch(_PATCH_TARGET, return_value=provider)
    inner = patch(_CAPS_TARGET, return_value=caps or {})

    class _Both:
        def __enter__(self):
            outer.__enter__()
            inner.__enter__()
            return self

        def __exit__(self, *exc):
            inner.__exit__(*exc)
            return outer.__exit__(*exc)

    return _Both()


@pytest.mark.parametrize(
    "provider",
    ["openai", "azure", "vertex_ai", "gemini", "bedrock_converse", ""],
)
def test_no_thinking_always_uses_tool_strategy(provider: str):
    """Without thinking, every provider uses ToolStrategy and leaves tools untouched.

    The gateway normalizes provider responses (incl. Gemini) into OpenAI-shape
    tool_calls, so ToolStrategy works uniformly. AutoStrategy would resolve to the
    native .parse() path, which requires every bound tool to be strict — dynamic
    MCP tools are not.
    """
    tools: list = []
    with _with_provider(provider):
        fmt = get_response_format("some-model", tools, thinking_enabled=False)

    assert isinstance(fmt, ToolStrategy)
    assert fmt.schema is SubAgentResponseSchema
    assert tools == []


@pytest.mark.parametrize(
    "provider",
    ["bedrock_converse", "bedrock", "anthropic", "gemini", "vertex_ai", ""],
)
def test_thinking_on_non_openai_provider_binds_tool(provider: str):
    """Thinking forces the bind-as-tool fallback on every provider NOT known to allow
    forced tool_choice with thinking.

    Anthropic/Bedrock explicitly reject it; Gemini/Vertex and an unknown/cold-cache
    provider ("") are treated as unsafe so a stale gateway snapshot can't force a
    forbidden combination.
    """
    tools: list = []
    with _with_provider(provider):
        fmt = get_response_format("some-model", tools, thinking_enabled=True)

    assert fmt is None
    assert len(tools) == 1
    assert tools[0].name == "SubAgentResponseSchema"


@pytest.mark.parametrize("provider", ["openai", "azure"])
def test_thinking_on_openai_like_keeps_tool_strategy(provider: str):
    """OpenAI/Azure are the only providers known-safe to force tool_choice with thinking,
    so they keep ToolStrategy."""
    tools: list = []
    with _with_provider(provider):
        fmt = get_response_format("some-model", tools, thinking_enabled=True)

    assert isinstance(fmt, ToolStrategy)
    assert fmt.schema is SubAgentResponseSchema
    assert tools == []


def test_none_model_type_uses_tool_strategy_without_calling_provider():
    """A None alias short-circuits without a gateway lookup. Without thinking it stays
    ToolStrategy (the safe default for the common path)."""
    tools: list = []
    with patch(_PATCH_TARGET) as mock_provider:
        fmt = get_response_format(None, tools, thinking_enabled=False)

    mock_provider.assert_not_called()
    assert isinstance(fmt, ToolStrategy)
    assert fmt.schema is SubAgentResponseSchema
    assert tools == []


# --- the registration probe's record (nannos#318) ----------------------------------------


@pytest.mark.parametrize("provider", ["bedrock_converse", "anthropic", "openai", "vertex_ai"])
def test_an_alias_recorded_as_rejecting_forced_tool_choice_binds_the_tool(provider: str):
    """Claude 5.5's case: no thinking, but the probe saw `tool_choice: required` 400. The
    gateway would downgrade the force on the serving deployment; the app asks for the shape
    the model accepts from the start, on every provider."""
    tools: list = []
    with _with_provider(provider, {"forced_tool_choice": False}):
        fmt = get_response_format("sonnet-5-5", tools, thinking_enabled=False)

    assert fmt is None
    assert [t.name for t in tools] == ["SubAgentResponseSchema"]


@pytest.mark.parametrize("caps", [{}, {"forced_tool_choice": True}, {"response_format": False}])
def test_an_unprobed_or_forceable_alias_keeps_tool_strategy(caps: dict):
    """The absence of a record is not a verdict, and the other flags say nothing about
    forcing."""
    tools: list = []
    with _with_provider("bedrock_converse", caps):
        fmt = get_response_format("some-model", tools, thinking_enabled=False)

    assert isinstance(fmt, ToolStrategy)
    assert tools == []


class _Translator(StructuredResponseMixin):
    """The mixin over stub builders: each records what it was handed."""

    name = "assistant"

    def _build_response_from_schema(self, schema, context_id, task_id):
        return ("schema", schema.message)

    def _build_success_response(self, content, context_id=None, task_id=None, **extra):
        return ("success", content)

    def _build_error_response(self, message, context_id=None, task_id=None, **extra):
        return ("error", message)


def _answered_turn(text: str, call_id: str) -> list:
    """One turn answered through the response tool, as ToolStrategy records it."""
    args = {"task_state": "completed", "message": text}
    return [
        HumanMessage(content=f"ask {call_id}"),
        AIMessage(content="", tool_calls=[{"name": "SubAgentResponseSchema", "args": args, "id": call_id}]),
        ToolMessage(content="ok", tool_call_id=call_id, name="SubAgentResponseSchema"),
    ]


def test_a_turn_that_ended_on_a_blocked_call_never_replays_the_previous_answer():
    """``structured_response`` is persisted state: after a turn ends on a loop block (an
    error result the model never reacted to) it still holds the PREVIOUS turn's answer."""
    previous = SubAgentResponseSchema(task_state="completed", message="the settings page shows...")
    messages = _answered_turn("the settings page shows...", "a1") + [
        HumanMessage(content="wait until the page loads"),
        AIMessage(content="", tool_calls=[{"name": "client_action", "args": {"kind": "read"}, "id": "r1"}]),
        ToolMessage(
            content="BLOCKED: client_action called 6 times with identical arguments", tool_call_id="r1", status="error"
        ),
    ]
    out = _Translator()._translate_agent_result({"structured_response": previous, "messages": messages}, None, None)
    assert out == ("success", "BLOCKED: client_action called 6 times with identical arguments")


def test_this_turns_structured_response_is_still_used():
    current = SubAgentResponseSchema(task_state="completed", message="it does not exist")
    messages = _answered_turn("earlier", "a1") + _answered_turn("it does not exist", "a2")
    out = _Translator()._translate_agent_result({"structured_response": current, "messages": messages}, None, None)
    assert out == ("schema", "it does not exist")


def test_without_structured_state_the_response_tool_call_is_read_from_this_turn_only():
    messages = _answered_turn("earlier", "a1") + _answered_turn("now", "a2")
    out = _Translator()._translate_agent_result({"messages": messages}, None, None)
    assert out == ("schema", "now")
