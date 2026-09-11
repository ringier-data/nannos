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

import asyncio
import hashlib
import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.config import get_config
from langgraph.prebuilt import ToolRuntime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from agent_common.core.tool_risk_cache import ToolRiskCache
    from agent_common.middleware.conditional_hitl import RiskScorerFn
    from agent_common.middleware.loop_detection_middleware import RepeatedToolCallMiddleware

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


def rejection_payload(tool_name: str, reason: str = "") -> dict[str, str]:
    """Build the error payload returned when the user rejects a PTC call.

    Surfaced inside ``eval`` on the approved/rejected re-run when the user chose
    ``reject`` for this call. The tool is not executed.

    ``reason`` is the human-written explanation that came with the rejection (a
    skipped authorization, say). Without it the model only sees "not approved"
    and invents a cause — it told a user the tool did not exist when in truth
    they had declined to authorize it.
    """
    message = _PTC_REJECTION_MESSAGE.format(tool_name=tool_name)
    if reason.strip():
        message = f"{reason.strip()} ({message})"
    return {
        "error": _PTC_REJECTION_ERROR,
        "tool": tool_name,
        "message": message,
    }


_PTC_RATE_LIMITED_ERROR = "rate_limited"

_PTC_RATE_LIMITED_MESSAGE = (
    "Tool '{tool_name}' is being rate-limited by its provider; the call was not served. "
    "Retrying now — or with a reworded query — will fail the same way, and there is no "
    "way to wait inside `eval`. Stop calling '{tool_name}' for this task and report what "
    "you have so far, saying the provider was rate-limited."
)

_PTC_REPEATED_CALL_ERROR = "repeated_call"

#: Program-made calls are recorded in ``RepeatedToolCallMiddleware``'s ``tool_call_history``
#: under this prefix + the inner tool's name, not under the bare name the model
#: boundary uses for direct calls. The identical-arguments rule is judged within the
#: program namespace (the #211 loop: the same call re-issued across ``eval`` turns);
#: the boundary's same-tool cap counts model turns, and a program iterating over 20
#: items in one turn must neither trip it nor prime it for the next direct call.
PTC_HISTORY_KEY_PREFIX = "eval:"


def ptc_history_key(tool_name: str) -> str:
    """The ``tool_call_history`` key under which program calls to ``tool_name`` are kept."""
    return f"{PTC_HISTORY_KEY_PREFIX}{tool_name}"


#: Provider / gateway wording for quota exhaustion: ``403 ... rate limit exceeded`` is
#: a quota problem, not an authorization one, and ``too many requests`` is how the
#: MCP gateway words its own throttling. The status code is word-bounded so a request
#: id or an issue number containing ``429`` is not a rate limit, and the wording must
#: start a word so an echoed ``x-ratelimit-remaining`` header inside an unrelated error
#: is not one either. This is the single definition — the orchestrator's error
#: classification imports it — so the PTC path and the model-boundary path can never
#: disagree on what a rate limit looks like.
RATE_LIMIT_PATTERN = re.compile(r"(?<![\w-])rate.?limit|\b429\b|too.many.requests", re.IGNORECASE)

_RATE_LIMIT_DETAIL_CHARS = 300


def is_rate_limit_error(text: str) -> bool:
    """Whether an error message describes quota exhaustion (provider or gateway)."""
    return bool(RATE_LIMIT_PATTERN.search(text or ""))


def rate_limited_payload(tool_name: str, detail: str = "") -> dict[str, str]:
    """Build the error payload returned when the inner tool was rate-limited.

    Returned (not raised) from the guarded wrapper for the same reason as the
    approval/rejection payloads: a raised exception surfaces in the program as a
    bare throw it will catch and retry, while a value tells it what happened and
    that retrying is pointless. The model cannot sleep inside ``eval``, so a quota
    error is terminal for the run — see :data:`_PTC_RATE_LIMITED_MESSAGE`.
    """
    payload = {
        "error": _PTC_RATE_LIMITED_ERROR,
        "tool": tool_name,
        "message": _PTC_RATE_LIMITED_MESSAGE.format(tool_name=tool_name),
    }
    if detail.strip():
        payload["detail"] = detail.strip()[:_RATE_LIMIT_DETAIL_CHARS]
    return payload


def repeated_call_payload(tool_name: str, message: str) -> dict[str, str]:
    """Build the error payload returned when the loop rule blocks an inner call.

    ``message`` is ``RepeatedToolCallMiddleware.blocked_message`` — the very text the
    model would have received had it made the call directly — so the instruction is
    identical whether the loop was caught at the model boundary or inside ``eval``.
    """
    return {"error": _PTC_REPEATED_CALL_ERROR, "tool": tool_name, "message": message}


