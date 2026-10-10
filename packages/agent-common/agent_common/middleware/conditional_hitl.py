"""Conditional Human-in-the-Loop middleware.

Extends LangChain's HumanInTheLoopMiddleware to support:
1. Argument-based conditions (static guards via ``interrupt_on`` dict)
2. Dynamic risk scoring (LLM-based scoring via ``risk_scorer`` callable)

The two modes compose: static guards (interrupt_on) always fire regardless of score.
Dynamic scoring evaluates all OTHER tool calls against a risk threshold.

Usage (static only — backward compatible):

    middleware = ConditionalHumanInTheLoopMiddleware(interrupt_on={
        "read_personal_file": {
            "allowed_decisions": ["approve", "reject"],
            "description": "Agent wants to read your personal file.",
        },
    })

Usage (dynamic scoring):

    from agent_common.core.tool_risk_scorer import score_tool_risk

    middleware = ConditionalHumanInTheLoopMiddleware(
        interrupt_on={},  # Static guards (or omit for DB-driven)
        risk_scorer=score_tool_risk,
        default_risk_threshold=0.8,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple, TypedDict

from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    HumanInTheLoopMiddleware,
    ReviewConfig,
    ToolMessage,
)
from langchain.agents.middleware.types import AgentState, ContextT, ResponseT, StateT, hook_config
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolCall
from langchain_core.tools import BaseTool
from langchain_quickjs._prompt import to_camel_case
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from agent_common.core.client_action_tool import (
    CLIENT_ACTION_TOOL_NAME,
    client_action_artifact,
    render_client_action_result,
    wire_args,
)
from agent_common.core.hitl_resume import REFUSAL_LEADS, decisions_from_resume, decisions_from_resume_sync
from agent_common.core.stream_watchdog import await_with_keepalive
from agent_common.core.tool_risk_cache import ToolRiskCache, ToolRiskEntry
from agent_common.core.turn_stops import REFUSED_AGAIN_LEAD
from agent_common.middleware.ptc_guard import PTC_CODE_INTERPRETER_TOOL_NAME
from agent_common.middleware.tool_status import RISK_ASSESSMENT_STATUS_TOOL, emit_tool_status

logger = logging.getLogger(__name__)

# Type alias for the risk scorer callable.
# Signature: (tool_name, args, *, tool, cache, server_slug) -> (score, entry | None)
RiskScorerFn = Callable[
    ...,
    Awaitable[tuple[float, ToolRiskEntry | None]],
]

# Type alias for a condition function that gates static interrupts on args.
ConditionFn = Callable[[dict[str, Any]], bool]


class BypassRule(TypedDict, total=False):
    """Structure of a single tool bypass rule in runtime context."""

    bypass_all: bool
    bypass_patterns: dict[str, list[str]]


class _RiskMetadata(TypedDict, total=False):
    """Internal metadata attached to each risk-triggered interrupt."""

    source: str
    score: float
    threshold: float
    matched_pattern: str | None
    server_slug: str
    allowed_actions: list[str]


def _client_action_tool_message(decision: dict[str, Any], tool_call: ToolCall) -> ToolMessage | None:
    """The ``client_action`` shortcut: the browser ALREADY did it.

    A risk-gated ``client_action`` used to cost two pauses. The gate asked, the
    graph resumed, the tool ran and then interrupted a SECOND time to ask the
    browser for its result. Both pauses are a full A2A resume, and the first one
    replays this node — so the user waited through it twice for one form write.

    The embed SDK now executes the directive when the user approves the card and
    sends the outcome back on the decision itself (``client_action_result``,
    see ``approval-codec.ts``). There is nothing left for the tool to do: we hand
    the model the same prose the tool would have returned and let the agent loop
    skip the call (a tool call that already has a ToolMessage is never dispatched
    — ``langchain.agents.factory``, ``pending_tool_calls``).

    Returns None whenever the shortcut does not apply — a reject, a different
    tool, or an SDK that sent no result. The tool then runs and interrupts for
    the result exactly as before, so an older client keeps working.
    """
    if tool_call["name"] != CLIENT_ACTION_TOOL_NAME:
        return None
    if decision.get("type") != "approve":
        return None
    result = decision.get("client_action_result")
    if not isinstance(result, dict):
        return None

    kind = str((tool_call.get("args") or {}).get("kind") or "")
    return ToolMessage(
        content=render_client_action_result(kind, result),
        artifact=client_action_artifact(result),
        name=tool_call["name"],
        tool_call_id=tool_call["id"],
        status="success" if result.get("ok") else "error",
    )


def _answered(last_ai_msg: AIMessage, tool_messages: list[ToolMessage], *, end: bool = False) -> dict[str, Any]:
    """The state update for tool calls this middleware answered itself.

    When every call of the turn has its ToolMessage here (an approved client_action
    the browser already ran, a rejection, a corrective answer), nothing is left for
    the tools node, and langchain's model→tools edge falls through to "a structured
    response exists → end" — and on any turn after the first, the checkpoint still
    holds the PREVIOUS turn's structured response. The run then ended without the
    model ever reading the answers, and the stream replayed the stale reply. Jumping
    back to the model explicitly is what the edge would do absent that stale state.

    ``end`` ends the turn instead: the reply then says the call was not done
    (``unanswered_turn_reply``), never the stale answer.
    """
    update: dict[str, Any] = {"messages": [last_ai_msg, *tool_messages]}
    answered = {m.tool_call_id for m in tool_messages}
    if tool_messages and all(call["id"] in answered for call in last_ai_msg.tool_calls):
        update["jump_to"] = "end" if end else "model"
    return update


#: What a ``client_action`` save scores (tool_risk_scorer): an action that saves is shown alike.
_SAVE_SCORE = 0.9


def _invoke_requires_approval(args: dict[str, Any], context: Any, messages: list[BaseMessage] | None = None) -> bool:
    """Whether a ``client_action`` invoke targets an action the host marked ``requiresApproval``.

    An action never saves by default, so ``invoke`` runs without a card. A host marks the
    ones that do save (a "set as default" button, "run now") and they get the save's card.
    Read from the object list the page sent with the turn — the orchestrator's runtime
    context or, for an embedded sub-agent, the run's config metadata — and from the page a
    navigate/invoke of this turn landed on (its ToolMessage artifact): an action on a page
    opened mid-turn is in no turn-start list. Marked in either is marked.
    """
    if args.get("kind") != "invoke":
        return False
    from agent_common.middleware.client_objects_middleware import _client_objects_from_config

    turn_start = getattr(context, "client_objects", None) or _client_objects_from_config() or []
    sources = [turn_start, _landed_objects(messages or [])]
    return any(_marked(objects, args) for objects in sources)


def _landed_objects(messages: list[BaseMessage]) -> list[Any]:
    """The objects of the newest page a ``client_action`` of this user turn landed on."""
    for message in reversed(messages):
        if _starts_user_turn(message):
            break
        if isinstance(message, ToolMessage) and message.name == CLIENT_ACTION_TOOL_NAME:
            artifact = message.artifact
            if isinstance(artifact, dict) and isinstance(artifact.get("objects"), list):
                return artifact["objects"]
    return []


def _starts_user_turn(message: BaseMessage) -> bool:
    """A user's message — not a steering message injected into the running turn."""
    return isinstance(message, HumanMessage) and not message.additional_kwargs.get("steering")


