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
    assert delegated_agents(turn_state.final_values) == ["slack-notifier"]

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

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.handlers import current_turn_messages

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

    status: str | None = None
    """ToolMessage status — ``"success"``, or ``"error"`` for a tool that raised
    *or* a call a human rejected. HITL rejection is the case that makes this
    worth having: the rejected call stays in the AIMessage (langchain's
    ``_process_decision`` returns the tool call, not None) and is answered with a
    synthetic error ToolMessage instead of being removed, so ``result is not
    None`` alone cannot tell "ran" from "refused"."""

    @property
    def completed(self) -> bool:
        return self.result is not None

    @property
    def rejected(self) -> bool:
        """Answered with an error rather than executed. See ``status``."""
        return self.status == "error"


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
    return list(messages) if all_turns else current_turn_messages(messages)


def message_text(message: Any) -> str:
    """A message's user-visible text — text blocks only, never reasoning.

    Non-messages return ``""`` rather than raising, as elsewhere here.
    """
    if isinstance(message, BaseMessage):
        return message.text
    return message if isinstance(message, str) else ""


def tool_calls(values: Any, *, all_turns: bool = False) -> list[ToolCall]:
    """Every orchestrator-visible tool call, in the order the model emitted them.

    Results are matched back by ``tool_call_id``; a call with no matching
    ToolMessage keeps ``result=None`` rather than being dropped, so "the model
    tried to call X but it never completed" stays assertable.
    """
    messages = _messages(values, all_turns=all_turns)

    results: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.tool_call_id:
            results[msg.tool_call_id] = message_text(msg)
            statuses[msg.tool_call_id] = getattr(msg, "status", None) or "success"

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
                    status=statuses.get(call_id) if call_id else None,
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


def interrupts(values: Any) -> list[Any]:
    """Pending interrupts on a turn that stopped for human review.

    ``ainvoke`` returns these on the ``__interrupt__`` key of the values dict, so
    the mock tier needs no second ``aget_state``. The real tier does not read
    this at all — ``agent.stream`` parks interrupts on ``TurnState.interrupts``
    (``app/core/agent.py:750``) and ``TurnState.has_interrupts`` is the property
    production checks.
    """
    if not isinstance(values, dict):
        return []
    pending = values.get("__interrupt__")
    return list(pending) if pending else []


def interrupt_action_requests(values: Any) -> list[dict[str, Any]]:
    """The tool calls a human is being asked to review, across all interrupts.

    ``ConditionalHumanInTheLoopMiddleware`` raises **one** interrupt carrying
    every guarded call in the batch, not one interrupt per call — so the length
    of this list is the number of decisions a resume must supply, and a mismatch
    raises ``ValueError`` rather than failing quietly.
    """
    requests: list[dict[str, Any]] = []
    for pending in interrupts(values):
        value = getattr(pending, "value", None)
        if isinstance(value, dict):
            requests.extend(r for r in (value.get("action_requests") or []) if isinstance(r, dict))
    return requests


def interrupted_tools(values: Any) -> list[str]:
    """Names of the tools awaiting human review, in the order presented."""
    return [str(request.get("name", "")) for request in interrupt_action_requests(values)]


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

    Normally the ``FinalResponseSchema`` ``message`` field, the only channel the
    model is supposed to speak through.

    The exception is ``include_subagent_output``: the schema instructs the model
    to leave ``message`` EMPTY and let the sub-agent's output be appended, so
    reading ``message`` alone reports an empty answer for a turn that actually
    answered the user at length. The executor does that append downstream of the
    graph, so it is reproduced here — otherwise any assertion about response
    content silently fails whenever the model chooses to pass a sub-agent's work
    through verbatim.
    """
    structured = final_response(values)
    if structured:
        message = str(structured.get("message") or "")
        if structured.get("include_subagent_output"):
            appended = last_subagent_output(values)
            return f"{message}\n\n{appended}".strip() if appended else message
        if message:
            return message

    for msg in reversed(_messages(values)):
        if isinstance(msg, AIMessage):
            text = message_text(msg)
            if text.strip():
                return text
    return ""


def last_subagent_output(values: Any, *, all_turns: bool = False) -> str:
    """Content returned by the most recent completed delegation, or ""."""
    for delegation in reversed(delegations(values, all_turns=all_turns)):
        if delegation.result:
            return delegation.result
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