# The gateway's secondary-authorization error. Raised by the INNER tool, but it
# escapes ``eval``, so the middleware that reads it sees only the sandbox tool
# and would report "authorize eval" — which is meaningless to a user and reads
# to the model as "the real tool is unavailable". Annotating the payload here,
# where the inner tool is known, is the only place the two facts meet.
_NEED_CREDENTIALS_FIELD = '"errorCode"'


def annotate_need_credentials(exc: BaseException, tool_name: str, server_slug: str | None) -> BaseException:
    """Name the tool (and its server) inside a ``need-credentials`` payload.

    Returns ``exc`` untouched for anything else, and for a payload that is not a
    plain JSON object — a best-effort annotation must never swallow the original
    error or change how it is detected.
    """
    text = str(exc)
    if _NEED_CREDENTIALS_FIELD not in text or "need-credentials" not in text:
        return exc
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return exc
    if not isinstance(payload, dict) or payload.get("errorCode") != "need-credentials":
        return exc
    payload.setdefault("tool", tool_name)
    if server_slug:
        payload.setdefault("service", server_slug)
    return ToolException(json.dumps(payload))


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
    #: Why a call was rejected, by call key — surfaced to the model with the
    #: rejection so it can say what actually happened instead of guessing.
    reject_reasons: dict[str, str] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    #: Scope component of every ask id minted for this turn (see ``ask_id``). Owned
    #: by the TURN, not by the wrapper that happens to drain ``pending``: the list is
    #: thread-keyed, so with two parallel ``eval`` calls either wrapper may drain a
    #: mixed list. Reading the scope from here keeps stamping and matching consistent
    #: whichever one does it — taking it from the draining wrapper's own tool call id
    #: made ask-id matching depend on a nondeterministic race. Two ``eval`` calls in one
    #: step no longer overlap (:func:`serialized_eval`, #217), so each drains its own
    #: list; keeping the scope on the turn is what makes that per-call scoping exact.
    ask_scope: str = ""
    #: ``RepeatedToolCallMiddleware``'s ``tool_call_history`` (per tool name, the arg
    #: hashes of past calls), seeded from checkpointed graph state by the PTC middleware
    #: when the turn begins and written back when the ``eval`` call returns. It lives on
    #: the turn only so the guard — which runs on the sandbox bridge thread, without
    #: access to graph state — can read and extend it; the durable copy is the state
    #: field the loop middleware itself owns, never this process.
    tool_call_history: dict[str, list[str]] = field(default_factory=dict)
    #: What this ``eval`` actually ADDED to ``tool_call_history``, per key, and the
    #: window to apply afterwards. The write-back is emitted as an incremental delta
    #: rather than as ``tool_call_history`` itself, because a step with two ``eval``
    #: calls has two tasks writing that one channel — see
    #: ``loop_detection_middleware.merge_tool_call_history`` and #217. Tracked here
    #: rather than diffed against the seed at the end: a diff cannot distinguish a
    #: hash this eval appended from one the window dropped.
    history_appends: dict[str, list[str]] = field(default_factory=dict)
    #: Per key, the sliding window to apply after appending, or ``None`` to leave it
    #: unbounded. ``None`` is sticky: once any call on this key was blocked, the loop
    #: middleware deliberately stops trimming so repeat counts keep escalating and
    #: ``force_stop_after`` can fire, and a later allowed call must not re-impose the
    #: window and undo that.
    history_caps: dict[str, int | None] = field(default_factory=dict)

    def record_pending(self, item: _PendingApproval) -> None:
        if any(p.call_key == item.call_key for p in self.pending):
            return
        self.pending.append(item)

    def record_history_append(self, key: str, args_hash: str, cap: int | None) -> None:
        """Note one arg hash this ``eval`` added under ``key``, and the window for it."""
        self.history_appends.setdefault(key, []).append(args_hash)
        if key in self.history_caps and self.history_caps[key] is None:
            return  # already unbounded because an earlier call on this key was blocked
        self.history_caps[key] = cap


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
    if not isinstance(config, dict):
        # Agent-level hooks (``after_agent``) get a ``Runtime`` without ``config``;
        # read the same thread id from LangGraph's config contextvar instead so the
        # per-run records keyed here can be cleared under the key they were made with.
        try:
            config = get_config()
        except RuntimeError:
            config = None
    if isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict):
            thread_id = configurable.get("thread_id")
            if thread_id:
                return str(thread_id)
    return _PTC_DEFAULT_THREAD_ID


