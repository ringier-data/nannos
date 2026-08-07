"""Read orchestrator behaviour out of a finished LangGraph turn.

This is the assertion vocabulary shared by *both* tiers of integration test:
the cheap tier that fakes the model layer, and the real tier that drives a live
gateway. Both end up holding the same thing — a state-values dict — so both ask
it the same questions here rather than each re-deriving "which agent ran?" from
raw messages.

Getting the state values
------------------------
``OrchestratorDeepAgent.stream()`` already performs an end-of-stream
``aget_state`` and parks the result on the optional ``turn_state`` carrier
(``app/core/agent.py``). Tests pass their own carrier and read it back rather
than issuing a second ``aget_state``::

    turn_state = TurnState()
    async for _ in agent.stream(parts, user_config, config, turn_state=turn_state):
        pass
    assert delegated_agents(turn_state.final_values) == ["slack-client"]

What the orchestrator can actually see
--------------------------------------
Delegation is *not* a per-agent ``delegate_to_x`` tool. There is exactly one
``task`` tool and the target lives in ``args["subagent_type"]``, with the
instruction in ``args["description"]`` (see ``DynamicToolDispatchMiddleware``).

Sub-agent *internal* tool calls never appear here. A sub-agent runs in its own
process behind A2A and returns one blob, so asserting on e.g.
``send_slack_message`` from an orchestrator test can only ever fail —
``tool_calls()`` sees ``task``, ``write_todos``, ``FinalResponseSchema``,
``get_current_time``, docstore tools and the user's dynamic MCP tools.

Turn scoping
------------
Everything defaults to the *current turn* (messages after the last
``HumanMessage``), matching how production reasons about state. Checkpointed
threads accumulate history, so unscoped assertions would happily pass on a
delegation that happened two turns ago. Pass ``all_turns=True`` when a
multi-turn conversation is deliberately under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from app.handlers import StreamHandler

# The production definition of "this turn" — imported rather than reimplemented
# so eval assertions cannot drift from the behaviour the executor relies on.
_current_turn_messages = StreamHandler._extract_current_turn_messages

TASK_TOOL = "task"
FINAL_RESPONSE_TOOL = "FinalResponseSchema"


@dataclass(frozen=True)
class ToolCall:
    """One orchestrator-visible tool call, paired with its result if it landed."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    result: str | None = None
    """ToolMessage content, or None when the call never returned (interrupt, error)."""

    @property
    def completed(self) -> bool:
        return self.result is not None


@dataclass(frozen=True)
class Delegation:
    """A single ``task`` call: the orchestrator handing work to a sub-agent."""

    subagent: str
    description: str = ""
    id: str | None = None
    result: str | None = None

    @property
    def completed(self) -> bool:
        return self.result is not None


def _messages(values: Any, *, all_turns: bool = False) -> list:
    """Messages from ``values``, scoped to the current turn unless told otherwise.

    Tolerates a non-dict (None, StateSnapshot mid-refactor) the same way
    ``StreamHandler`` does, so a harness bug surfaces as an empty result rather
    than an AttributeError deep inside an assertion.
    """
    if not isinstance(values, dict):
        return []
    messages = values.get("messages") or []
    return list(messages) if all_turns else _current_turn_messages(messages)