def _marked(objects: Any, args: dict[str, Any]) -> bool:
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict) or obj.get("type") != args.get("target_type"):
            continue
        if str(obj.get("id")) != str(args.get("target_id")):
            continue
        for action in obj.get("actions") or []:
            if isinstance(action, dict) and action.get("name") == args.get("action"):
                return action.get("requiresApproval") is True
    return False


_REFUSED_AGAIN = (
    f"{REFUSED_AGAIN_LEAD} a moment ago, so it was not put to them again. "
    "Do not send it again. Tell the user plainly that it was not done, and ask what they want instead."
)


def _call_signature(tool_call: ToolCall) -> str:
    """Name plus arguments, without the ``_``-prefixed bookkeeping ones (``_call_id``)."""
    args = {k: v for k, v in (tool_call.get("args") or {}).items() if not k.startswith("_")}
    return f"{tool_call['name']}:{json.dumps(args, sort_keys=True, default=str)}"


def _refused_this_turn(messages: list[BaseMessage]) -> set[str]:
    """The calls the user refused since their last message, by :func:`_call_signature`.

    Told plainly not to retry, the agent re-sent the identical save the user had just
    clicked Reject on, so a second card asked the same question again at once. Within
    one user turn an identical call is that retry; a new message from the user (who
    may well ask for it after all) starts over.
    """
    # Any user message counts here, a steering one too: "actually yes, save it" sent
    # mid-run after a Reject is the user asking again, not the agent retrying.
    start = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), -1)
    turn = messages[start + 1 :]
    calls = {call["id"]: call for m in turn if isinstance(m, AIMessage) for call in m.tool_calls}
    return {
        _call_signature(calls[m.tool_call_id])
        for m in turn
        if isinstance(m, ToolMessage)
        and m.status == "error"
        and m.tool_call_id in calls
        and isinstance(m.content, str)
        and m.content.startswith(REFUSAL_LEADS)
    }


def _refused_again_this_turn(messages: list[BaseMessage]) -> set[str]:
    """The calls already answered :data:`_REFUSED_AGAIN` since the user's last message.

    A model that re-sends one of those a second time will not stop: answered and sent
    back to the model, it looped until the turn's step budget, unseen by loop detection
    (its ``after_model`` runs after this one's jump on the orchestrator).
    """
    start = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), -1)
    turn = messages[start + 1 :]
    calls = {call["id"]: call for m in turn if isinstance(m, AIMessage) for call in m.tool_calls}
    return {
        _call_signature(calls[m.tool_call_id])
        for m in turn
        if isinstance(m, ToolMessage)
        and m.tool_call_id in calls
        and isinstance(m.content, str)
        and m.content.startswith(REFUSED_AGAIN_LEAD)
    }


def _ptc_exposed_tool_names(state: Any) -> set[str] | None:
    """The exact tool names the PTC bridge last exposed inside ``eval``, or None.

    Written to the checkpoint by the code-interpreter middleware
    (``PTC_EXPOSED_TOOL_NAMES_STATE_KEY``) so an interrupt resume can re-expose the
    same set. Read here to keep the corrective hint honest — see
    _reachable_inside_eval. Absent on the first turn and whenever PTC is off, hence
    the None fallback rather than an empty set (which would mean "nothing exposed").
    """
    from agent_common.core.graph_utils import PTC_EXPOSED_TOOL_NAMES_STATE_KEY

    try:
        names = state.get(PTC_EXPOSED_TOOL_NAMES_STATE_KEY)
    except AttributeError:
        return None
    if not names:
        return None
    return {name for name in names if isinstance(name, str)}


def _reachable_inside_eval(snake_name: str, exposed_names: set[str] | None) -> bool:
    """Whether ``snake_name`` is actually callable as ``tools.<camel>`` inside ``eval``.

    Being a known tool is not enough. ``_PTC_EXCLUDED_TOOL_NAMES`` (``task``,
    ``write_todos``, the response schemas, ``client_action``, ``notify_user``) are
    deliberately kept natively bound because they *always fail* inside the sandbox,
    and the raw MCP-catalogue listers are stripped from the namespace by
    ``_without_raw_listers``. Sending the model into ``eval`` for one of those costs
    a round-trip and bounces straight back out through ``_not_a_function_hint``.

    *exposed_names* is the exact set the PTC bridge last exposed, read from the
    checkpointed state; when it is unavailable we fall back to excluding only the
    names that can never be in there.
    """
    from agent_common.core.graph_utils import _PTC_EXCLUDED_TOOL_NAMES, _RAW_LISTING_TOOL_NAMES

    if snake_name in _PTC_EXCLUDED_TOOL_NAMES or snake_name in _RAW_LISTING_TOOL_NAMES:
        return False
    if exposed_names is None:
        return True
    return snake_name in exposed_names