@dataclass
class _EvalGate:
    """One thread's ``eval`` mutex, plus the count of callers still using it."""

    #: A ``threading`` lock, NOT an ``asyncio`` one, because the evals it has to
    #: serialize do not all share an event loop. Two parallel ``task`` dispatches of
    #: the same sub-agent land on one ``thread_id`` (the default is
    #: ``{context_id}::{checkpoint_ns}``) but reach it through
    #: ``LocalA2ARunnable.invoke`` → ``asyncio.run`` (``a2a/base.py``), which builds a
    #: fresh loop per dispatch on a ToolNode executor thread. An ``asyncio.Lock``
    #: binds to the loop that first awaits it and would serialize neither.
    lock: threading.Lock
    #: Callers inside :func:`serialized_eval` for this key — holder and waiters
    #: alike. The gate is dropped when it reaches zero, so the registry does not
    #: grow one permanent entry per conversation for the life of the process.
    users: int = 0


#: Live ``eval`` gates, keyed by ``thread_id`` — the same key the resources being
#: protected use (``_PTC_TURNS``, the pending collector, and ``langchain_quickjs``'s
#: REPL slot registry), so the gate cannot be finer-grained than they are.
_PTC_EVAL_GATES: dict[str, _EvalGate] = {}

#: Guards ``_PTC_EVAL_GATES`` itself. Needed because the registry is now reached from
#: several threads, not just several tasks on one loop.
_PTC_EVAL_GATES_LOCK = threading.Lock()

#: How long to wait between attempts on a held gate. Polling rather than blocking
#: because the lock has to be acquirable from any loop: a blocking acquire would
#: stall the caller's whole event loop, and handing it to ``asyncio.to_thread``
#: would park an executor worker per waiter — which can deadlock, since the holder
#: itself needs a worker for the summarizer's ``to_thread`` call. Coarse on purpose:
#: an ``eval`` runs for orders of magnitude longer than this.
_GATE_POLL_SECONDS = 0.01


@asynccontextmanager
async def serialized_eval(thread_id: str):
    """Hold the thread's ``eval`` mutex for the duration of one ``eval`` execution.

    Nothing guarantees a model step emits at most one ``eval`` tool call, and
    ``ToolNode`` runs the calls of one assistant message concurrently — but every
    piece of per-``eval`` bookkeeping on the PTC path is keyed by ``thread_id``
    alone and assumes one ``eval`` in flight per thread (#217):

    * upstream, ``langchain_quickjs`` hands both calls the *same* QuickJS context
      and, in ``mode="call"``, closes it in whichever ``finally`` runs first —
      killing the other eval mid-execution with ``already closed``, out of the
      tool node, taking the whole agent turn down with it;
    * :func:`begin_ptc_turn` *replaces* the thread's turn, so the second call
      swaps the first's turn out from under it — shared ``results``/``decisions``,
      an inert repeat guard, and a ``tool_call_history`` write-back computed
      against a stale seed (last writer wins);
    * :func:`take_ptc_pending` drains one collector per thread, so approvals
      raised by two evals land in whichever ``interrupt()`` fires first.

    Serializing here fixes all three at once and is the only one of the three
    candidate fixes that is wholly ours: re-keying the two collectors by
    ``(thread_id, tool_call_id)`` still leaves the REPL colliding underneath, and
    constraining the model (``parallel_tool_calls=False``) would disable parallel
    calls for *every* tool, not just ``eval``. Concurrency for other tools is
    untouched — this gate is only ever entered on the ``eval`` path.

    The two evals therefore run back-to-back, each with its own turn and its own
    REPL reset in between, which is what the bookkeeping already assumes.
    """
    with _PTC_EVAL_GATES_LOCK:
        gate = _PTC_EVAL_GATES.get(thread_id)
        if gate is None:
            gate = _EvalGate(threading.Lock())
            _PTC_EVAL_GATES[thread_id] = gate
        # Bump before contending so a waiter keeps the gate alive while the holder's
        # ``finally`` runs and would otherwise drop it from the registry.
        gate.users += 1
    try:
        while not gate.lock.acquire(blocking=False):
            await asyncio.sleep(_GATE_POLL_SECONDS)
        try:
            yield
        finally:
            gate.lock.release()
    finally:
        with _PTC_EVAL_GATES_LOCK:
            gate.users -= 1
            if gate.users == 0:
                _PTC_EVAL_GATES.pop(thread_id, None)