def message_text(message: Any) -> str:
    """Flatten a message's content to plain text.

    Content is a bare string on some providers and a list of blocks on others
    (Bedrock). Only text blocks are kept — thinking/reasoning blocks are not
    part of what the user sees and must not leak into response assertions.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
        elif getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "".join(parts)


def tool_calls(values: Any, *, all_turns: bool = False) -> list[ToolCall]:
    """Every orchestrator-visible tool call, in the order the model emitted them.

    Results are matched back by ``tool_call_id``; a call with no matching
    ToolMessage keeps ``result=None`` rather than being dropped, so "the model
    tried to call X but it never completed" stays assertable.
    """
    messages = _messages(values, all_turns=all_turns)

    results: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.tool_call_id:
            results[msg.tool_call_id] = message_text(msg)

    calls: list[ToolCall] = []
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for call in getattr(msg, "tool_calls", None) or []:
            call_id = call.get("id")
            calls.append(
                ToolCall(
                    name=call.get("name", ""),
                    args=call.get("args") or {},
                    id=call_id,
                    result=results.get(call_id) if call_id else None,
                )
            )
    return calls


def tool_names(values: Any, *, all_turns: bool = False) -> list[str]:
    """Ordered, de-duplicated names of the tools the orchestrator called."""
    return _ordered_unique(call.name for call in tool_calls(values, all_turns=all_turns))


def delegations(values: Any, *, all_turns: bool = False) -> list[Delegation]:
    """The ``task`` calls, in order — who was asked to do what, and what came back."""
    return [
        Delegation(
            subagent=call.args.get("subagent_type", ""),
            description=call.args.get("description", "") or "",
            id=call.id,
            result=call.result,
        )
        for call in tool_calls(values, all_turns=all_turns)
        if call.name == TASK_TOOL
    ]


def delegated_agents(values: Any, *, all_turns: bool = False, completed_only: bool = False) -> list[str]:
    """Ordered, de-duplicated sub-agent names the orchestrator delegated to.

    ``completed_only=True`` narrows to delegations that actually returned,
    matching what ``StreamHandler._extract_recently_called_subagents`` counts as
    a sub-agent having run. The default includes attempted-but-unfinished calls,
    which is usually what a routing assertion means.
    """
    return _ordered_unique(
        d.subagent
        for d in delegations(values, all_turns=all_turns)
        if d.subagent and (d.completed or not completed_only)
    )


def a2a_tracking(values: Any) -> dict[str, dict[str, Any]]:
    """The ``a2a_tracking`` state channel, keyed by sub-agent name.

    Written by ``A2ATaskTrackingMiddleware`` and carries per-sub-agent
    ``task_id``/``context_id``/``state``/``is_complete``/``requires_auth``. This
    is a first-class delegation record — richer than message parsing, and the
    right source for multi-turn continuity assertions.

    Only registry (remote A2A) sub-agents appear; deepagents' built-in
    ``general-purpose`` does not, so absence here is not absence of delegation.
    """
    if not isinstance(values, dict):
        return {}
    tracking = values.get("a2a_tracking")
    return tracking if isinstance(tracking, dict) else {}


def final_response(values: Any) -> dict[str, Any] | None:
    """The turn's ``FinalResponseSchema`` envelope, or None if absent.

    Mirrors the precedence in ``StreamHandler.is_phantom_subagent_completion``:
    a ``FinalResponseSchema`` tool call *in the current turn* wins over the
    ``structured_response`` channel, which can still hold a previous turn's
    value. Asserting on the channel alone silently reads stale data.
    """
    if not isinstance(values, dict):
        return None

    for msg in reversed(_messages(values)):
        if not isinstance(msg, AIMessage):
            continue
        for call in getattr(msg, "tool_calls", None) or []:
            if call.get("name") == FINAL_RESPONSE_TOOL:
                return call.get("args") or {}

    structured = values.get("structured_response")
    if structured is None:
        return None
    if isinstance(structured, dict):
        return structured

    # The graph is built with ``response_format``, so LangGraph populates this
    # channel with a validated ``FinalResponseSchema`` *instance*, not a dict —
    # which is why ``StreamHandler`` reads it via ``getattr``. Prefer the
    # pydantic API over ``__dict__``: the latter happens to hold field values in
    # pydantic v2, but it is an implementation detail that skips serialization
    # (aliases, computed fields) and would silently return the wrong shape.
    dump = getattr(structured, "model_dump", None)
    if callable(dump):
        return dump()
    return dict(getattr(structured, "__dict__", {})) or None


def final_text(values: Any) -> str:
    """The user-visible answer for this turn.

    Prefers the ``FinalResponseSchema`` ``message`` field, which is the only
    channel the model is supposed to speak through, and falls back to the last
    AIMessage text for paths that bypass structured output.
    """
    structured = final_response(values)
    if structured and structured.get("message"):
        return str(structured["message"])

    for msg in reversed(_messages(values)):
        if isinstance(msg, AIMessage):
            text = message_text(msg)
            if text.strip():
                return text
    return ""


def task_state(values: Any) -> str | None:
    """A2A task state the model declared (``completed``/``working``/``input_required``/``failed``)."""
    structured = final_response(values)
    return structured.get("task_state") if structured else None


def _ordered_unique(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