def _unresolvable_tool_message(
    tool_name: str,
    tool_call: ToolCall,
    known_names: set[str],
    exposed_names: set[str] | None = None,
    known_names_are_exhaustive: bool = False,
) -> ToolMessage | None:
    """Answer a native call that used a tool's *PTC* identifier, instead of scoring it.

    Under PTC the ``eval`` prompt advertises tools as camelCase members
    (``tools.consoleCreateBugReport``). Models regularly lift one of those
    identifiers out of the prompt and emit it as a **native** tool call. Nothing
    resolves it: the registry is keyed on the snake_case name, so the risk gate
    cannot fetch the tool and the dispatcher cannot execute it.

    Left alone, the call used to be classified from its name alone (see
    ``score_tool_risk``) and then answered with ``Tool '…' is not available`` — a
    dead end the model relayed to the user as "unavailable in this session",
    against the standing instruction never to report a tool as missing. Here we
    answer the call ourselves with the defect and the fix, the way
    ``graph_utils._not_a_function_hint`` does for the inside-``eval`` direction.

    The caller KEEPS the answered tool call in the AIMessage. A call that already
    has a ToolMessage is never dispatched (``langchain.agents.factory``,
    ``pending_tool_calls``), so keeping it costs nothing — while *stripping* it
    would leave a ``tool_result`` with no matching ``tool_use`` in the checkpointed
    history, which Bedrock/Anthropic hard-400 on for every later turn (the #184
    postmortem), and on an all-alias turn would route the graph to END with the
    hint unread. Upstream's own reject path keeps the call for the same reason.

    Without *known_names_are_exhaustive*, returns None unless the name is
    *positively* identifiable as a camelCase alias of a known tool. Absence alone
    proves nothing then: tools registered directly with ``ToolNode`` are invisible
    to this middleware, and refusing those would break callable tools. When the
    caller has registered every dispatchable tool (see
    ``deep_agent_builtin_tools``), absence *is* proof and any unresolvable name is
    answered — sparing the user an approval card for a call that cannot run, and
    the summary LLM the cost of describing it.
    """
    if tool_name in known_names:
        return None

    snake = next((n for n in known_names if to_camel_case(n) == tool_name), None)
    if snake is None:
        if not known_names_are_exhaustive:
            return None
        return ToolMessage(
            content=(
                f"`{tool_name}` is not a tool that exists here, so nothing ran and nothing was "
                "approved. The name is wrong, not the capability: check the tools you were given "
                "for the one you meant, or delegate the work with `task`."
            ),
            name=tool_name,
            tool_call_id=tool_call["id"],
            status="error",
        )

    # Imported lazily: ``graph_utils`` reaches back into this module (it builds the
    # middleware), so a module-level import would close the cycle.
    from agent_common.core.graph_utils import code_interpreter_ptc_enabled

    if code_interpreter_ptc_enabled() and _reachable_inside_eval(snake, exposed_names):
        content = (
            f"`{tool_name}` is not a tool you can call directly — it is the identifier of "
            f"`{snake}` *inside* the `eval` code interpreter, where tool names are camelCase. "
            f"Call it there instead:\n\n"
            f"    const result = await tools.{tool_name}({{ ... }});\n    result\n\n"
            "Nothing ran and nothing was approved; re-issue the call inside `eval`."
        )
    else:
        # Either PTC is off, or the tool is one of those deliberately kept OUT of the
        # sandbox — in both cases it is natively bound under its snake_case name.
        content = (
            f"`{tool_name}` is not a tool name — that is the camelCase form used inside the "
            f"`eval` code interpreter. This tool is called `{snake}` and is a regular tool "
            "call: re-issue it under that name. Nothing ran."
        )

    return ToolMessage(
        content=content,
        name=tool_name,
        tool_call_id=tool_call["id"],
        status="error",
    )


class _PendingScore(NamedTuple):
    """A tool call that reached dynamic risk scoring, with what deciding it needs."""

    idx: int
    tool_call: ToolCall
    tool_instance: BaseTool | None
    cache: ToolRiskCache | None
    server_slug: str
    requires_click: bool