# One turn per thread, not per ``eval`` call: two concurrent ``eval`` calls on one
# thread would swap the turn out from under each other. Callers must therefore
# hold :func:`serialized_eval` for the thread across the whole begin/end span —
# ``_PTCToleranceCodeInterpreterMiddleware.awrap_tool_call`` is the one caller and
# does. The constraint is inherited rather than chosen: ``langchain_quickjs`` keys
# the REPL slot per thread and resets it after every call in ``mode="call"``, so
# concurrent evals collide one layer down however this dict is keyed (#217).
def begin_ptc_turn(thread_id: str, ask_scope: str = "") -> _PTCTurnState:
    """Start (or reset) a PTC approval turn for ``thread_id``.

    ``ask_scope`` is the ``eval`` tool call id this turn belongs to; it scopes the
    turn's ask ids (see ``ask_id``) and is read back off the state rather than from
    whichever wrapper drains ``pending``.
    """
    state = _PTCTurnState(ask_scope=ask_scope)
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
    """Stable identity for a (tool, args) pair across ``eval`` re-runs.

    Deliberately content-derived: after an approval the guard re-runs the whole
    ``eval`` program, and it must recognise the calls it already asked about so it
    can replay the decision (``turn.decisions``) and reuse the cached result
    (``turn.results``) instead of prompting again. Two *different* questions about
    the same tool+args are therefore indistinguishable by this key — which is why
    it is NOT what goes on the wire; see ``ask_id``.

    This is a per-turn decision/result memo key, not a bypass rule. The bypass
    policy is a separate, durable, per-user thing (``context.tool_bypass_rules``,
    keyed ``tool_name::server_slug`` with glob patterns) that outlives the turn.
    """
    try:
        payload = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - best-effort hashing of arbitrary args
        payload = repr(args)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


#: Separates the ``call_key`` from the ask ordinal in a wire ``_call_id``. Not
#: ``#``, which the embed SDK already uses to suffix a client-action part id.
_ASK_SEPARATOR = "@"


def ask_id(call_key: str, ask_scope: str, ask_round: int) -> str:
    """The wire ``_call_id`` for ONE approval question about ``call_key``.

    Clients need the opposite property from ``_call_key``: every question must
    carry its own id, so that answering one is never mistaken for answering
    another. A client that suppresses prompts it has already answered (the embed
    SDK does, to swallow a snapshot replay racing a resume) otherwise drops the
    second, genuine ask for an identical call and parks the turn forever.

    Uniqueness comes from two nested scopes, because an identical call can be asked
    about twice at two different levels:

    * ``ask_scope`` — the ``eval`` tool call this ask belongs to. A PTC turn lives
      for ONE ``eval`` invocation, and within it an identical call is served from
      ``turn.results``, so it never asks twice. Two asks therefore mean two ``eval``
      calls — which is the case seen in the wild, and which a per-turn counter alone
      cannot tell apart (both would be round 0). The model's ``tool_call["id"]``
      separates them and is checkpointed on the AI message, so it survives replay.
    * ``ask_round`` — the interrupt round within one ``eval`` invocation, for a
      program whose later calls only become reachable once earlier ones are decided.

    Both are replay-stable: the node re-runs deterministically from the top on every
    resume, so round N's ``interrupt()`` is always round N's, under the same tool
    call id. Embedding ``call_key`` keeps decision matching order-independent, which
    is what it was chosen for (parallel ``eval`` calls register concurrently, so the
    re-run's pending order can differ from the order the human saw).

    The scope is folded to a short digest rather than carried verbatim: these ids ride
    in places with hard size budgets (Slack packs a batch of them, base64-encoded, into
    a 2000-char button ``value``), and raw tool call ids are provider-sized — Gemini's
    are markedly longer than OpenAI's. A digest keeps the id's growth constant instead
    of letting the model's provider decide it. The scope stays greppable in traces and
    logs, where the raw id is what appears.
    """
    scope = hashlib.sha256(ask_scope.encode("utf-8")).hexdigest()[:8] if ask_scope else "-"
    return f"{call_key}{_ASK_SEPARATOR}{scope}:{ask_round}"


