"""Runtime risk-guard wrappers for Programmatic Tool Calling (PTC).

``CodeInterpreterMiddleware``'s PTC bridge invokes tools directly, bypassing the
normal ``ToolNode`` path and therefore the ``HumanInTheLoop`` approval workflow.
To preserve per-call HITL guarantees we expose only *wrapped* tool instances to
PTC (via the middleware's ``ptc=[...]`` allowlist of explicit ``BaseTool``s).

Each wrapper re-runs the exact risk decision the
``ConditionalHumanInTheLoopMiddleware`` would apply for a normal tool call
(per-user bypass rules + ``score_tool_risk`` + threshold), reading the live
per-user context the PTC bridge injects into the wrapper at call time. When a
call *would* require approval the wrapper records it on a per-turn collector and
*returns* an approval-required payload instead of executing. The enclosing
``awrap_tool_call`` hook (in ``graph_utils``) drains the collector, fires a
single batched ``interrupt()`` for the turn, stores the human decisions, and
re-runs ``eval`` (which runs with ``mode="call"`` — a fresh REPL per call) so
the guard honors each decision: approved calls execute for real, rejected calls
return a rejection payload. Calls that score below threshold (or are whitelisted
by the user) execute normally inside ``eval``.

The guard deliberately *returns* rather than *raises*: the PTC bridge propagates
any Python exception raised inside ``eval`` straight out of the eval tool and
aborts the whole agent turn (``_aeval_async`` only catches a fixed set of
interpreter errors). Returning a payload keeps the turn alive so the wrapper can
interrupt cleanly from the main graph loop and re-run.

The wrapper forwards LangGraph's injected ``runtime`` / ``state`` / ``store`` to
the inner tool, mirroring ``ToolNode`` injection so wrapped filesystem tools keep
operating on the correct backend.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import ToolRuntime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from agent_common.core.tool_risk_cache import ToolRiskCache
    from agent_common.middleware.conditional_hitl import RiskScorerFn

logger = logging.getLogger(__name__)


# Name of the code-interpreter (PTC) tool exposed to the model. Must match
# ``langchain_quickjs``'s ``_DEFAULT_TOOL_NAME``. The ``eval`` tool itself must
# never be HITL-interrupted by the risk-based guard: its inner wrapped tool
# calls already carry the per-call risk decision (returning an approval-required
# payload instead of executing). Interrupting ``eval`` would force a graph
# interrupt/resume cycle that the PTC bridge is explicitly designed to avoid.
PTC_CODE_INTERPRETER_TOOL_NAME = "eval"


_PTC_APPROVAL_ERROR = "human_approval_required"

_PTC_APPROVAL_MESSAGE = (
    "Tool '{tool_name}' requires human approval before it can run. The call has "
    "been recorded for review; if the user approves, `eval` is re-run and the "
    "call executes automatically with its real result. Do not attempt to bypass "
    "this approval. Lower-risk calls may still run from inside `eval`."
)

_PTC_REJECTION_ERROR = "human_rejected"

_PTC_REJECTION_MESSAGE = (
    "Tool '{tool_name}' was not approved by the user and was not executed. Do "
    "not retry this exact call; consider an alternative approach or ask the user "
    "for guidance."
)


def approval_required_payload(tool_name: str) -> dict[str, str]:
    """Build the error payload returned when a PTC call needs HITL approval.

    Returned (not raised) from the guarded wrapper so it marshals into a JS
    object inside ``eval`` and surfaces in the eval result. When an
    interrupt-capable PTC turn is active (the common path), this payload is only
    observed by the *first* (probe) ``eval`` run, which is discarded: the call is
    recorded for HITL approval and, once approved, ``eval`` re-runs and the tool
    executes for real. When no PTC turn is active (no interrupt is possible),
    this payload is the final result and signals the model the call was blocked.
    """
    return {
        "error": _PTC_APPROVAL_ERROR,
        "tool": tool_name,
        "message": _PTC_APPROVAL_MESSAGE.format(tool_name=tool_name),
    }


def rejection_payload(tool_name: str) -> dict[str, str]:
    """Build the error payload returned when the user rejects a PTC call.

    Surfaced inside ``eval`` on the approved/rejected re-run when the user chose
    ``reject`` for this call. The tool is not executed.
    """
    return {
        "error": _PTC_REJECTION_ERROR,
        "tool": tool_name,
        "message": _PTC_REJECTION_MESSAGE.format(tool_name=tool_name),
    }


# ---------------------------------------------------------------------------
# Per-turn HITL collector for PTC (Programmatic Tool Calling) approvals.
#
# The risk guard runs *inside* the ``eval`` tool (dispatched by the PTC bridge
# on the outer event loop), where ``interrupt()`` cannot be raised cleanly. The
# enclosing ``awrap_tool_call`` hook (see graph_utils) runs in the main graph
# loop and *can* interrupt. They communicate through this module-level
# collector, keyed by ``thread_id``:
#
#   * the guard *records* high-risk calls (``pending``) and reads back human
#     ``decisions`` / cached ``results``;
#   * the wrapper drains ``pending``, fires a single ``interrupt()`` for the
#     batch, then writes the decisions and re-runs ``eval`` so the guard honors
#     them.
#
# A module dict (not a ContextVar) is required because the PTC bridge invokes
# the guard via ``asyncio.run_coroutine_threadsafe`` from its worker thread,
# which copies a *fresh* context — ContextVars set in ``awrap_tool_call`` would
# not propagate. Both sides run cooperatively on the same outer loop, and
# entries are isolated by ``thread_id``, so plain dict access is safe.
#
# IMPORTANT (double-execution caveat): ``interrupt()`` replays the tool node
# from the top, and this collector is NOT checkpointed (only the interrupt
# resume value is). Decisions therefore flow back in via the resume value and
# are re-applied on every replay. The ``results`` cache only dedups *within* a
# single process execution; low-risk side-effecting calls that *precede* a
# blocked call re-execute once per approval round-trip. This is acceptable and
# is why the PTC code interpreter runs with ``mode="call"`` (fresh REPL per
# ``eval``) so the re-run is deterministic and pod-independent.
# ---------------------------------------------------------------------------


@dataclass
class _PendingApproval:
    """A single high-risk PTC call awaiting human approval."""

    call_key: str
    tool_name: str
    args: dict[str, Any]
    server_slug: str
    allowed_actions: list[str]
    score: float
    threshold: float
    matched_pattern: str | None


@dataclass
class _PTCTurnState:
    """Mutable per-turn state shared between the guard and the wrapper."""

    pending: list[_PendingApproval] = field(default_factory=list)
    decisions: dict[str, str] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)

    def record_pending(self, item: _PendingApproval) -> None:
        if any(p.call_key == item.call_key for p in self.pending):
            return
        self.pending.append(item)


_PTC_TURNS: dict[str, _PTCTurnState] = {}

_PTC_DEFAULT_THREAD_ID = "_ptc_default"


def resolve_ptc_thread_id(runtime: Any) -> str:
    """Resolve the LangGraph ``thread_id`` shared by the guard and wrapper.

    Both the PTC bridge's derived runtime (guard side) and the ``eval`` tool's
    runtime (wrapper side) carry the same ``config``, so they agree on the key.
    Falls back to a constant when absent — which only happens when no
    checkpointer/thread is configured, i.e. when ``interrupt()`` could not run
    anyway.
    """
    config = getattr(runtime, "config", None)
    if isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict):
            thread_id = configurable.get("thread_id")
            if thread_id:
                return str(thread_id)
    return _PTC_DEFAULT_THREAD_ID


def begin_ptc_turn(thread_id: str) -> _PTCTurnState:
    """Start (or reset) a PTC approval turn for ``thread_id``."""
    state = _PTCTurnState()
    _PTC_TURNS[thread_id] = state
    return state


def end_ptc_turn(thread_id: str) -> None:
    """Discard the PTC approval turn for ``thread_id``."""
    _PTC_TURNS.pop(thread_id, None)


def get_ptc_turn(thread_id: str) -> _PTCTurnState | None:
    """Return the active PTC approval turn for ``thread_id`` (or ``None``)."""
    return _PTC_TURNS.get(thread_id)


def clear_ptc_pending(thread_id: str) -> None:
    """Drop any recorded pending approvals before a fresh ``eval`` run."""
    state = _PTC_TURNS.get(thread_id)
    if state is not None:
        state.pending = []


def take_ptc_pending(thread_id: str) -> list[_PendingApproval]:
    """Return and clear the pending approvals recorded by the last ``eval`` run."""
    state = _PTC_TURNS.get(thread_id)
    if state is None:
        return []
    pending = state.pending
    state.pending = []
    return pending


def _call_key(tool_name: str, args: dict[str, Any]) -> str:
    """Stable identity for a (tool, args) pair across ``eval`` re-runs."""
    try:
        payload = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - best-effort hashing of arbitrary args
        payload = repr(args)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


def _resolve_server_slug(
    tool_name: str,
    context: Any,
    static_map: dict[str, str] | None,
) -> str:
    """Resolve the MCP server slug for a tool (mirrors ConditionalHITL logic)."""
    if context is not None:
        ctx_map: dict[str, str] | None = getattr(context, "tool_server_map", None)
        if ctx_map and tool_name in ctx_map:
            return ctx_map[tool_name]
    if static_map and tool_name in static_map:
        return static_map[tool_name]
    return "_self"


def _inject_for_inner(
    inner: BaseTool,
    kwargs: dict[str, Any],
    runtime: ToolRuntime | None,
) -> dict[str, Any]:
    """Re-create LangGraph's injected args for ``inner`` from ``runtime``.

    The PTC bridge injects ``runtime`` into the *wrapper*; the inner tool needs
    its own injected ``runtime`` / ``state`` / ``store``. This mirrors the
    library's ``_inject_tool_args_for_ptc`` so wrapped filesystem tools keep
    their backend wiring.
    """
    enriched = dict(kwargs)
    if runtime is None:
        return enriched
    try:
        from langgraph.prebuilt.tool_node import _get_all_injected_args
    except ImportError:  # pragma: no cover - langgraph always present
        return enriched

    injected = _get_all_injected_args(inner)
    if not injected:
        return enriched

    if injected.runtime:
        enriched[injected.runtime] = runtime
    if injected.state:
        state = runtime.state
        for arg_name, state_field in injected.state.items():
            if state_field:
                enriched[arg_name] = (
                    state.get(state_field) if isinstance(state, dict) else getattr(state, state_field, None)
                )
            else:
                enriched[arg_name] = state
    store = getattr(runtime, "store", None)
    if injected.store and store is not None:
        enriched[injected.store] = store
    return enriched


def wrap_tool_for_ptc(
    inner: BaseTool,
    *,
    risk_scorer: RiskScorerFn | None,
    tool_risk_cache: ToolRiskCache | None = None,
    default_risk_threshold: float = 0.8,
    tool_server_map: dict[str, str] | None = None,
) -> BaseTool:
    """Wrap ``inner`` with a runtime HITL risk guard for safe PTC exposure.

    The returned tool keeps ``inner``'s name, description and ``args_schema``
    (so the PTC prompt and call shape are unchanged) and adds an injected
    ``runtime`` parameter that the PTC bridge populates with the live per-user
    context. On each call it applies the same risk decision the
    ``ConditionalHumanInTheLoopMiddleware`` would, returning an
    approval-required payload (via :func:`approval_required_payload`) when
    approval would be required.

    Args:
        inner: The real tool to expose inside ``eval``.
        risk_scorer: The dynamic risk scorer (``score_tool_risk``). When
            ``None``, no guard is applied and the inner tool runs directly.
        tool_risk_cache: Shared risk cache, used as a fallback when the runtime
            context does not carry one.
        default_risk_threshold: Score at/above which approval is required.
        tool_server_map: Static tool-name -> server-slug map (sub-agent path).

    Returns:
        A ``StructuredTool`` suitable for the ``ptc=[...]`` allowlist.
    """
    tool_name = inner.name

    async def _execute(runtime: ToolRuntime | None, kwargs: dict[str, Any]) -> Any:
        return await inner.arun(_inject_for_inner(inner, kwargs, runtime))

    async def _guarded(runtime: ToolRuntime = None, **kwargs: Any) -> Any:  # type: ignore[assignment]
        if risk_scorer is None:
            return await _execute(runtime, kwargs)

        from agent_common.middleware.conditional_hitl import (
            ConditionalHumanInTheLoopMiddleware,
        )

        context: Any = getattr(runtime, "context", None)
        server_slug = _resolve_server_slug(tool_name, context, tool_server_map)
        thread_id = resolve_ptc_thread_id(runtime)
        turn = get_ptc_turn(thread_id)
        call_key = _call_key(tool_name, kwargs)

        # 1. Within one process execution, never re-run an already-executed call
        #    (dedups the post-interrupt replay of approved/low-risk calls).
        if turn is not None and call_key in turn.results:
            return turn.results[call_key]

        # 2. Honor a human decision recorded for this call earlier in the turn
        #    (re-applied on every interrupt replay via the resume value).
        if turn is not None:
            decision = turn.decisions.get(call_key)
            if decision == "reject":
                return rejection_payload(tool_name)
            if decision == "approve":
                result = await _execute(runtime, kwargs)
                turn.results[call_key] = result
                return result

        # 3. Per-user bypass rules (allow-all / allow-pattern this session).
        bypass_rules = getattr(context, "tool_bypass_rules", None) if context else None
        if bypass_rules and ConditionalHumanInTheLoopMiddleware._is_bypassed(
            tool_name, server_slug, kwargs, bypass_rules
        ):
            result = await _execute(runtime, kwargs)
            if turn is not None:
                turn.results[call_key] = result
            return result

        # 4. Score the call; below threshold executes, at/above records for HITL.
        cache: ToolRiskCache | None = (
            getattr(context, "tool_risk_cache", None) if context else None
        ) or tool_risk_cache

        threshold = default_risk_threshold
        if context is not None:
            override = getattr(context, "risk_threshold", None)
            if override is not None:
                threshold = float(override)

        try:
            score, entry = await risk_scorer(
                tool_name,
                kwargs,
                tool=inner,
                cache=cache,
                server_slug=server_slug,
            )
        except Exception:
            logger.exception(
                "PTC risk scoring failed for '%s'; proceeding without guard",
                tool_name,
            )
            score, entry = 0.0, None

        if score < threshold:
            result = await _execute(runtime, kwargs)
            if turn is not None:
                turn.results[call_key] = result
            return result

        # At/above threshold: record the pending approval so the enclosing
        # ``awrap_tool_call`` can fire a single batched ``interrupt()`` after
        # this ``eval`` run. When no PTC turn is active (no interrupt is
        # possible) we still block by returning the approval payload.
        if turn is not None:
            allowed = list(entry.allowed_actions) if entry else ["approve", "reject"]
            # PTC cannot honor "edit" — the approved call is re-executed verbatim
            # from the re-run ``eval`` code, so there is no per-call arg to edit.
            allowed = [a for a in allowed if a != "edit"] or ["approve", "reject"]
            matched = entry.get_matched_pattern(kwargs) if entry else None
            turn.record_pending(
                _PendingApproval(
                    call_key=call_key,
                    tool_name=tool_name,
                    args=dict(kwargs),
                    server_slug=server_slug,
                    allowed_actions=allowed,
                    score=score,
                    threshold=threshold,
                    matched_pattern=matched,
                )
            )
        return approval_required_payload(tool_name)

    # ``from __future__ import annotations`` stores ``_guarded``'s annotations as
    # strings, so ``StructuredTool._injected_args_keys`` -- which reads the raw
    # ``signature(fn).parameters[...].annotation`` -- would not recognise
    # ``runtime`` as a directly-injected ``ToolRuntime`` argument. Without that,
    # the ``runtime`` the PTC bridge injects is stripped by
    # ``BaseTool._parse_input`` (the wrapper reuses the inner tool's LLM-facing
    # ``args_schema``, which has no ``runtime`` field) before it reaches
    # ``_guarded``, leaving ``runtime=None`` and crashing the inner tool with a
    # missing ``runtime`` argument. Pin the real type object so injection
    # detection works regardless of the string-annotation behaviour.
    _guarded.__annotations__["runtime"] = ToolRuntime

    return StructuredTool.from_function(
        coroutine=_guarded,
        name=tool_name,
        description=inner.description,
        args_schema=inner.args_schema,
        # Preserve the inner tool's metadata (notably ``server_name``) so downstream
        # consumers can still distinguish MCP tools from base tools on the *wrapped*
        # instance — e.g. the PTC middleware's core-vs-catalog render split.
        metadata=inner.metadata,
    )


class HiddenToolsFromModelMiddleware(AgentMiddleware):
    """Strip a set of tool names from the model-facing tool list each call.

    Tools that are fully available (and safe) via PTC ``eval`` -- e.g. the
    read-only filesystem tools -- are hidden from the model's normal tool list
    to reduce entropy, while remaining executable through the ``eval`` bridge
    (which dispatches the wrapped instances directly, independent of
    ``request.tools``) and through ``ToolNode`` if ever called.
    """

    def __init__(self, hidden_tool_names: set[str]) -> None:
        super().__init__()
        self._hidden = set(hidden_tool_names)

    def _filter(self, request: ModelRequest) -> ModelRequest:
        tools = list(getattr(request, "tools", []) or [])
        if not self._hidden or not tools:
            return request
        kept = [t for t in tools if getattr(t, "name", None) not in self._hidden]
        if len(kept) == len(tools):
            return request
        return request.override(tools=kept)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._filter(request))