class ConditionalHumanInTheLoopMiddleware(HumanInTheLoopMiddleware[StateT, ContextT, ResponseT]):
    """HumanInTheLoopMiddleware with conditional guarding and dynamic risk scoring.

    Supports two complementary guard modes:

    1. **Static guards** (``interrupt_on`` dict): Tools listed here ALWAYS trigger
       an interrupt (optionally gated by a ``condition`` callable on args).

    2. **Dynamic risk scoring** (``risk_scorer`` callable): All other tool calls
       are scored asynchronously. If score >= threshold, an interrupt fires.

    The ``aafter_model`` method is async-native and handles both modes.
    The sync ``after_model`` only handles static guards (no scoring).
    """

    def __init__(
        self,
        interrupt_on: dict[str, bool | dict[str, Any]] | None = None,
        *,
        description_prefix: str = "Tool execution requires approval",
        risk_scorer: RiskScorerFn | None = None,
        default_risk_threshold: float = 0.8,
        tool_risk_cache: ToolRiskCache | None = None,
        tool_server_map: dict[str, str] | None = None,
        platform_tools: dict[str, BaseTool] | None = None,
        platform_tools_are_exhaustive: bool = False,
    ) -> None:
        interrupt_on = interrupt_on or {}

        # Store conditions separately before calling super().__init__
        # because InterruptOnConfig doesn't know about 'condition'.
        self._conditions: dict[str, ConditionFn] = {}
        for tool_name, tool_config in interrupt_on.items():
            if isinstance(tool_config, dict) and "condition" in tool_config:
                self._conditions[tool_name] = tool_config["condition"]

        # Dynamic risk scoring
        self._risk_scorer = risk_scorer
        self._default_risk_threshold = default_risk_threshold
        # Fallback cache for graphs that don't pass context (e.g. sub-agents)
        self._tool_risk_cache: ToolRiskCache | None = tool_risk_cache
        # Fallback server map for sub-agents that don't pass context
        self._tool_server_map: dict[str, str] | None = tool_server_map
        # Platform tools (e.g. filesystem tools from FilesystemMiddleware) that
        # aren't in the runtime tool_registry but need schema for risk scoring
        self._platform_tools: dict[str, BaseTool] = platform_tools or {}
        # True only when the caller has registered EVERY tool the graph can dispatch
        # that isn't in the runtime registry — the deep-agent builtins included. It
        # licenses the stronger verdict in _unresolvable_tool_message: with a partial
        # set, "I can't find it" means nothing; with an exhaustive one it means the
        # call cannot resolve, and the model is better served by being told so now
        # than by an approval card for a tool that will fail to dispatch.
        self._platform_tools_are_exhaustive = platform_tools_are_exhaustive

        # Pass through to parent (it safely ignores unknown keys in the dict)
        super().__init__(interrupt_on=interrupt_on, description_prefix=description_prefix)

    def _should_interrupt(self, tool_call: ToolCall) -> bool:
        """Check whether a tool call should be statically interrupted.

        Returns True if:
        - The tool is in interrupt_on AND
        - Either no condition is defined, OR the condition returns True for the args.
        """
        tool_name: str = tool_call["name"]
        if tool_name not in self.interrupt_on:
            return False

        condition: ConditionFn | None = self._conditions.get(tool_name)
        if condition is None:
            return True

        return bool(condition(tool_call.get("args", {})))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: AgentState[Any], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Sync handler: only processes static interrupt_on guards.

        Does NOT invoke risk scoring (which requires async). If you need
        dynamic risk scoring, ensure your graph uses the async execution path
        which calls ``aafter_model``.
        """
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        # Create action requests and review configs for tools that need approval
        action_requests: list[ActionRequest] = []
        review_configs: list[ReviewConfig] = []
        interrupt_indices: list[int] = []

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if self._should_interrupt(tool_call):
                config = self.interrupt_on[tool_call["name"]]
                action_request, review_config = self._create_action_and_config(tool_call, config, state, runtime)
                # Stamp the per-call id (see aafter_model) so multi-action interrupts
                # raised on the sync path align decisions by id too.
                action_request["args"] = {**action_request.get("args", {}), "_call_id": tool_call["id"]}
                action_requests.append(action_request)
                review_configs.append(review_config)
                interrupt_indices.append(idx)

        # If no interrupts needed, return early
        if not action_requests:
            return None

        # Create single HITLRequest with all actions and configs
        hitl_request = HITLRequest(
            action_requests=action_requests,
            review_configs=review_configs,
        )

        # Send interrupt and get response
        # Shape-tolerant read — see agent_common.core.hitl_resume: a resume value
        # written for another interrupt (or typed as words) must reject the call,
        # never crash the agent with KeyError('decisions').
        decisions = decisions_from_resume_sync(interrupt(hitl_request), hitl_request["action_requests"])

        # Validate that the number of decisions matches the number of interrupt tool calls
        if (decisions_len := len(decisions)) != (interrupt_count := len(interrupt_indices)):
            msg = (
                f"Number of human decisions ({decisions_len}) does not match "
                f"number of hanging tool calls ({interrupt_count})."
            )
            raise ValueError(msg)

        # Process decisions and rebuild tool calls in original order
        revised_tool_calls: list[ToolCall] = []
        artificial_tool_messages: list[ToolMessage] = []
        decision_idx = 0

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in interrupt_indices:
                # This was an interrupt tool call - process the decision
                config = self.interrupt_on[tool_call["name"]]
                decision = decisions[decision_idx]
                decision_idx += 1

                revised_tool_call, tool_message = self._process_decision(decision, tool_call, config)
                # An approved client_action the browser already executed answers
                # itself — no second pause. See _client_action_tool_message.
                if tool_message is None:
                    tool_message = _client_action_tool_message(decision, tool_call)
                if revised_tool_call is not None:
                    revised_tool_calls.append(revised_tool_call)
                if tool_message:
                    artificial_tool_messages.append(tool_message)
            else:
                # This was auto-approved - keep original
                revised_tool_calls.append(tool_call)

        # Update the AI message to only include approved tool calls
        last_ai_msg.tool_calls = revised_tool_calls

        return _answered(last_ai_msg, artificial_tool_messages)

    async def _score_concurrently(
        self, pending: list[_PendingScore]
    ) -> list[tuple[float, ToolRiskEntry | None] | BaseException]:
        """Score every pending call at once; a failed score comes back as its exception.

        A cache miss is an LLM classification on the default chat tier with reasoning on (see
        tool_risk_scorer) — tens of seconds per new tool. Nothing reaches the graph stream
        while this hook awaits, so the orchestrator's inter-chunk watchdog would read the wait
        as a hung stream and cancel the turn: the wait is kept alive, and when it is long
        enough to notice, the activity log says what the turn is waiting on.

        One call per (tool, server) is scored first, the rest after it. They are then cache
        hits, as they were when scoring was sequential: N calls of one new tool pay one
        classification, and every call of it is judged on the same profile — independent
        classifications could fall on both sides of the threshold, and the resume, which
        re-scores against the one cached profile, would then pair decisions with the wrong calls.
        """
        scorer = self._risk_scorer
        assert scorer is not None  # only called with calls that reached step 3
        names = ", ".join(dict.fromkeys(p.tool_call["name"] for p in pending))

        async def _say_why() -> None:
            await emit_tool_status(f"Assessing the risk of {names}…", RISK_ASSESSMENT_STATUS_TOOL)

        def score(p: _PendingScore) -> Awaitable[tuple[float, ToolRiskEntry | None]]:
            return scorer(
                p.tool_call["name"],
                p.tool_call.get("args", {}),
                tool=p.tool_instance,
                cache=p.cache,
                server_slug=p.server_slug,
            )

        first_of: dict[tuple[str, str], int] = {}
        for i, p in enumerate(pending):
            first_of.setdefault((p.tool_call["name"], p.server_slug), i)
        leaders = list(first_of.values())
        followers = [i for i in range(len(pending)) if i not in set(leaders)]

        async def scores() -> list[tuple[float, ToolRiskEntry | None] | BaseException]:
            outcomes: list[Any] = [None] * len(pending)
            results = await asyncio.gather(*(score(pending[i]) for i in leaders), return_exceptions=True)
            for i, result in zip(leaders, results):
                outcomes[i] = result
            # A leader that got no profile (classification failed → the scorer's name-based
            # fallback, nothing cached) answers for its followers: they would only classify
            # again, in parallel, and could disagree with it. The fallback ignores args.
            redo = []
            for i in followers:
                leader = outcomes[first_of[(pending[i].tool_call["name"], pending[i].server_slug)]]
                if isinstance(leader, BaseException) or leader[1] is None:
                    outcomes[i] = leader
                else:
                    redo.append(i)
            results = await asyncio.gather(*(score(pending[i]) for i in redo), return_exceptions=True)
            for i, result in zip(redo, results):
                outcomes[i] = result
            return outcomes

        return await await_with_keepalive(scores(), source="tool-risk-scoring", on_slow=_say_why)

    @hook_config(can_jump_to=["model", "end"])
    async def aafter_model(self, state: AgentState[Any], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Async handler: combines static guards + dynamic risk scoring.

        Flow for each tool call:
        1. If tool_name == "task" -> auto-approve (sub-agent owns its own HITL)
        1b. If the name resolves to no dispatchable tool -> answer the call with a
            corrective ToolMessage (never scored, never dispatched; the call itself
            stays put). See _unresolvable_tool_message.
        2. If tool is in static interrupt_on -> use static guard (same as sync)
        3. If risk_scorer is configured -> score the tool call:
           a. Check bypass rules from runtime context
           b. Score every such call of the step concurrently (cache lookup or LLM call),
              keeping the graph's idle watchdog fed while they run — see _score_concurrently
           c. Compare against threshold
           d. If score >= threshold -> interrupt with allowed_actions from entry
        4. Otherwise -> auto-approve
        """
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        action_requests: list[ActionRequest] = []
        review_configs: list[ReviewConfig] = []
        interrupt_indices: list[int] = []
        # Store risk metadata per interrupt for inclusion in the payload
        _risk_metadata: list[_RiskMetadata] = []
        # Calls answered here instead of being scored or dispatched, by index:
        # a camelCase PTC identifier emitted as a native call, or (when the platform
        # set is exhaustive) any name that resolves to nothing. See
        # _unresolvable_tool_message.
        corrective_messages: dict[int, ToolMessage] = {}
        # Interrupts by tool-call index. Risk-scored calls are decided after the loop (they are
        # scored concurrently), so the request lists are built from this, in index order — the
        # decision loop below pairs decisions with interrupt_indices positionally.
        interrupts: dict[int, tuple[ActionRequest, ReviewConfig, _RiskMetadata]] = {}
        # Calls that reached step 3, scored together after the loop.
        to_score: list[_PendingScore] = []
        refused = _refused_this_turn(messages)
        refused_again = _refused_again_this_turn(messages) if refused else set()
        # A refused call sent a third time ends the turn (see _refused_again_this_turn).
        end_turn = False
        # The step's first ``client_action`` invoke, if any — see 1c below.
        step_context: Any = getattr(runtime, "context", None)
        # The step's first invoke that changes the screen (``edit``, a dialog) — not one
        # that saves: a save is refused when it shares a step (1c), and must not win over
        # the fill next to it, or it saves the form as it was before that fill.
        step_invoke = next(
            (
                tc
                for tc in last_ai_msg.tool_calls
                if tc["name"] == CLIENT_ACTION_TOOL_NAME
                and (tc.get("args") or {}).get("kind") == "invoke"
                and not _invoke_requires_approval(tc.get("args") or {}, step_context, messages)
            ),
            None,
        )

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            tool_name: str = tool_call["name"]
            args: dict[str, Any] = tool_call.get("args", {})

            # 1. Sub-agent dispatch and the PTC code interpreter are never
            #    interrupted here. ``task`` is a dispatch primitive; ``eval`` (the
            #    code interpreter) carries its risk guard on the inner wrapped
            #    tool calls (which return an approval-required payload instead of
            #    executing). Interrupting ``eval`` would trigger a graph
            #    interrupt/resume cycle the PTC bridge is designed to avoid.
            if tool_name in ("task", PTC_CODE_INTERPRETER_TOOL_NAME):
                continue

            # 1b. A name nothing can resolve is not scored — there is no schema to
            #     reason about, so a classification would be derived from the name
            #     alone and cached as if it were a real profile. Answer the call
            #     with the fix instead of letting it fall through to the
            #     dispatcher's "Tool ... is not available".
            #     Gated on the instance lookup (two dict hits) so the name-set build
            #     and the camel scan stay off the path every resolvable call takes.
            corrective = (
                _unresolvable_tool_message(
                    tool_name,
                    tool_call,
                    self._known_tool_names(getattr(runtime, "context", None)),
                    _ptc_exposed_tool_names(state),
                    self._platform_tools_are_exhaustive,
                )
                if self._get_tool_instance(tool_name, getattr(runtime, "context", None)) is None
                else None
            )
            if corrective is not None:
                logger.info(
                    "Tool call '%s' does not resolve to any dispatchable tool; answering with a "
                    "hint instead of scoring or dispatching it",
                    tool_name,
                )
                corrective_messages[idx] = corrective
                continue

            # 1b'. The user refused this very call earlier in the turn: answer the
            #      retry instead of putting the same question to them again.
            if refused and _call_signature(tool_call) in refused:
                logger.info("Tool call '%s' repeats a call the user refused this turn; not asking again", tool_name)
                end_turn = end_turn or _call_signature(tool_call) in refused_again
                corrective_messages[idx] = ToolMessage(
                    content=_REFUSED_AGAIN, name=tool_name, tool_call_id=tool_call["id"], status="error"
                )
                continue

            # 1c. A saving ``client_action`` (an invoke the host marked requiresApproval,
            #     e.g. a form's ``save``) runs in the browser the moment the user
            #     approves it — before any sibling call of the same step. Sent next
            #     to the ``apply`` that fills the form, it saved the form as it was
            #     and the fill landed afterwards, unsaved, while the agent was told
            #     both worked. A save must see the results of everything before it,
            #     so it only runs alone; answered here, it never raises a card.
            #     An ``invoke`` changes what is on screen (``edit`` mounts the form the
            #     ``apply`` then fills), so every OTHER ``client_action`` of its step
            #     could reach the browser before the screen it was written for exists.
            #     The invoke runs; the others are answered and resent next step.
            if step_invoke is not None and tool_name == CLIENT_ACTION_TOOL_NAME and tool_call is not step_invoke:
                kind = args.get("kind")
                action = (step_invoke.get("args") or {}).get("action")
                corrective_messages[idx] = ToolMessage(
                    content=(
                        f"NOT RUN: {kind} was sent together with invoke '{action}'; the invoke changes "
                        f"what is on screen — send it alone, read its result, then {kind} in the next step"
                    ),
                    name=tool_name,
                    tool_call_id=tool_call["id"],
                    status="error",
                )
                continue
            if (
                tool_name == CLIENT_ACTION_TOOL_NAME
                and len(last_ai_msg.tool_calls) > 1
                and _invoke_requires_approval(args, step_context, messages)
            ):
                action = args.get("action")
                corrective_messages[idx] = ToolMessage(
                    content=(
                        f"NOT RUN: '{action}' saves, so it must be invoked on its own, after the results of "
                        "your other calls are back — nothing was saved. Check those results, then invoke "
                        f"'{action}' again, alone."
                    ),
                    name=tool_name,
                    tool_call_id=tool_call["id"],
                    status="error",
                )
                continue

            # 2. Static guards take priority
            if self._should_interrupt(tool_call):
                config = self.interrupt_on[tool_name]
                action_request, review_config = self._create_action_and_config(tool_call, config, state, runtime)
                # Stamp the stable per-call id on EVERY interrupted call (here a static
                # guard, no risk metadata) so the client can return one decision per
                # action_request and the resume path aligns them by id. Display-only:
                # ``args`` is never passed to the tool (approve replays the original call).
                action_request["args"] = {**action_request.get("args", {}), "_call_id": tool_call["id"]}
                interrupts[idx] = (action_request, review_config, {"source": "static_guard"})
                continue

            # 3. Dynamic risk scoring
            if self._risk_scorer is None:
                continue

            # Check bypass rules from runtime context
            context: Any = getattr(runtime, "context", None)
            requires_click = tool_name == CLIENT_ACTION_TOOL_NAME and _invoke_requires_approval(args, context, messages)
            bypass_rules: dict[str, BypassRule] | None = (
                getattr(context, "tool_bypass_rules", None) if context else None
            )
            server_slug: str = self._get_server_slug(tool_name, context)

            # A standing bypass never covers a click-only action: the browser refuses it
            # without the click, so skipping the card would only make it fail every time.
            if not requires_click and bypass_rules and self._is_bypassed(tool_name, server_slug, args, bypass_rules):
                continue

            # Get tool instance and cache from context
            tool_instance: BaseTool | None = self._get_tool_instance(tool_name, context)
            cache: ToolRiskCache | None = (
                getattr(context, "tool_risk_cache", None) if context else None
            ) or self._tool_risk_cache

            to_score.append(_PendingScore(idx, tool_call, tool_instance, cache, server_slug, requires_click))

        # 3b. Score the collected calls together — a step's new tools each cost an LLM
        #     classification, and awaiting them one by one put their SUM in front of the
        #     approval card — then decide each one in its own right.
        outcomes = await self._score_concurrently(to_score) if to_score else []
        context = getattr(runtime, "context", None)
        for pending, outcome in zip(to_score, outcomes):
            idx, tool_call, tool_instance = pending.idx, pending.tool_call, pending.tool_instance
            server_slug, requires_click = pending.server_slug, pending.requires_click
            tool_name = tool_call["name"]
            args = tool_call.get("args", {})
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                logger.error("Risk scoring failed for tool '%s', skipping guard", tool_name, exc_info=outcome)
                continue
            score: float
            entry: ToolRiskEntry | None
            score, entry = outcome

            # Compare against threshold
            threshold: float = self._get_threshold(context)
            # A page action the host marked requiresApproval saves something: it is asked
            # like a save, whatever ``invoke``'s base score.
            if requires_click:
                score = max(score, threshold, _SAVE_SCORE)
            if score < threshold:
                continue

            # Score exceeds threshold — interrupt
            allowed_actions: list[str] = entry.allowed_actions if entry else ["approve", "edit", "reject"]
            matched_pattern: str | None = entry.get_matched_pattern(args) if entry else None

            # Build action request for this tool call
            description = f"Tool '{tool_name}' has risk score {score:.2f} (threshold: {threshold:.2f})"
            if matched_pattern:
                description += f" — {matched_pattern}"

            # Include structured risk metadata in args for frontend rendering.
            # ``_call_id`` is a top-level, risk-independent per-call id (set for every
            # interrupted call — static or risk-scored) the client echoes so the resume
            # path aligns decisions by id (see executor._build_interrupt_resume_map).
            enriched_args: dict[str, Any] = {
                # A client_action's values/args travel as typed pairs; the card and the
                # browser (which builds the directive from these on Approve) read objects.
                **(wire_args(args) if tool_name == CLIENT_ACTION_TOOL_NAME else args),
                "_call_id": tool_call["id"],
                # Only the Approve click runs it: typed words never do (see hitl_resume).
                **({"_requires_click": True} if requires_click else {}),
                "_risk_metadata": {
                    "source": "risk_score",
                    "score": score,
                    "threshold": threshold,
                    "matched_pattern": matched_pattern,
                    "server_slug": server_slug,
                    "tool_name": tool_name,
                },
            }

            action_request = ActionRequest(
                name=tool_name,
                args=enriched_args,
                description=description,
            )
            review_config = ReviewConfig(
                action_name=tool_name,
                allowed_decisions=allowed_actions,
            )
            # Include args_schema if "edit" is allowed
            if "edit" in allowed_actions and tool_instance is not None:
                try:
                    review_config["args_schema"] = tool_instance.get_input_schema().model_json_schema()
                except Exception:
                    pass

            interrupts[idx] = (
                action_request,
                review_config,
                {
                    "source": "risk_score",
                    "score": score,
                    "threshold": threshold,
                    "matched_pattern": matched_pattern,
                    "server_slug": server_slug,
                    "allowed_actions": allowed_actions,
                },
            )

        for idx in sorted(interrupts):
            action_request, review_config, metadata = interrupts[idx]
            action_requests.append(action_request)
            review_configs.append(review_config)
            interrupt_indices.append(idx)
            _risk_metadata.append(metadata)

        # If no interrupts needed, return early — unless a call was answered above,
        # whose ToolMessage still has to reach the graph. The AIMessage is returned
        # unchanged: the answered call STAYS in ``tool_calls`` (see
        # _unresolvable_tool_message on why it must not be stripped).
        if not action_requests:
            if not corrective_messages:
                return None
            return _answered(last_ai_msg, list(corrective_messages.values()), end=end_turn)

        # Attach a plain-language summary to each action request so the client
        # can show non-technical users what the tool would do. Display-only
        # (rides in args like _risk_metadata) and best-effort: on failure the
        # client falls back to rendering the raw args.
        await self._attach_summaries(action_requests, runtime)

        # Create single HITLRequest with all actions and configs
        hitl_request = HITLRequest(
            action_requests=action_requests,
            review_configs=review_configs,
        )

        # Send interrupt and get response
        # Shape-tolerant read — see agent_common.core.hitl_resume.
        decisions = await decisions_from_resume(interrupt(hitl_request), hitl_request["action_requests"])

        # Validate decisions count
        if (decisions_len := len(decisions)) != (interrupt_count := len(interrupt_indices)):
            msg = (
                f"Number of human decisions ({decisions_len}) does not match "
                f"number of hanging tool calls ({interrupt_count})."
            )
            raise ValueError(msg)

        # Process decisions and rebuild tool calls in original order
        revised_tool_calls: list[ToolCall] = []
        artificial_tool_messages: list[ToolMessage] = []
        decision_idx = 0

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in corrective_messages:
                # Answered above. The call is KEPT (a call that already has a
                # ToolMessage is never dispatched) so the turn stays provider-legal
                # and the router loops back to the model with the hint.
                revised_tool_calls.append(tool_call)
                artificial_tool_messages.append(corrective_messages[idx])
            elif idx in interrupt_indices:
                tool_name = tool_call["name"]
                decision = decisions[decision_idx]
                metadata = _risk_metadata[decision_idx]
                decision_idx += 1

                # Determine config for _process_decision
                if tool_name in self.interrupt_on:
                    config = self.interrupt_on[tool_name]
                else:
                    # Dynamic guard — build a synthetic config
                    entry_actions = metadata.get("allowed_actions") or ["approve", "edit", "reject"]
                    config = {"allowed_decisions": entry_actions}

                revised_tool_call, tool_message = self._process_decision(decision, tool_call, config)
                # An approved client_action the browser already executed answers
                # itself — no second pause. See _client_action_tool_message.
                if tool_message is None:
                    tool_message = _client_action_tool_message(decision, tool_call)
                if revised_tool_call is not None:
                    revised_tool_calls.append(revised_tool_call)
                if tool_message:
                    artificial_tool_messages.append(tool_message)

                # Handle bypass-next-time: if user approved with bypass flag,
                # update in-memory bypass rules for this session
                if (
                    decision.get("type") == "approve"
                    and decision.get("bypass")
                    and metadata.get("source") == "risk_score"
                ):
                    self._apply_bypass_rule(
                        tool_name=tool_name,
                        server_slug=metadata.get("server_slug", "_self"),
                        bypass_all=bool(decision.get("bypass_all", False)),
                        bypass_pattern=decision.get("bypass_pattern"),
                        context=context,
                    )
            else:
                # Auto-approved (no interrupt)
                revised_tool_calls.append(tool_call)

        # Update the AI message
        last_ai_msg.tool_calls = revised_tool_calls

        return _answered(last_ai_msg, artificial_tool_messages)

    async def _attach_summaries(self, action_requests: list[ActionRequest], runtime: Runtime[ContextT]) -> None:
        """Stamp a plain-language ``_summary`` into each action request's args.

        Makes one batched fast-LLM call for the whole interrupt (in the user's
        language from runtime context). Best-effort: any failure leaves the
        action requests untouched.
        """
        from agent_common.core.tool_call_summarizer import attach_summaries

        context: Any = getattr(runtime, "context", None)
        await attach_summaries(
            action_requests,
            language=getattr(context, "language", None) or "en",
            describe=lambda name: getattr(self._get_tool_instance(name, context), "description", None) or "",
        )

    # ------------------------------------------------------------------
    # Helper methods for dynamic risk scoring
    # ------------------------------------------------------------------

    def _get_server_slug(self, tool_name: str, context: Any) -> str:
        """Resolve the MCP server slug for a tool.

        MCP tools resolve to their server name (e.g. 'console', 'github').
        In-process tools (read_personal_file, docstore_search) fall back to '_self'.

        Resolution order:
        1. tool_server_map on context (orchestrator pre-builds this)
        2. Middleware-level _tool_server_map (sub-agents inject at build time)
        3. tool.metadata["server_name"] on the tool instance (set by MCP discovery)
        4. Fallback to '_self' (in-process platform tools)
        """
        # Check tool_server_map on context (orchestrator path)
        if context is not None:
            tool_server_map: dict[str, str] | None = getattr(context, "tool_server_map", None)
            if tool_server_map and tool_name in tool_server_map:
                return tool_server_map[tool_name]

        # Check middleware-level fallback (sub-agent path)
        if self._tool_server_map and tool_name in self._tool_server_map:
            return self._tool_server_map[tool_name]

        # Fall back to tool metadata on context's tool_registry
        if context is not None:
            tool_registry: dict[str, Any] | None = getattr(context, "tool_registry", None)
            if tool_registry and tool_name in tool_registry:
                tool = tool_registry[tool_name]
                metadata = getattr(tool, "metadata", None)
                if metadata and isinstance(metadata, dict):
                    server_name = metadata.get("server_name")
                    if server_name:
                        return server_name

        # Default: platform tools
        return "_self"

    def _known_tool_names(self, context: Any) -> set[str]:
        """Every tool name this middleware can account for, for alias detection.

        The union of the per-user registry, the injected platform/static tools and
        both server maps. It is deliberately *not* treated as the set of callable
        tools: the dispatcher also resolves tools registered directly with
        ``ToolNode`` (``write_todos``, the filesystem tools, the response schemas),
        which never appear here. So this set answers "is this name a mangled form
        of something I know?", never "is this name callable?".
        """
        names: set[str] = set(self._platform_tools)
        if self._tool_server_map:
            names |= set(self._tool_server_map)
        if context is not None:
            for attr in ("tool_registry", "tool_server_map"):
                mapping = getattr(context, attr, None)
                if isinstance(mapping, dict):
                    names |= {name for name in mapping if isinstance(name, str)}
        return names

    def _get_tool_instance(self, tool_name: str, context: Any) -> BaseTool | None:
        """Get a BaseTool instance from the runtime context's tool registry or platform tools."""
        # Check runtime context's tool_registry first
        if context is not None:
            tool_registry: dict[str, BaseTool] | None = getattr(context, "tool_registry", None)
            if tool_registry and tool_name in tool_registry:
                return tool_registry[tool_name]

        # Fallback to platform tools (e.g. filesystem tools from FilesystemMiddleware)
        if tool_name in self._platform_tools:
            return self._platform_tools[tool_name]

        return None

    def _get_threshold(self, context: Any) -> float:
        """
        Get the risk threshold

        TODO: potentially role-adjusted from context.
        """
        if context is None:
            return self._default_risk_threshold

        # Allow per-request threshold override from context
        threshold: float | None = getattr(context, "risk_threshold", None)
        if threshold is not None:
            return float(threshold)

        return self._default_risk_threshold

    @staticmethod
    def _is_bypassed(
        tool_name: str,
        server_slug: str,
        args: dict[str, Any],
        bypass_rules: dict[str, BypassRule],
    ) -> bool:
        """Check if a tool call is bypassed by user rules.

        Bypass rules format:
        {
            "tool_name::server_slug": {"bypass_all": True},
            "other_tool::server": {"bypass_patterns": {"param": ["glob1", "glob2"]}}
        }
        """
        key: str = f"{tool_name}::{server_slug}"
        rule: BypassRule | None = bypass_rules.get(key)
        if rule is None:
            return False

        # bypass_all: skip entirely
        if rule.get("bypass_all"):
            return True

        # bypass_patterns: check if the specific pattern that would trigger
        # is in the bypass list
        bypass_patterns: dict[str, list[str]] = rule.get("bypass_patterns", {})
        if not bypass_patterns:
            return False

        # Check each param's arg value against the bypass patterns
        for param_name, patterns in bypass_patterns.items():
            arg_value = args.get(param_name)
            if arg_value is None:
                continue
            from agent_common.core.tool_risk_cache import _glob_to_regex

            arg_str = str(arg_value)
            for pattern in patterns:
                try:
                    if _glob_to_regex(pattern).match(arg_str):
                        return True
                except Exception:
                    continue

        return False

    @staticmethod
    def _apply_bypass_rule(
        tool_name: str,
        server_slug: str,
        bypass_all: bool,
        bypass_pattern: str | None,
        context: Any,
    ) -> None:
        """Apply a bypass rule to the in-memory context.

        Updates `context.tool_bypass_rules` so subsequent calls in this
        session are automatically bypassed. The orchestrator is responsible
        for persisting the rule to the backend API after the turn completes.
        """
        bypass_rules: dict[str, BypassRule] | None = getattr(context, "tool_bypass_rules", None)
        if bypass_rules is None:
            return

        key = f"{tool_name}::{server_slug}"
        existing: BypassRule = bypass_rules.get(key, {})  # type: ignore[assignment]

        if bypass_all:
            bypass_rules[key] = {"bypass_all": True, "bypass_patterns": {}}
        elif bypass_pattern:
            # Parse param and glob from bypass_pattern.
            # Supported formats:
            #   "param_name:glob_pattern" (legacy)
            #   "param_name matches `glob_pattern`" (from risk metadata)
            param: str | None = None
            glob: str | None = None
            if " matches `" in bypass_pattern and bypass_pattern.endswith("`"):
                param, rest = bypass_pattern.split(" matches `", 1)
                glob = rest[:-1]  # strip trailing backtick
            elif ":" in bypass_pattern:
                param, glob = bypass_pattern.split(":", 1)

            if param and glob:
                patterns = existing.get("bypass_patterns", {})
                param_patterns = patterns.get(param, [])
                if glob not in param_patterns:
                    param_patterns.append(glob)
                patterns[param] = param_patterns
                bypass_rules[key] = {
                    "bypass_all": existing.get("bypass_all", False),
                    "bypass_patterns": patterns,
                }

        # Store pending bypass for persistence by the orchestrator
        if key in bypass_rules:
            pending: list[dict[str, Any]] = getattr(context, "_pending_bypass_rules", [])
            pending.append({"key": key, "rule": bypass_rules[key]})
            if not hasattr(context, "_pending_bypass_rules"):
                context._pending_bypass_rules = pending