def _resolve_server_slug(
    tool_name: str,
    context: Any,
    static_map: dict[str, str] | None,
    tool: BaseTool | None = None,
) -> str:
    """Resolve the MCP server slug for a tool (mirrors ConditionalHITL logic).

    Resolution order matches ``ConditionalHITL._get_server_slug`` so a tool scores
    under the SAME slug whether the risk gate fires at the model boundary or here,
    inside the PTC code interpreter — otherwise the same tool splits across two
    rows (real slug vs ``_self``) and per-tool HITL overrides silently miss the
    eval path:
      1. ``context.tool_server_map`` (orchestrator path)
      2. wrap-time ``static_map`` (sub-agent injects at build)
      3. ``tool.metadata["server_name"]`` (stamped by MCP discovery)
      4. ``_self`` (in-process platform tools)
    """
    if context is not None:
        ctx_map: dict[str, str] | None = getattr(context, "tool_server_map", None)
        if ctx_map and tool_name in ctx_map:
            return ctx_map[tool_name]
    if static_map and tool_name in static_map:
        return static_map[tool_name]
    if tool is not None:
        metadata = getattr(tool, "metadata", None)
        if isinstance(metadata, dict):
            server_name = metadata.get("server_name")
            if server_name:
                return server_name
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
    loop_detection: RepeatedToolCallMiddleware | None = None,
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

    async def _execute(
        runtime: ToolRuntime | None,
        kwargs: dict[str, Any],
        server_slug: str | None = None,
        *,
        turn: _PTCTurnState | None,
        call_key: str,
    ) -> Any:
        # The loop rule, applied to the call the model boundary cannot see (it only
        # sees ``eval``): the same ``RepeatedToolCallMiddleware`` instance that judges
        # direct calls judges this one, against the same ``tool_call_history``, so one
        # configuration governs both paths — as the HITL guard above reuses the HITL
        # middleware's policy. Applied at the point of execution, so HITL round-trips
        # (pending → interrupt replay → approved) and cache hits never count. Outside
        # an ``eval`` turn there is no history to judge against and the check is inert.
        if turn is not None and loop_detection is not None and loop_detection.applies_to(tool_name):
            key = ptc_history_key(tool_name)
            verdict = loop_detection.evaluate(tool_name, kwargs, turn.tool_call_history.get(key, []), program_call=True)
            turn.tool_call_history[key] = verdict.history
            # ``evaluate`` appends the new hash last and trims from the front, so the
            # tail is always this call's own hash. A blocked call is left uncapped,
            # mirroring the window rule ``evaluate`` itself applies.
            turn.record_history_append(
                key,
                verdict.history[-1],
                None if verdict.blocked else loop_detection.window_size,
            )
            if verdict.blocked:
                return repeated_call_payload(tool_name, loop_detection.blocked_message(tool_name, verdict))
        try:
            return await inner.arun(_inject_for_inner(inner, kwargs, runtime))
        except ToolException as exc:
            # A quota error is terminal for this run: the model cannot wait inside
            # ``eval`` and every immediate retry fails the same way. Return it, like
            # the approval/rejection payloads, so the program sees a value naming
            # the cause instead of a bare throw it will catch and retry (#211).
            if is_rate_limit_error(str(exc)):
                logger.warning("PTC call to '%s' was rate-limited; returning terminal payload", tool_name)
                return rate_limited_payload(tool_name, str(exc))
            # Stamp the inner tool onto a secondary-authorization error before it
            # escapes into ``eval`` and loses that fact (see annotate_need_credentials).
            raise annotate_need_credentials(exc, tool_name, server_slug) from None

    async def _guarded(runtime: ToolRuntime = None, **kwargs: Any) -> Any:  # type: ignore[assignment]
        context: Any = getattr(runtime, "context", None)
        server_slug = _resolve_server_slug(tool_name, context, tool_server_map, inner)
        thread_id = resolve_ptc_thread_id(runtime)
        turn = get_ptc_turn(thread_id)
        call_key = _call_key(tool_name, kwargs)

        # 1. Within one process execution, never re-run an already-executed call
        #    (dedups the post-interrupt replay of approved/low-risk calls).
        if turn is not None and call_key in turn.results:
            return turn.results[call_key]

        if risk_scorer is None:
            return await _execute(runtime, kwargs, server_slug, turn=turn, call_key=call_key)

        from agent_common.middleware.conditional_hitl import (
            ConditionalHumanInTheLoopMiddleware,
        )

        # 2. Honor a human decision recorded for this call earlier in the turn
        #    (re-applied on every interrupt replay via the resume value).
        if turn is not None:
            decision = turn.decisions.get(call_key)
            if decision == "reject":
                return rejection_payload(tool_name, turn.reject_reasons.get(call_key, ""))
            if decision == "approve":
                result = await _execute(runtime, kwargs, server_slug, turn=turn, call_key=call_key)
                turn.results[call_key] = result
                return result

        # 3. Per-user bypass rules (allow-all / allow-pattern this session).
        bypass_rules = getattr(context, "tool_bypass_rules", None) if context else None
        if bypass_rules and ConditionalHumanInTheLoopMiddleware._is_bypassed(
            tool_name, server_slug, kwargs, bypass_rules
        ):
            result = await _execute(runtime, kwargs, server_slug, turn=turn, call_key=call_key)
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
            result = await _execute(runtime, kwargs, server_slug, turn=turn, call_key=call_key)
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
