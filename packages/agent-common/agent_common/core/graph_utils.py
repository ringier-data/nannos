"""Shared agent graph utilities.

Provides helpers shared between the orchestrator, agent-runner and dynamic local sub-agents
to avoid duplicating the common middleware stack.

Key exports
-----------
build_common_middleware_stack
    Assemble the standard list of middlewares (Filesystem, Summarization,
    Anthropic caching, tool retries, …).

create_indexing_backend_factory
    Return a backend-factory callable that routes ``/memories/`` writes through
    ``IndexingStoreBackend`` (semantic indexing) when a document store is
    available, and falls back to ephemeral ``StateBackend`` otherwise.

build_sub_agent_graph
    One-stop helper that combines ``create_indexing_backend_factory``,
    ``build_common_middleware_stack``, and ``create_agent`` into a single
    call.  Intended for agents that do not need the orchestrator's custom
    middleware ordering (``ToolsetSelectorMiddleware``,
    ``DynamicToolDispatchMiddleware``, …).  Also used by
    ``DynamicLocalAgentRunnable`` to avoid duplicating the build logic.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
from typing import TYPE_CHECKING, Annotated, Any, Iterator, Optional

import httpx
from langchain_core.tools import BaseTool, ToolException
from langgraph.errors import GraphBubbleUp

if TYPE_CHECKING:
    from deepagents.backends.protocol import SandboxBackendProtocol

    from agent_common.core.tool_risk_cache import ToolRiskCache
    from agent_common.middleware.conditional_hitl import RiskScorerFn

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.state import StateBackend
from deepagents.middleware import FilesystemMiddleware, SummarizationMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ToolRetryMiddleware
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_quickjs import CodeInterpreterMiddleware
from langchain_quickjs.middleware import REPLState, _resolve_thread_id
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import interrupt
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.middleware.tool_schema_cleaning import ToolSchemaCleaningMiddleware
from typing_extensions import NotRequired

from agent_common.backends.attachments_store import ContextScopedAttachmentsBackend
from agent_common.backends.indexing_store import IndexingStoreBackend
from agent_common.backends.skills_store import SkillsStoreBackend
from agent_common.middleware.conversation_context_tools_middleware import (
    ContextGatedTool,
    ConversationContextToolsMiddleware,
)
from agent_common.middleware.loop_detection_middleware import RepeatedToolCallMiddleware
from agent_common.middleware.prompt_caching import LiteLLMPromptCachingMiddleware
from agent_common.core.model_factory import is_gemini_model
from agent_common.core.ptc_discovery import (
    PTC_DESCRIBE_TOOL_NAME,
    PTC_SEARCH_TOOL_NAME,
    build_discovery_tools,
)
from agent_common.core.ptc_signatures import render_tools_namespace
from agent_common.middleware.ptc_guard import (
    PTC_CODE_INTERPRETER_TOOL_NAME,
    begin_ptc_turn,
    clear_ptc_pending,
    end_ptc_turn,
    resolve_ptc_thread_id,
    take_ptc_pending,
    wrap_tool_for_ptc,
)
from agent_common.middleware.storage_paths_middleware import StoragePathsInstructionMiddleware
from agent_common.middleware.tool_status import ToolStatusMiddleware
from agent_common.models.skill import ResolvedSkill

logger = logging.getLogger(__name__)


# langgraph parent-Pregel scope keys (langgraph/_internal/_constants.py) that must
# NOT leak from a parent graph's node into a sub-agent graph invoked inside it.
# We avoid importing the private constants and pin the literal values instead.
#
#   * ``__pregel_task_id`` — when present in a graph's effective ``configurable``
#     the Pregel loop marks itself ``is_nested=True`` (langgraph/pregel/_loop.py),
#     so a ``GraphInterrupt`` propagates as an exception instead of being
#     suppressed + saved to the checkpoint.
#   * ``checkpoint_id`` / ``checkpoint_map`` — the parent's *checkpoint
#     coordinates*. ``checkpoint_map`` maps a checkpoint namespace to the specific
#     checkpoint id to load and is populated while the parent is *resuming*. If it
#     leaks into the sub-agent's config, the sub-agent's Pregel resolves a parent
#     checkpoint id for its own (thread, ns) instead of loading the LATEST
#     checkpoint of its own thread — so on a standalone ``Command(resume)`` it
#     finds no pending interrupt, discards the resume, and re-runs the model from
#     an empty state (the "sub-agent greets / approved call lost" symptom).
#
# ``checkpoint_ns`` and ``__pregel_checkpointer`` are deliberately NOT stripped:
# the sub-agent's explicit standalone config already overrides ``checkpoint_ns``
# (to ``""``) and shares the orchestrator's single checkpointer instance, so the
# inherited values are harmless. Stripping ``__pregel_checkpointer`` additionally
# breaks interrupt suppression when the sub-agent does not bring its own
# checkpointer.
_PREGEL_TASK_ID_KEY = "__pregel_task_id"
_PARENT_PREGEL_SCOPE_KEYS = frozenset(
    {
        _PREGEL_TASK_ID_KEY,
        "checkpoint_id",
        "checkpoint_map",
    }
)


@contextlib.contextmanager
def denest_parent_pregel_context() -> Iterator[None]:
    """Run a sub-graph as a standalone root (``is_nested=False``).

    When a sub-agent graph is invoked from *inside* a parent graph's node (as the
    orchestrator does when dispatching a task), langgraph's ``merge_configs``
    deep-merges the parent's ``configurable`` from the
    ``var_child_runnable_config`` contextvar into the sub-graph's config. Parent
    keys the sub-graph does not explicitly override therefore leak in. Two
    failure modes result:

    1. ``__pregel_task_id`` flips the sub-graph's Pregel loop to
       ``is_nested=True``, so a ``GraphInterrupt`` *propagates as an exception*
       instead of being suppressed and saved to the checkpoint.
    2. The parent's *checkpoint coordinates* (``checkpoint_id`` /
       ``checkpoint_map``) leak in. On the **resume** path — where the parent is
       itself replaying under a ``Command(resume)`` — ``checkpoint_map`` carries
       the parent's namespace→checkpoint-id resolution, so the sub-agent's Pregel
       resolves a parent checkpoint id for its own thread instead of loading the
       LATEST checkpoint of its own thread. It finds no pending interrupt,
       silently discards the resume, and re-runs the model from an empty state —
       the approved tool call is lost and the sub-agent answers as if freshly
       invoked (the "sub-agent greets / approved eval lost" production symptom).
       This leak is invisible on the *first* dispatch (the parent is not
       resuming, so there is no stale ``checkpoint_map``), which is why the bug
       only surfaces on HITL resume.

    Stripping all of these keys (:data:`_PARENT_PREGEL_SCOPE_KEYS`) from the
    inherited contextvar restores true standalone-root behaviour: the interrupt is
    suppressed and persisted to the sub-agent's own checkpointer/thread, the
    post-stream ``aget_state`` check re-raises it, and on resume the interrupted
    tool node replays cleanly against the sub-agent's own latest checkpoint. This
    is a no-op when there is no parent context. The contextvar must stay active
    for the whole sub-graph iteration, so callers wrap the astream generator.
    """
    from langchain_core.runnables.config import var_child_runnable_config

    parent = var_child_runnable_config.get()
    if not parent or not (_PARENT_PREGEL_SCOPE_KEYS & parent.get("configurable", {}).keys()):
        yield
        return

    sanitized = {
        **parent,
        "configurable": {k: v for k, v in parent["configurable"].items() if k not in _PARENT_PREGEL_SCOPE_KEYS},
    }
    token = var_child_runnable_config.set(sanitized)
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


# langgraph's stream handler for ``stream_mode=["messages"|"custom"]``
# (langgraph/pregel/_messages.py). It is an *inheritable* callback handler, so a
# sub-agent graph invoked inside a parent node inherits it via
# ``var_child_runnable_config``'s callback manager. We match by class name to
# avoid importing the private class.
_STREAM_MESSAGES_HANDLER = "StreamMessagesHandler"


@contextlib.contextmanager
def isolate_parent_stream_context() -> Iterator[None]:
    """Stop a sub-agent's token stream from leaking into the parent's stream.

    A sub-agent graph invoked inside a parent graph's node inherits the parent's
    langgraph ``StreamMessagesHandler`` through ``var_child_runnable_config``'s
    callback manager (the handler is *inheritable*). The sub-agent's own LLM
    token / tool-call chunks then fire the parent's handler as well, so they
    surface on the **parent's** ``messages`` stream stamped with the *parent's*
    ``thread_id`` and an empty namespace — indistinguishable from the parent's
    own model output. Downstream this leaks unattributed sub-agent activity into
    the orchestrator stream: e.g. an unprefixed ``"Using eval…"`` activity line
    (the sub-agent's ``eval`` tool-call chunk), and sub-agent thinking/content
    tokens mis-rendered as the orchestrator's. A ``thread_id`` filter cannot stop
    it because the leaked copy carries the parent's ``thread_id``.

    Removing only the inherited ``StreamMessagesHandler`` for the duration of the
    sub-agent invocation stops the leak. The sub-agent's *own* ``astream``
    installs its own ``StreamMessagesHandler`` for its own stream, so its events
    are still captured (and re-emitted with proper attribution by the dispatch).
    All other inherited handlers — LangSmith tracers, cost trackers — are
    preserved, so trace nesting and cost attribution are unaffected. No-op when
    there is no parent stream handler in context.
    """
    from langchain_core.runnables.config import var_child_runnable_config

    parent = var_child_runnable_config.get()
    callbacks = parent.get("callbacks") if parent else None
    handlers = getattr(callbacks, "handlers", None)
    if not handlers or not any(type(h).__name__ == _STREAM_MESSAGES_HANDLER for h in handlers):
        yield
        return

    manager = callbacks.copy()
    for handler in list(manager.handlers):
        if type(handler).__name__ == _STREAM_MESSAGES_HANDLER:
            manager.remove_handler(handler)
    token = var_child_runnable_config.set({**parent, "callbacks": manager})
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def _should_retry_tool_error(exc: Exception) -> bool:
    """Determine if a tool exception is transient and worth retrying.

    Only retries errors that may succeed on a subsequent attempt:
    - HTTP 5xx server errors (temporary server issues)
    - Network errors: connection failures, timeouts

    Does NOT retry (non-transient / deterministic):
    - GraphBubbleUp (LangGraph interrupt mechanism — must propagate)
    - ToolException (MCP errors like need-credentials, 400 validation, ResponseBodyError)
    - HTTP 4xx client errors (bad request, forbidden, not found)
    - Any other exception type
    """
    # GraphBubbleUp is raised by interrupt() — it MUST propagate to pause the graph.
    if isinstance(exc, GraphBubbleUp):
        raise exc

    if isinstance(exc, ToolException):
        return False

    if isinstance(exc, httpx.HTTPStatusError):
        # Only retry 5xx server errors (transient)
        return exc.response.status_code >= 500

    if isinstance(exc, httpx.HTTPError):
        # Network-level errors (ConnectError, TimeoutException, etc.) are transient
        return True

    return False


# Read-only filesystem tools: safe to expose via PTC ``eval``. They can never
# require HITL approval, so they form part of the stable PTC baseline.
_PTC_READONLY_FS_TOOLS: frozenset[str] = frozenset({"ls", "read_file", "glob", "grep"})

# Mutating filesystem tools: also exposed via PTC as part of the baseline. Their
# per-call risk guard (inside the PTC wrapper) re-applies the user's HITL/bypass
# rules at call time, so they are exposed (and hidden from the model) like every
# other PTC-exposed tool.
_PTC_MUTATING_FS_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})

# Sandbox tools exposed via PTC *only when the backend supports execution*
# (``supports_execution``). ``execute`` is a ``FilesystemMiddleware``-provided
# tool, not part of the agent's explicit tool list, so it is NOT in
# ``broaden_baseline_tools`` and would otherwise vanish from the ``eval`` REPL on
# an interrupt *resume* (where ``request.tools`` is empty and re-exposure relies
# on the static baseline). Wrapping it into the static baseline — gated on
# execution support so non-sandbox agents never see a dead ``tools.execute`` —
# keeps ``tools.execute(...)`` bound across the approve→resume re-run of ``eval``.
_PTC_SANDBOX_TOOLS: frozenset[str] = frozenset({"execute"})

# Tools that must NEVER be exposed inside the PTC ``eval`` bridge, regardless of
# what is present on the request or in the runtime tool registry:
#   - ``task``: sub-agent dispatch — spawns sub-agents and must run via the
#     normal graph path (deepagents dispatch), not from inside the JS REPL.
#   - ``write_todos``: planning/UI tool whose effect is the work-plan stream.
#   - ``FinalResponseSchema`` / ``SubAgentResponseSchema``: structured-response
#     schema "tools" — not executable; selecting them terminates the turn.
# The PTC self tool (``eval``) is always auto-excluded by ``filter_tools_for_ptc``.
_PTC_EXCLUDED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "task",
        "write_todos",
        "FinalResponseSchema",
        "SubAgentResponseSchema",
    }
)


# Checkpointed state key holding the exact set of tool *names* exposed inside the
# PTC ``eval`` bridge on the most recent model call. Persisted by
# ``_PTCToleranceCodeInterpreterMiddleware.aafter_model`` so that on an interrupt
# *resume* — where the model-call hook does not run and ``request.tools`` is empty
# — the eval REPL re-exposes exactly the (ToolsetSelector-filtered) set the
# original call used, instead of falling back to the full build-time baseline.
# Must equal the field name on ``_PTCExposureState``.
PTC_EXPOSED_TOOL_NAMES_STATE_KEY = "ptc_exposed_tool_names"

# When the number of MCP catalog tools (those carrying ``server_name`` metadata)
# exposed via PTC exceeds this threshold, the middleware switches to *core-only*
# rendering: it still exposes (installs bridges for) every tool, but renders only the
# stable, user-invariant core into the system prompt (filesystem/base tools plus the
# ``search``/``describe`` discovery tools) and instructs the model to find the rest at
# runtime. This keeps the prompt invariant across turns (restoring prompt caching) for
# the large-catalog GP agent, while small, fixed sub-agent toolsets stay fully rendered
# inline. Override via env for tuning.
PTC_INLINE_RENDER_THRESHOLD = int(os.getenv("PTC_INLINE_RENDER_THRESHOLD", "40"))

# Appended to the PTC prompt in core-only mode. Tells the model that only the core
# tools above are listed and the rest must be discovered at runtime via the pinned
# ``search`` / ``describe`` helpers (which are themselves rendered above).
_PTC_DISCOVERY_INSTRUCTION = (
    "Only the core tools above are listed. Many more tools are available but NOT listed "
    "here (to keep this prompt stable). Discover them at runtime:\n"
    f"- `await tools.{PTC_SEARCH_TOOL_NAME}({{ query: '...' }})` — find tools by intent; "
    "returns `{ name, description }` matches.\n"
    f"- `await tools.{PTC_DESCRIBE_TOOL_NAME}({{ name: '...' }})` — get the exact "
    "signature for a tool before calling it.\n"
    f"Always `{PTC_SEARCH_TOOL_NAME}`/`{PTC_DESCRIBE_TOOL_NAME}` a tool you don't see "
    "above before calling it — calling an unknown `tools.<name>` throws."
)

# Max characters of a tool result kept inline before the code-interpreter
# middleware evicts it to a file. Override via env for tuning. Default: 20k chars.
PTC_MAX_RESULT_CHARS = int(os.getenv("PTC_MAX_RESULT_CHARS", "20000"))


# Per-invocation relay of the names exposed inside ``eval`` on the current model
# call, carrying them from ``awrap_model_call`` (where the set is computed) to
# ``aafter_model`` (the only hook that can write a checkpointed state update).
# A ContextVar keeps concurrent agent invocations isolated; it is best-effort —
# if unset on a given turn the resume path simply falls back to the full baseline.
_ptc_exposed_names_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "ptc_exposed_names", default=None
)


class _PTCExposureState(REPLState):
    """``REPLState`` extended with the checkpointed PTC exposure set.

    The field name MUST match ``PTC_EXPOSED_TOOL_NAMES_STATE_KEY``. ``PrivateStateAttr``
    keeps it out of the agent's input/output schema while still persisting it in
    the checkpoint so it survives an interrupt/resume cycle.
    """

    ptc_exposed_tool_names: NotRequired[Annotated[list[str], PrivateStateAttr]]


def _code_interpreter_ptc_enabled() -> bool:
    """Whether PTC (programmatic tool calling) is enabled for the code interpreter.

    Gated by the ``CODE_INTERPRETER_PTC`` env var (default **off** for backward
    compatibility). Set to a truthy value (``1``/``true``/``yes``/``on``) to
    enable PTC; any falsy/unset value falls back to a bare ``eval`` tool with no
    agent-tool bridge (tools are bound to the model and called directly).
    """
    return os.getenv("CODE_INTERPRETER_PTC", "0").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "",
    }


def code_interpreter_ptc_enabled() -> bool:
    """Public wrapper around :func:`_code_interpreter_ptc_enabled`.

    Used by callers outside this module (e.g. the orchestrator's GP wiring) to decide
    PTC-conditional behaviour such as gating ``ToolsetSelectorMiddleware`` off when PTC
    runtime tool discovery (``tools.search``/``tools.describe``) supersedes it.
    """
    return _code_interpreter_ptc_enabled()


class _PTCToleranceCodeInterpreterMiddleware(CodeInterpreterMiddleware):
    """``CodeInterpreterMiddleware`` that exposes *all* eligible tools via PTC.

    Two responsibilities beyond the upstream middleware:

    1. **Dict-form tool tolerance.** The orchestrator injects provider-native
       tool schemas (plain ``dict``s) into ``request.tools`` at runtime via
       ``DynamicToolDispatchMiddleware``. The upstream ``filter_tools_for_ptc``
       accesses ``tool.name`` on *every* request tool and raises
       ``AttributeError: 'dict' object has no attribute 'name'`` on those
       entries. We strip dict entries from the request used to build the PTC
       prompt while forwarding the full, unmodified request to the model handler.

    2. **Whitelisted, risk-guarded PTC exposure.** When ``broaden_exposure`` is
       enabled (sub-agent / runner graphs), every eligible tool the agent
       actually carries for the current turn is exposed inside ``eval`` as a
       *risk-guarded* wrapped instance (via ``wrap_tool_for_ptc``). Eligible
       inner tools are gathered per turn from two sources, de-duplicated by name:

       * ``_static_ptc_tools`` — build-time wrapped filesystem tools (a stable
         baseline that survives middleware ordering that hides read-only fs
         tools from later stages);
       * ``request.tools`` — the real ``BaseTool`` instances the graph carries
         (a sub-agent's whitelisted tools plus its injected filesystem and
         self-improvement tools).

       The per-user runtime ``tool_registry`` is intentionally NOT harvested:
       on the orchestrator it holds hundreds of MCP tools, which would bloat the
       PTC prompt and over-expose tools the agent never whitelisted. The
       orchestrator therefore sets ``broaden_exposure=False`` and exposes only
       the filesystem baseline (it plans and delegates via ``task``).

       Tools in ``_PTC_EXCLUDED_TOOL_NAMES`` (dispatch / response-schema) and the
       PTC self tool are never exposed. The per-call risk guard inside each
       wrapper re-applies the user's HITL/bypass rules at call time, so exposing
       sensitive tools is safe — a guarded call returns an
       ``human_approval_required`` payload that redirects the model to the
       normal (HITL-gated) tool path.

    3. **Hide PTC-exposed tools from the model.** Every tool exposed via PTC for
       a turn is stripped from the model's normal (bound) tool list in
       ``wrap_model_call`` / ``awrap_model_call`` (handling both ``BaseTool`` and
       the orchestrator's dict schemas). The model reaches them only through
       ``eval``, so the bound toolset — and the LangSmith trace — is limited to
       ``eval`` plus the never-exposed dispatch / response-schema tools.

    The shared-graph (orchestrator) instance is reused across users, so per-turn
    exposure is computed locally and only momentarily assigned to ``self._ptc``
    around the synchronous ``_prepare_for_call`` (which has no ``await`` points);
    a lock guards the rare synchronous thread-pool model path.
    """

    # Extend the upstream REPL state with the checkpointed PTC exposure set so the
    # resume path can rebuild the (filtered) ``tools.*`` namespace from the
    # checkpoint rather than the full build-time baseline.
    state_schema = _PTCExposureState  # type: ignore[assignment]

    def __init__(
        self,
        *,
        static_ptc_tools: list[BaseTool] | None = None,
        broaden_baseline_tools: list[BaseTool] | None = None,
        ptc_enabled: bool = True,
        broaden_exposure: bool = True,
        risk_scorer: RiskScorerFn | None = None,
        tool_risk_cache: ToolRiskCache | None = None,
        default_risk_threshold: float = 0.8,
        tool_server_map: dict[str, str] | None = None,
        excluded_ptc_names: frozenset[str] = _PTC_EXCLUDED_TOOL_NAMES,
        backend_supports_execution: bool = False,
        **kwargs: Any,
    ) -> None:
        # Force a fresh REPL per ``eval`` call (no cross-eval persistence).
        #
        # The PTC risk guard fires HITL ``interrupt()``s from inside ``eval``
        # (a mid-tool-node pause). On resume, LangGraph re-enters at the tool
        # node and re-runs ``eval`` so the guard can honor the approval
        # decision. The live QuickJS interpreter is process-local state that is
        # NOT part of the checkpoint (it is only snapshotted at turn end via
        # ``after_agent`` and restored at turn start via ``before_agent`` —
        # neither runs on an interrupt resume). With the default ``mode="thread"``
        # that re-run would execute against either a stale/polluted REPL (same
        # pod, mutated by the aborted probe run) or an empty one (a different,
        # stateless pod), making the re-run non-deterministic and unsound.
        #
        # ``mode="call"`` removes persistent REPL state entirely: each ``eval``
        # gets a fresh context, so the re-run depends only on checkpointed
        # inputs (the ``code`` arg + the interrupt resume decisions) and is
        # therefore deterministic and pod-independent. Callers may override by
        # passing ``mode`` explicitly.
        kwargs.setdefault("mode", "call")
        super().__init__(ptc=static_ptc_tools or None, **kwargs)
        self._ptc_enabled = ptc_enabled
        self._broaden_exposure = broaden_exposure
        self._static_ptc_tools: list[BaseTool] = list(static_ptc_tools or [])
        # Durable, build-time copy of the agent's own tool instances (a
        # sub-agent's whitelisted tools plus injected filesystem and
        # self-improvement tools). Used to re-expose the broadened PTC set on an
        # interrupt *resume*, where the model-call hook does not run and
        # ``ToolCallRequest`` carries no ``tools`` list — see
        # ``_collect_ptc_tools`` and ``awrap_tool_call``.
        self._broaden_baseline_tools: list[BaseTool] = list(broaden_baseline_tools or [])
        self._ptc_risk_scorer = risk_scorer
        self._ptc_tool_risk_cache = tool_risk_cache
        self._ptc_default_risk_threshold = default_risk_threshold
        self._ptc_tool_server_map = tool_server_map
        self._excluded_ptc_names = excluded_ptc_names
        # Whether the graph's backend can run shell commands. Gates the sandbox
        # ``execute`` tool: FilesystemMiddleware binds a dead ``execute`` even on
        # non-sandbox backends, so it must be kept out of the PTC ``eval`` namespace
        # (and stripped from the model) when execution is unsupported.
        self._supports_execution = backend_supports_execution
        self._ptc_swap_lock = threading.Lock()

    @staticmethod
    def _basetool_only(request: Any) -> Any:
        tools = list(getattr(request, "tools", []) or [])
        filtered = [t for t in tools if isinstance(t, BaseTool)]
        if len(filtered) == len(tools):
            return request
        return request.override(tools=filtered)

    def _collect_ptc_tools(self, request: Any) -> list[BaseTool]:
        """Gather and risk-wrap every eligible tool to expose via PTC this turn.

        De-duplicates by tool name (static fs baseline wins), skips excluded and
        self tool names, and skips non-``BaseTool`` (dict) entries. Dynamic tools
        are wrapped fresh each turn — the wrapper closes over the specific inner
        instance, so this avoids leaking one user's tool instance to another on
        the shared orchestrator graph.
        """
        collected: list[BaseTool] = []
        seen: set[str] = set()
        excluded = self._excluded_ptc_names | {self._tool_name}

        for tool in self._static_ptc_tools:
            if tool.name in seen:
                continue
            seen.add(tool.name)
            collected.append(tool)

        # When broadened exposure is disabled (e.g. the orchestrator), expose
        # ONLY the stable filesystem baseline via ``eval``. The orchestrator's
        # job is to plan and delegate via ``task``; harvesting its entire
        # per-user ``tool_registry`` (hundreds of MCP tools) into the PTC system
        # prompt both bloats the prompt and strips every dispatchable tool from
        # the model's bound list, derailing it into emitting a final response
        # instead of dispatching. Sub-agents keep broadened exposure so their
        # own tools remain reachable through ``eval``.
        if not self._broaden_exposure:
            return collected

        def _consider(tool: Any) -> None:
            if not isinstance(tool, BaseTool):
                return
            name = tool.name
            if name in excluded or name in seen:
                return
            # Never expose the sandbox ``execute`` tool when the backend cannot
            # run commands. ``FilesystemMiddleware`` binds a dead ``execute`` even
            # on non-sandbox backends, and it arrives here via ``request.tools``;
            # exposing it would surface a non-functional ``tools.execute`` in the
            # PTC namespace (the static baseline already gates it out the same way).
            if name in _PTC_SANDBOX_TOOLS and not self._supports_execution:
                return
            seen.add(name)
            collected.append(
                wrap_tool_for_ptc(
                    tool,
                    risk_scorer=self._ptc_risk_scorer,
                    tool_risk_cache=self._ptc_tool_risk_cache,
                    default_risk_threshold=self._ptc_default_risk_threshold,
                    tool_server_map=self._ptc_tool_server_map,
                )
            )

        # Sub-agent / runner graphs pass their concrete tool set on
        # ``request.tools`` — exactly the sub-agent's whitelisted tools plus the
        # injected filesystem and self-improvement tools (and, for the GP agent,
        # the ToolsetSelector-filtered subset). This is the authoritative set on a
        # normal model call. The per-user runtime ``tool_registry`` is never
        # harvested directly. ``had_request_tools`` distinguishes this path from an
        # interrupt *resume*, where a ``ToolCallRequest`` carries no ``tools`` list.
        had_request_tools = False
        for tool in list(getattr(request, "tools", []) or []):
            if isinstance(tool, BaseTool):
                had_request_tools = True
            _consider(tool)

        # Interrupt *resume* only: the eval tool node replays without a preceding
        # model call, so ``request.tools`` is empty. Rebuild from the agent's
        # build-time baseline, but filter it to the exposure set checkpointed by
        # ``aafter_model`` on the original call — so the REPL re-exposes exactly the
        # (filtered) ``tools.*`` namespace the original ``eval`` referenced, not the
        # full registry. When no checkpointed set is available (older checkpoint,
        # or selection never ran) the full baseline is used as a safe fallback.
        if not had_request_tools:
            selected = self._checkpointed_exposure(request)
            for tool in self._broaden_baseline_tools:
                if selected is not None and tool.name not in selected:
                    continue
                _consider(tool)

        # Large catalog (the GP agent): the prompt renders only the stable core, so
        # pin the read-only ``search``/``describe`` discovery tools into the exposed
        # set (callable + rendered) so the model can find the unrendered catalog at
        # runtime. They close over the catalog collected above. Small, fixed sub-agent
        # toolsets stay fully rendered inline and need no discovery helpers.
        if self._is_core_only(collected):
            collected.extend(build_discovery_tools(collected))

        return collected

    @staticmethod
    def _mcp_tool_count(tools: list[BaseTool]) -> int:
        """Count exposed tools carrying ``server_name`` metadata (MCP catalog tools)."""
        return sum(1 for t in tools if isinstance(t, BaseTool) and (t.metadata or {}).get("server_name"))

    def _is_core_only(self, tools: list[BaseTool]) -> bool:
        """Whether to render only the stable core (vs. every exposed tool) this turn.

        Triggered when the exposed MCP catalog exceeds
        ``PTC_INLINE_RENDER_THRESHOLD`` — i.e. the large-catalog GP agent. Sub-agents
        and the orchestrator (small / no MCP catalog) render every exposed tool inline.
        """
        return self._mcp_tool_count(tools) > PTC_INLINE_RENDER_THRESHOLD

    def _render_partition(self, exposed: list[BaseTool]) -> tuple[list[BaseTool], str]:
        """Split the exposed set into (tools_to_render, discovery_note).

        In core-only mode render only the stable, user-invariant core — base tools
        (no ``server_name``: filesystem + orchestrator static tools) plus the pinned
        ``search``/``describe`` helpers — and append the discovery instruction. The
        MCP catalog stays exposed-but-unrendered. Otherwise render everything.
        """
        if self._is_core_only(exposed):
            render_set = [t for t in exposed if not (getattr(t, "metadata", None) or {}).get("server_name")]
            return render_set, _PTC_DISCOVERY_INSTRUCTION
        return list(exposed), ""

    @staticmethod
    def _checkpointed_exposure(request: Any) -> set[str] | None:
        """Return the checkpointed PTC exposure name set, or ``None`` if absent.

        Read from the request's checkpointed state on the interrupt-resume path so
        the rebuilt ``eval`` namespace mirrors the original (filtered) call.
        """
        state = getattr(request, "state", None)
        if not isinstance(state, dict):
            return None
        names = state.get(PTC_EXPOSED_TOOL_NAMES_STATE_KEY)
        if not names:
            return None
        return set(names)

    def _prepare_for_call(self, request: Any) -> str:
        """Install the full exposed set as bridges, but render only the core.

        Overrides the upstream ``_prepare_for_call`` (which renders *every* exposed
        tool) to decouple **exposing** a tool (installing its callable
        ``globalThis.tools`` bridge) from **rendering** its signature into the prompt.
        Every tool in ``self._ptc`` (set by ``_ptc_prompt_and_hidden`` to this turn's
        exposed set) is installed so it is callable; only ``_render_partition``'s core
        subset is written into the prompt, with ``$ref``-resolved signatures via our
        own renderer (also fixing nested-arg type hints on the sub-agent inline path).
        """
        if self._ptc is None:
            return self._base_system_prompt
        exposed = [t for t in self._ptc if isinstance(t, BaseTool) and t.name != self._tool_name]
        thread_id = _resolve_thread_id(self._fallback_thread_id)
        repl = self._registry.get(thread_id)
        repl.install_tools(exposed)
        self._ptc_tools_by_thread[thread_id] = tuple(exposed)
        render_set, discovery_note = self._render_partition(exposed)
        # Cache the rendered body by the set of *rendered* names (+ a discovery marker).
        # In core-only mode this set is user-invariant and stable across turns, so the
        # returned block is identical turn-to-turn — restoring prompt caching.
        cache_key = frozenset(t.name for t in render_set) | ({"__discovery__"} if discovery_note else frozenset())
        if self._ptc_prompt_cache is None or self._ptc_prompt_cache[0] != cache_key:
            body = render_tools_namespace(render_set, tool_name=self._tool_name, discovery_note=discovery_note)
            self._ptc_prompt_cache = (cache_key, body)
        return self._base_system_prompt + self._ptc_prompt_cache[1]

    def _ptc_prompt_and_hidden(self, request: Any) -> tuple[str, set[str]]:
        """Build the PTC prompt and the set of tool names exposed this turn.

        The returned name set is exactly what is exposed inside ``eval``; those
        tools are then stripped from the model's normal (bound) tool list so the
        model reaches them only via the PTC bridge — keeping the LangSmith trace
        and the bound toolset limited to ``eval`` plus the dispatch / response-
        schema tools that are never PTC-exposed.
        """
        base_request = self._basetool_only(request)
        if not self._ptc_enabled:
            return self._prepare_for_call(base_request), set()
        exposed = self._collect_ptc_tools(request)
        exposed_names = {tool.name for tool in exposed}
        # Relay the exposed set to ``aafter_model`` so it can checkpoint it for the
        # interrupt-resume path (best-effort; same-task ContextVar).
        _ptc_exposed_names_var.set(sorted(exposed_names))
        hidden = set(exposed_names)
        # Also strip the dead sandbox ``execute`` from the model's bound tools on
        # non-sandbox agents: ``FilesystemMiddleware`` binds it, but it cannot run,
        # so it is neither PTC-exposed (above) nor left visible to the model.
        if not self._supports_execution:
            hidden |= _PTC_SANDBOX_TOOLS
        # _prepare_for_call reads self._ptc synchronously (no awaits) and installs
        # the exposed tools onto the thread-local REPL. Swap in this turn's set,
        # then restore so the shared instance never retains per-user tools.
        with self._ptc_swap_lock:
            saved = self._ptc
            self._ptc = exposed or None
            try:
                prompt = self._prepare_for_call(base_request)
            finally:
                self._ptc = saved
        return prompt, hidden

    @staticmethod
    def _tool_name_of(tool: Any) -> str | None:
        """Extract a tool's name from a ``BaseTool`` or a provider-native dict.

        Sub-agent / runner graphs carry ``BaseTool`` instances; the orchestrator
        injects OpenAI-format dict schemas (``{"function": {"name": ...}}``) via
        ``DynamicToolDispatchMiddleware``. Bare ``{"name": ...}`` dicts are also
        tolerated.
        """
        if isinstance(tool, BaseTool):
            return tool.name
        if isinstance(tool, dict):
            fn = tool.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                return fn["name"]
            return tool.get("name")
        return getattr(tool, "name", None)

    def _strip_hidden(self, request: Any, hidden: set[str]) -> Any:
        """Remove PTC-exposed tools from the model's bound tool list."""
        if not hidden:
            return request
        tools = list(getattr(request, "tools", []) or [])
        if not tools:
            return request
        kept = [t for t in tools if self._tool_name_of(t) not in hidden]
        if len(kept) == len(tools):
            return request
        return request.override(tools=kept)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        prompt, hidden = self._ptc_prompt_and_hidden(request)
        request = self._strip_hidden(request, hidden)
        return handler(
            request.override(system_message=self._extend(request.system_message, prompt)),
        )

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        prompt, hidden = self._ptc_prompt_and_hidden(request)
        request = self._strip_hidden(request, hidden)
        return await handler(
            request.override(system_message=self._extend(request.system_message, prompt)),
        )

    @staticmethod
    def _exposure_state_update() -> dict[str, Any] | None:
        """Checkpoint the PTC exposure set computed during the model call.

        Reads the names relayed by ``_ptc_prompt_and_hidden`` (set on a same-task
        ContextVar) and persists them so the interrupt-resume path can rebuild the
        same filtered ``tools.*`` namespace. Returns ``None`` (no state write) when
        PTC exposure did not run this turn.
        """
        names = _ptc_exposed_names_var.get()
        if not names:
            return None
        return {PTC_EXPOSED_TOOL_NAMES_STATE_KEY: list(names)}

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._exposure_state_update()

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._exposure_state_update()

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Drive PTC HITL approvals for the ``eval`` tool from the main graph loop.

        The risk guard inside ``eval`` cannot ``interrupt()`` (it runs on the PTC
        bridge's threadsafe-dispatched task). Instead it *records* high-risk calls
        on a per-turn collector and returns an approval payload. This hook —
        which wraps the ``eval`` tool's execution in the main Pregel loop where
        ``interrupt()`` works — drains that collector after each ``eval`` run,
        fires one batched ``interrupt()`` for the turn, stores the human
        decisions, and re-runs ``eval`` so the guard honors them.

        ``interrupt()`` replays this node from the top, so the loop below does not
        actually iterate within a single process execution: the first run raises
        ``GraphInterrupt`` at ``interrupt()``; on resume the node re-executes,
        ``interrupt()`` returns the decisions, they are applied, and the next
        ``eval`` run executes the approved calls. ``eval`` runs with
        ``mode="call"`` (fresh REPL per call) so each re-run is deterministic and
        pod-independent — see ``ptc_guard`` for the full rationale.

        Only engages for the ``eval`` tool when risk scoring is active; all other
        tool calls (and the unguarded configuration) pass straight through.
        """
        tool_call = getattr(request, "tool_call", None) or {}
        if not self._ptc_enabled or self._ptc_risk_scorer is None or tool_call.get("name") != self._tool_name:
            return await handler(request)

        runtime = getattr(request, "runtime", None)
        thread_id = resolve_ptc_thread_id(runtime)
        context = getattr(runtime, "context", None)
        # On an interrupt *resume* the graph may have been rebuilt (a fresh
        # middleware instance on a different request/pod), so the upstream
        # ``langchain_quickjs`` middleware's per-instance ``_ptc_tools_by_thread``
        # cache is empty — and the model-call hook that normally populates it does
        # NOT run on resume (only this interrupted eval tool node replays). Without
        # the cache the eval REPL installs an empty ``tools`` namespace and the
        # replayed ``code`` throws ``TypeError: ... is not a function``, derailing
        # the model with a bogus error. Repopulate the cache here (idempotent on
        # the normal path, where the model hook already filled it) so the fresh
        # per-call REPL reinstalls the same PTC bindings the original ``eval`` used.
        if self._ptc_enabled and not self._ptc_tools_by_thread.get(thread_id):
            self._ptc_tools_by_thread[thread_id] = tuple(self._collect_ptc_tools(request))
        turn = begin_ptc_turn(thread_id)
        try:
            while True:
                clear_ptc_pending(thread_id)
                result = await handler(request)
                pending = take_ptc_pending(thread_id)
                if not pending:
                    return result

                # Decisions arrive already aligned 1:1 with `pending`. The orchestrator's
                # resume path (executor._build_interrupt_resume_map) replicates the single
                # blanket UI decision to this interrupt's action_request count and keys the
                # resume by interrupt id, so each interrupt() returns exactly its own
                # decisions — no cross-eval bleed from LangGraph's multi-interrupt resume.
                # Any count mismatch here is therefore a genuine bug, not a resume artefact.
                decisions = interrupt(self._build_ptc_hitl_request(pending))["decisions"]
                if (n := len(decisions)) != (m := len(pending)):
                    msg = f"Number of PTC human decisions ({n}) does not match number of pending eval tool calls ({m})."
                    raise ValueError(msg)
                self._apply_ptc_decisions(turn, pending, decisions, context)
        finally:
            end_ptc_turn(thread_id)

    @staticmethod
    def _build_ptc_hitl_request(pending: list[Any]) -> Any:
        """Build the HITL interrupt payload for a batch of pending PTC calls.

        Mirrors ``ConditionalHumanInTheLoopMiddleware.aafter_model`` so the
        frontend renders PTC approvals identically to normal tool approvals
        (``_risk_metadata`` enrichment + per-action ``allowed_decisions``).
        ``edit`` is never offered — the approved call is re-executed verbatim
        from the re-run ``eval`` code, so there is no per-call arg to edit.
        """
        from langchain.agents.middleware.human_in_the_loop import (
            ActionRequest,
            HITLRequest,
            ReviewConfig,
        )

        action_requests: list[Any] = []
        review_configs: list[Any] = []
        for p in pending:
            enriched_args = {
                **p.args,
                # Top-level, risk-independent per-call id the client echoes so the
                # resume path aligns decisions by id (see
                # executor._build_interrupt_resume_map) instead of positionally —
                # the latter is fragile to model replay reordering. ``call_key`` is
                # deterministic on tool+args.
                "_call_id": p.call_key,
                "_risk_metadata": {
                    "source": "risk_score",
                    "score": p.score,
                    "threshold": p.threshold,
                    "matched_pattern": p.matched_pattern,
                    "server_slug": p.server_slug,
                    "tool_name": p.tool_name,
                },
            }
            description = f"Tool '{p.tool_name}' has risk score {p.score:.2f} (threshold: {p.threshold:.2f})"
            if p.matched_pattern:
                description += f" — {p.matched_pattern}"
            action_requests.append(
                ActionRequest(
                    name=p.tool_name,
                    args=enriched_args,
                    description=description,
                )
            )
            review_configs.append(
                ReviewConfig(
                    action_name=p.tool_name,
                    allowed_decisions=p.allowed_actions,
                )
            )
        return HITLRequest(action_requests=action_requests, review_configs=review_configs)

    @staticmethod
    def _apply_ptc_decisions(
        turn: Any,
        pending: list[Any],
        decisions: list[dict[str, Any]],
        context: Any,
    ) -> None:
        """Record human decisions for the re-run and apply any bypass rules.

        Re-applied on every interrupt replay (decisions arrive via the resume
        value), so it must be idempotent. Bypass rules are written into the
        per-user context so the orchestrator persists them after the turn.
        """
        from agent_common.middleware.conditional_hitl import (
            ConditionalHumanInTheLoopMiddleware,
        )

        # Match each decision to its pending call by id when the client returned
        # per-call decisions. ``Promise.all``-style eval calls register concurrently,
        # so the re-run's ``pending`` order can differ from the order the decisions
        # were collected/displayed in — a positional zip would then apply a decision
        # to the WRONG call (e.g. approve `/memories` lands on `/`). ``call_id`` equals
        # ``call_key`` (deterministic on tool+args), so by-id matching is order-
        # independent. Fall back to positional zip for legacy decisions without ids.
        by_id = {d["id"]: d for d in decisions if isinstance(d, dict) and "id" in d}
        use_by_id = bool(by_id) and all(p.call_key in by_id for p in pending)

        for i, p in enumerate(pending):
            decision = by_id[p.call_key] if use_by_id else decisions[i]
            dtype = decision.get("type")
            if dtype == "approve":
                turn.decisions[p.call_key] = "approve"
                if decision.get("bypass"):
                    ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
                        tool_name=p.tool_name,
                        server_slug=p.server_slug,
                        bypass_all=bool(decision.get("bypass_all", False)),
                        bypass_pattern=decision.get("bypass_pattern"),
                        context=context,
                    )
            else:
                # reject / edit / unknown — PTC cannot honor edit, so block.
                turn.decisions[p.call_key] = "reject"


def build_code_interpreter_middlewares(
    backend: BackendProtocol,
    *,
    broaden_exposure: bool = True,
    broaden_baseline_tools: list[BaseTool] | None = None,
    risk_scorer: RiskScorerFn | None = None,
    tool_risk_cache: ToolRiskCache | None = None,
    default_risk_threshold: float = 0.8,
    tool_server_map: dict[str, str] | None = None,
) -> list[Any]:
    """Build the code-interpreter middleware(s) for a graph.

    Returns a single ``_PTCToleranceCodeInterpreterMiddleware`` (exposing an
    ``eval`` JS REPL with a ``skills_backend``).

    When PTC (``CODE_INTERPRETER_PTC``, default off) is enabled, the eligible
    tools the agent carries for a turn are exposed inside ``eval`` as
    *risk-guarded* wrapped instances so the HITL approval workflow is preserved
    per inner call (the PTC bridge otherwise bypasses ``ToolNode``). The
    filesystem tools are wrapped here at build time to form a stable baseline (a
    fresh ``FilesystemMiddleware`` bound to *backend* is instantiated solely to
    harvest them). When ``broaden_exposure`` is enabled (sub-agent / runner
    graphs), the agent's own ``request.tools`` (its whitelisted tools plus
    injected filesystem and self-improvement tools) are also wrapped per turn by
    the middleware. The orchestrator passes ``broaden_exposure=False`` so only
    the filesystem baseline is exposed — it plans and delegates via ``task`` and
    must not pull its hundreds of registry tools into the PTC prompt. Dispatch
    and response-schema tools (``_PTC_EXCLUDED_TOOL_NAMES``) are never exposed.
    Every PTC-exposed tool is hidden from the model's normal bound tool list by
    the middleware itself, so no separate hiding middleware is needed.

    Args:
        backend: The filesystem/skills backend for this graph.
        broaden_exposure: When ``True`` (default), expose the agent's
            ``request.tools`` via ``eval`` in addition to the filesystem
            baseline. When ``False`` (orchestrator), expose only the filesystem
            baseline.
        broaden_baseline_tools: The agent's own build-time tool instances (a
            sub-agent's whitelisted tools plus injected filesystem and
            self-improvement tools). Used only when ``broaden_exposure`` is
            ``True`` to rebuild the PTC set on an interrupt *resume*, where the
            model-call hook does not run and ``request.tools`` is unavailable. On
            resume it is filtered to the exposure set checkpointed by
            ``aafter_model`` (``PTC_EXPOSED_TOOL_NAMES_STATE_KEY``) so the rebuilt
            ``tools.*`` namespace mirrors the original (filtered) call; absent a
            checkpointed set, the full baseline is used as a safe fallback.
        risk_scorer: The dynamic risk scorer (``score_tool_risk``). When
            ``None``, PTC tools run unguarded.
        tool_risk_cache: Shared risk cache fallback when the runtime context
            does not carry one.
        default_risk_threshold: Score at/above which approval is required.
        tool_server_map: Static tool-name -> server-slug map.

    Returns:
        Ordered middleware list to insert into a graph's stack.
    """
    ptc_enabled = _code_interpreter_ptc_enabled()
    static_ptc_tools: list[Any] = []
    exec_supported = False
    if ptc_enabled:
        from deepagents.middleware.filesystem import supports_execution

        # ``execute`` is only meaningful (and only safe to advertise) when the
        # backend can run commands; gate it so non-sandbox agents never expose a
        # dead ``tools.execute`` — both in the static baseline below and, via
        # ``backend_supports_execution``, in the per-turn ``request.tools`` harvest.
        exec_supported = supports_execution(backend)
        baseline_names = _PTC_READONLY_FS_TOOLS | _PTC_MUTATING_FS_TOOLS
        if exec_supported:
            baseline_names = baseline_names | _PTC_SANDBOX_TOOLS

        fs_tools = FilesystemMiddleware(backend=backend).tools
        for tool in fs_tools:
            if tool.name in baseline_names:
                static_ptc_tools.append(
                    wrap_tool_for_ptc(
                        tool,
                        risk_scorer=risk_scorer,
                        tool_risk_cache=tool_risk_cache,
                        default_risk_threshold=default_risk_threshold,
                        tool_server_map=tool_server_map,
                    )
                )

    return [
        _PTCToleranceCodeInterpreterMiddleware(
            static_ptc_tools=static_ptc_tools,
            broaden_baseline_tools=broaden_baseline_tools,
            ptc_enabled=ptc_enabled,
            broaden_exposure=broaden_exposure,
            risk_scorer=risk_scorer,
            tool_risk_cache=tool_risk_cache,
            default_risk_threshold=default_risk_threshold,
            tool_server_map=tool_server_map,
            backend_supports_execution=exec_supported,
            skills_backend=backend,
            max_result_chars=PTC_MAX_RESULT_CHARS,
        )
    ]


_DOCSTORE_HINT = (
    "\n\nNote: the full content was saved to the file above. To work with it: "
    "use `grep` for an exact/known string, `read_file` to read it directly, or "
    "`semantic_search_file` to find relevant passages within this file by meaning "
    "(it is indexed on first use). Do NOT use `docstore_search` for this — that "
    "tool only searches your durable memory notes, not this in-hand tool result."
)

# TODO: not all the conversations should have a channel configuration


class _FilesystemMiddlewareWithDocstoreHint(FilesystemMiddleware):
    """FilesystemMiddleware subclass that appends a search hint to eviction messages.

    When ``FilesystemMiddleware`` evicts an oversized tool result to
    ``/large_tool_results/``, it replaces the ``ToolMessage`` content with a
    summary and a ``read_file`` instruction.  This subclass appends an
    additional line reminding the agent how to work with the evicted blob:
    ``grep`` for exact strings, ``read_file`` to read it, or
    ``semantic_search_file`` to fuzzy-search within it (the file is indexed
    on demand by ``IndexingStoreBackend`` the first time it is searched).
    Evicted tool results are intentionally NOT eagerly indexed and are NOT
    discoverable via ``docstore_search``.

    TODO: the orchestrator's main graph does not use this subclass because it uses `deepagents.create_deep_agent()`
    which doesn't support to override the `FilesystemMiddleware` middleware. While the orchestrator
    has the same composite backend (memories, large_tool_results, channel_memories), it will be more challenging
    to leverage semantic search for evicted contents. Since it won't be dynamically instructed by the tool call result.
    """

    def _process_large_message(
        self,
        message: ToolMessage,
        resolved_backend: BackendProtocol,
    ) -> tuple[ToolMessage, dict | None]:
        processed_message, files_update = super()._process_large_message(message, resolved_backend)
        # Only append when eviction actually happened (a new ToolMessage was created)
        if processed_message is not message:
            processed_message = ToolMessage(
                content=processed_message.content + _DOCSTORE_HINT,
                tool_call_id=processed_message.tool_call_id,
                name=processed_message.name,
                id=processed_message.id,
                artifact=processed_message.artifact,
                status=processed_message.status,
                additional_kwargs=dict(processed_message.additional_kwargs),
                response_metadata=dict(processed_message.response_metadata),
            )
        return processed_message, files_update

    async def _aprocess_large_message(
        self,
        message: ToolMessage,
        resolved_backend: BackendProtocol,
    ) -> tuple[ToolMessage, dict | None]:
        processed_message, files_update = await super()._aprocess_large_message(message, resolved_backend)
        if processed_message is not message:
            processed_message = ToolMessage(
                content=processed_message.content + _DOCSTORE_HINT,
                tool_call_id=processed_message.tool_call_id,
                name=processed_message.name,
                id=processed_message.id,
                artifact=processed_message.artifact,
                status=processed_message.status,
                additional_kwargs=dict(processed_message.additional_kwargs),
                response_metadata=dict(processed_message.response_metadata),
            )
        return processed_message, files_update


def build_common_middleware_stack(
    model: BaseChatModel,
    backend: Any,
    exclude_deep_agents_middlewares: bool = False,
    add_docstore_hint: bool = False,
    hitl_guarded_tools: dict[str, dict] | None = None,
    sandbox_enabled: bool = False,
    sandbox_home: str | None = None,
    risk_scorer: RiskScorerFn | None = None,
    default_risk_threshold: float = 0.8,
    tool_risk_cache: ToolRiskCache | None = None,
    tool_server_map: dict[str, str] | None = None,
    context_gated_tools: list[ContextGatedTool] | None = None,
    broaden_baseline_tools: list[BaseTool] | None = None,
) -> list:
    """Build the common middleware stack shared by every LangGraph agent in this project.

    Creates middlewares that every agent should run beneath its
    tool-selection / dispatch layer:

    1. ``FilesystemMiddleware`` - virtual file-system backed by *backend*.
       When *backend* is a ``CompositeBackend`` with an ``IndexingStoreBackend``
       route for ``/memories/`` and ``/large_tool_results/``, written files and
       evicted tool results are automatically indexed for semantic search.
       When *add_docstore_hint* is ``True``, eviction messages are extended
       with a note that ``docstore_search`` can be used on the indexed content.
    2. ``SummarizationMiddleware`` - summarises old messages to stay within the
       model's context-window limit.  Trigger / keep values are computed from
       the model's token profile via ``compute_summarization_defaults``.
    3. ``LiteLLMPromptCachingMiddleware`` - injects an OpenAI-format
       ``cache_control`` breakpoint on the system prefix that survives the
       ChatOpenAI → LiteLLM gateway; LiteLLM translates it to the
       provider's native format and ignores it for non-caching providers.
    6. ``PatchToolCallsMiddleware`` - normalises tool-call format across
       providers (Bedrock, OpenAI, Gemini, \u2026).
    7. ``ToolRetryMiddleware`` - retries failed tool calls with exponential
       back-off (max 5 retries, factor 2.0).
    8. ``RepeatedToolCallMiddleware`` - detects and breaks tool-call loops
       (max 5 identical calls within a window of 10).
    9. ``ToolSchemaCleaningMiddleware`` - cleans tool schemas at model-binding
       time for Gemini compatibility.

    Args:
        model: The ``BaseChatModel`` instance used to compute summarization
            defaults and to pass into ``SummarizationMiddleware``.
        backend: A backend instance **or** a backend factory
            ``Callable[[Runtime], Backend]``.  Passed directly to both
            ``FilesystemMiddleware`` and ``SummarizationMiddleware``.
        exclude_deep_agents_middlewares: When ``True``, omits
            ``FilesystemMiddleware`` and ``SummarizationMiddleware``.
        add_docstore_hint: When ``True`` (and *exclude_deep_agents_middlewares*
            is ``False``), uses ``_FilesystemMiddlewareWithDocstoreHint``
            instead of plain ``FilesystemMiddleware``.  Set this to ``True``
            whenever the backend includes ``IndexingStoreBackend`` so that
            eviction messages tell the agent it can run ``docstore_search``
            on the indexed content.
        hitl_guarded_tools: Optional dict of tool names -> config for
            HumanInTheLoopMiddleware. When provided, adds HITL middleware
            that interrupts execution for approval before running these tools.

    Returns:
        Ordered list of middleware instances ready to be included in a
        ``create_agent`` / ``create_deep_agent`` call.
    """
    middleware = []
    if not exclude_deep_agents_middlewares:
        summarization_defaults = compute_summarization_defaults(model)
        fs_cls = _FilesystemMiddlewareWithDocstoreHint if add_docstore_hint else FilesystemMiddleware

        fs_middleware = fs_cls(backend=backend)

        # Configure the code interpreter. When PTC is enabled the filesystem
        # tools are exposed inside ``eval`` as risk-guarded wrapped instances and
        # the read-only ones are hidden from the model's normal tool list.
        middleware += build_code_interpreter_middlewares(
            backend,
            broaden_baseline_tools=broaden_baseline_tools,
            risk_scorer=risk_scorer,
            tool_risk_cache=tool_risk_cache,
            default_risk_threshold=default_risk_threshold,
            tool_server_map=tool_server_map,
        )
        middleware += [
            StoragePathsInstructionMiddleware(
                sandbox_enabled=sandbox_enabled,
                sandbox_home=sandbox_home,
            ),  # right before FilesytemMiddleware
            fs_middleware,
            ToolStatusMiddleware(),
            SummarizationMiddleware(
                model=model,
                backend=backend,
                trigger=summarization_defaults["trigger"],
                keep=summarization_defaults["keep"],
                trim_tokens_to_summarize=None,
                truncate_args_settings=summarization_defaults["truncate_args_settings"],
            ),
            # Inject the cache_control breakpoint as an OpenAI-format content-block
            # marker so it survives the ChatOpenAI → LiteLLM gateway and is
            # translated to the provider's native format (Bedrock cachePoint / Anthropic
            # ephemeral); LiteLLM ignores it for non-caching providers. The vendored
            # provider-specific middlewares can't fire here — the client is never a
            # ChatBedrockConverse/ChatAnthropic instance, and their model_settings path
            # is a no-op for ChatOpenAI.
            #
            # Gemini (Vertex/AI-Studio) uses *extractive* context caching: LiteLLM physically
            # MOVES cache_control-tagged messages out of the live request into a Vertex
            # CachedContent (see vertex_ai/context_caching separate_cached_messages), unlike the
            # inline Anthropic ephemeral / Bedrock cachePoint breakpoints. Tagging the last
            # conversation message there pulls the current turn into the cache — on a
            # single-message turn it empties `contents` entirely (LiteLLM: "No contents in
            # messages"). So for Gemini we cache only the static system prefix and skip the
            # incremental conversation breakpoint; every other provider keeps it.
            LiteLLMPromptCachingMiddleware(
                cache_conversation=not is_gemini_model(getattr(model, "model_name", "") or "")
            ),
        ]
        middleware += [
            PatchToolCallsMiddleware(),
            ToolRetryMiddleware(
                max_retries=5,
                backoff_factor=2.0,
                retry_on=_should_retry_tool_error,
            ),
        ]
    else:
        fs_middleware = None

    # Add HITL middleware if guarded tools are specified or risk scorer is available
    # Use ConditionalHumanInTheLoopMiddleware which supports argument-based conditions
    # (e.g., docstore_search only interrupts when include_personal=True)
    # and dynamic risk scoring (LLM-based scoring for unguarded tools)
    if hitl_guarded_tools or risk_scorer:
        from agent_common.middleware.conditional_hitl import ConditionalHumanInTheLoopMiddleware

        # Extract filesystem tool instances so the risk scorer has access to their schemas
        platform_tools: dict[str, Any] | None = None
        if fs_middleware is not None:
            platform_tools = {t.name: t for t in fs_middleware.tools}

        middleware.append(
            ConditionalHumanInTheLoopMiddleware(
                interrupt_on=hitl_guarded_tools or {},
                risk_scorer=risk_scorer,
                default_risk_threshold=default_risk_threshold,
                tool_risk_cache=tool_risk_cache,
                tool_server_map=tool_server_map,
                platform_tools=platform_tools,
            )
        )

    # Conversation-context tool gate: additively injects tools (e.g.
    # read_personal_file) only in the conversation contexts where they apply.
    if context_gated_tools:
        middleware.append(ConversationContextToolsMiddleware(context_gated_tools))

    middleware += [
        RepeatedToolCallMiddleware(
            max_repeats=5,
            max_tool_repeats=10,
            window_size=10,
            # ``task`` (sub-agent dispatch) and ``eval`` (the PTC code interpreter)
            # are meta/gateway tools legitimately called many times with *different*
            # arguments — distinct delegations / distinct code. They must be exempt
            # from the per-tool-name ``max_tool_repeats`` cap (otherwise a normal
            # multi-step PTC agent gets blocked mid-task and force-stopped, ending
            # with no structured response). They remain subject to ``max_repeats``
            # (identical-args) detection, which still catches true loops.
            dispatch_tools={"task", PTC_CODE_INTERPRETER_TOOL_NAME},
        ),
        ToolSchemaCleaningMiddleware(),
    ]
    return middleware


def create_indexing_backend_factory(
    store: AsyncPostgresStore | None,
    model_name: str | None = None,
    cost_logger: Optional[CostLogger] = None,
    resolved_skills: dict[str, ResolvedSkill] | None = None,
    include_attachments: bool = False,
) -> BackendProtocol:
    """Return a backend instance for FilesystemMiddleware.

    When a document store is available, returns a ``CompositeBackend`` that
    routes ``/memories/`` and ``/large_tool_results/`` writes through
    ``IndexingStoreBackend`` for automatic semantic indexing, with everything
    else falling back to ephemeral ``StateBackend``.

    ``/large_tool_results/`` is the path used by ``FilesystemMiddleware``
    when it evicts oversized tool results.  Routing it through
    ``IndexingStoreBackend`` ensures evicted content is indexed for
    semantic search and persisted across turns, not lost in ephemeral
    ``StateBackend``.

    When no store is configured, returns a simple ``StateBackend``
    (ephemeral, in-agent-state storage).

    Args:
        store: Initialised ``AsyncPostgresStore`` instance, or ``None`` when
            the document store is not configured.
        model_name: Model to use for chunking/contextualization in the indexing
            pipeline.  When ``None``, ``get_default_indexing_model()`` selects
            the cheapest available provider model automatically.
        cost_logger: Optional ``CostLogger`` for reporting LLM usage costs
            incurred by the indexing pipeline (contextualisation calls).
        resolved_skills: Pre-resolved skills dict to mount at ``/skills/``
            as a read-only backend.  When ``None``, no skills route is added.
        include_attachments: When ``True``, mounts a stateless
            ``ContextScopedAttachmentsBackend`` proxy at ``/attachments/`` so
            the shared/cached graph can read the per-turn attachments registered
            via ``set_current_attachments_backend``.  Used by the orchestrator,
            whose graph is shared across users and cannot bake in per-turn
            attachment content.

    Returns:
        A ``BackendProtocol`` instance suitable for passing to
        ``FilesystemMiddleware``, ``SummarizationMiddleware``,
        ``build_common_middleware_stack``, or ``create_deep_agent``.
    """
    if store is not None:
        # Create IndexingStoreBackend instances with explicit path-based routing:
        # 1. /memories/ → user-scoped (user_id, "filesystem") - personal files
        # 2. /large_tool_results/ → conversation-scoped (conversation_id, "filesystem") - tool results
        # 3. /channel_memories/ → channel-scoped (assistant_id, "filesystem") - shared channel files
        # 4. /group_memories/ → group-scoped (group_id, "filesystem") - shared group files
        #
        # Application logic decides which path to use:
        # - write_file("/memories/foo") → personal, user-scoped
        # - write_file("/channel_memories/foo") → shared, channel-scoped
        # - write_file("/group_memories/foo") → shared, group-scoped
        # - Tool results always go to /large_tool_results/ (conversation-scoped)
        #
        # Personal files: user-scoped namespace
        user_documents_backend = IndexingStoreBackend(
            store=store,
            model_name=model_name,
            cost_logger=cost_logger,
            namespace_factory=lambda ctx: _user_scoped_namespace(ctx),
        )

        # Tool results: conversation-scoped namespace for isolation
        tool_results_backend = IndexingStoreBackend(
            store=store,
            model_name=model_name,
            cost_logger=cost_logger,
            namespace_factory=lambda ctx: _conversation_scoped_namespace(ctx),
        )

        # Channel files: channel-scoped namespace for shared access
        channel_documents_backend = IndexingStoreBackend(
            store=store,
            model_name=model_name,
            cost_logger=cost_logger,
            namespace_factory=lambda ctx: _channel_scoped_namespace(ctx),
        )

        # Group files: group-scoped namespace for shared group files
        group_documents_backend = IndexingStoreBackend(
            store=store,
            model_name=model_name,
            cost_logger=cost_logger,
            namespace_factory=lambda ctx: _group_scoped_namespace(ctx),
        )

        routes: dict[str, BackendProtocol] = {
            "/memories/": user_documents_backend,
            "/large_tool_results/": tool_results_backend,
            "/channel_memories/": channel_documents_backend,
            "/group_memories/": group_documents_backend,
        }
        if resolved_skills:
            routes["/skills/"] = SkillsStoreBackend(resolved_skills)

        if include_attachments:
            routes["/attachments/"] = ContextScopedAttachmentsBackend()

        return CompositeBackend(
            default=StateBackend(),
            routes=routes,
        )
    else:
        return StateBackend()


def create_sandboxed_backend_factory(
    sandbox_backend: "SandboxBackendProtocol",
    base_backend: Any,
) -> CompositeBackend:
    """Wrap a base backend to use a sandbox as the default backend.

    The sandbox_backend is a SandboxBackendProtocol instance (e.g., GatanaSandbox)
    that already implements the full BackendProtocol (read, write, edit, execute,
    grep, glob, ls). It's used directly as the default backend — no adapter needed.

    The sandbox is wrapped with ReadySandboxWrapper which detects "not ready"
    responses and raises SandboxNotReadyError — allowing ToolRetryMiddleware
    to automatically retry with exponential backoff.

    Preserves all existing routes (/skills/, /memories/, etc.) from the base backend.

    Args:
        sandbox_backend: A SandboxBackendProtocol instance (from SandboxPool.acquire)
        base_backend: The non-sandboxed backend instance

    Returns:
        A CompositeBackend with sandbox as default
    """
    from agent_common.core.sandbox_ready_wrapper import ReadySandboxWrapper

    wrapped = ReadySandboxWrapper(sandbox_backend)
    if isinstance(base_backend, CompositeBackend):
        return CompositeBackend(default=wrapped, routes=base_backend.routes)
    # If base is a simple backend, use sandbox as default with no routes
    return CompositeBackend(default=wrapped, routes={})


def _get_metadata() -> dict:
    """Get metadata from the current RunnableConfig.

    Uses ``langgraph.config.get_config()`` which reads the config from
    contextvars — works in both node and tool execution contexts.
    """
    try:
        cfg = get_config()
    except RuntimeError:
        return {}
    return cfg.get("metadata", {})


def _conversation_scoped_namespace(ctx: Any) -> tuple[str, ...]:
    """Namespace factory for conversation-scoped files (tool results).

    Returns (conversation_id, "filesystem") to isolate files per conversation.
    Returns impossible-to-match sentinel if conversation_id missing (for graceful grep failure).
    """
    metadata = _get_metadata()
    conversation_id = metadata.get("conversation_id")

    if not conversation_id:
        # Return sentinel namespace that won't match any real data
        # This allows grep to continue without failing, while preventing wrong-namespace access
        logger.warning("[NAMESPACE] conversation_id missing, using sentinel namespace")
        return ("__missing_conversation_id__", "filesystem")

    logger.info(f"[NAMESPACE] conversation-scoped: ({conversation_id}, 'filesystem')")
    return (conversation_id, "filesystem")


def _user_scoped_namespace(ctx: Any) -> tuple[str, ...]:
    """Namespace factory for user-scoped files (personal documents).

    Returns (user_id, "filesystem") to persist files across conversations.
    Returns impossible-to-match sentinel if user_id missing (for graceful grep failure).
    """
    metadata = _get_metadata()
    user_id = metadata.get("user_id")

    if not user_id:
        # Return sentinel namespace that won't match any real data
        # This allows grep to continue without failing, while preventing wrong-namespace access
        logger.warning("[NAMESPACE] user_id missing, using sentinel namespace")
        return ("__missing_user_id__", "filesystem")

    logger.info(f"[NAMESPACE] user-scoped: ({user_id}, 'filesystem')")
    return (user_id, "filesystem")


def _channel_scoped_namespace(ctx: Any) -> tuple[str, ...]:
    """Namespace factory for channel-scoped files (shared documents).

    Returns (assistant_id, "filesystem") for files shared in Slack channels.
    All users in the same channel see the same files.
    Returns impossible-to-match sentinel if assistant_id missing (for graceful grep failure).
    """
    metadata = _get_metadata()
    assistant_id = metadata.get("assistant_id")

    if not assistant_id:
        # Return sentinel namespace that won't match any real data
        # This allows grep to continue without failing, while preventing wrong-namespace access
        logger.warning("[NAMESPACE] assistant_id missing, using sentinel namespace")
        return ("__missing_assistant_id__", "filesystem")

    logger.info(f"[NAMESPACE] channel-scoped: ({assistant_id}, 'filesystem')")
    return (assistant_id, "filesystem")


def _group_scoped_namespace(ctx: Any) -> tuple[str, ...]:
    """Namespace factory for group-scoped files (shared group playbooks).

    Returns (group_id, "filesystem") for files shared within a user group.
    Validates that group_id is in the user's group_ids membership list.
    Returns impossible-to-match sentinel if group_id missing or not authorized.
    """
    metadata = _get_metadata()
    group_id = metadata.get("group_id")

    if not group_id:
        logger.warning("[NAMESPACE] group_id missing, using sentinel namespace")
        return ("__missing_group_id__", "filesystem")

    # Validate group_id against the user's verified memberships
    group_ids = metadata.get("group_ids") or []
    if group_ids and str(group_id) not in [str(g) for g in group_ids]:
        logger.warning(f"[NAMESPACE] group_id {group_id} not in user's group_ids, denied")
        return ("__unauthorized_group_id__", "filesystem")

    logger.info(f"[NAMESPACE] group-scoped: ({group_id}, 'filesystem')")
    return (str(group_id), "filesystem")


def build_sub_agent_graph(
    model: BaseChatModel,
    tools: list,
    system_prompt: str,
    checkpointer: BaseCheckpointSaver | None,
    store: AsyncPostgresStore | None = None,
    model_name: str | None = None,
    cost_logger: Optional[CostLogger] = None,
    response_format: Any = None,
    exclude_deep_agents_middlewares: bool = False,
    backend_factory: Optional[Any] = None,
    hitl_guarded_tools: dict[str, dict] | None = None,
    extra_middlewares: list[AgentMiddleware] | None = None,
    extra_tools: list | None = None,
    sandbox_enabled: bool = False,
    sandbox_home: str | None = None,
    risk_scorer: RiskScorerFn | None = None,
    default_risk_threshold: float = 0.8,
    tool_risk_cache: ToolRiskCache | None = None,
    tool_server_map: dict[str, str] | None = None,
    context_gated_tools: list[ContextGatedTool] | None = None,
    **kwargs: Any,
) -> CompiledStateGraph:
    """Build a standard deep-agent LangGraph graph.

    Combines three steps that every non-orchestrator agent repeats:

    1. **Backend** — ``create_indexing_backend_factory(store, ...)`` returns
       the appropriate backend instance (``CompositeBackend`` with indexing
       or plain ``StateBackend``), unless *backend_factory* is provided
       directly (e.g. by ``DynamicLocalAgentRunnable`` which may receive a
       pre-built backend from the orchestrator).
    2. **Middleware stack** — ``build_common_middleware_stack(model, backend,
       exclude_deep_agents_middlewares)`` assembles the standard middlewares.
    3. **Graph** — ``create_agent(...)`` wires everything together.

    This helper is intentionally *not* used by the orchestrator's main graph,
    which has a custom middleware ordering
    (``ToolsetSelectorMiddleware`` → ``DynamicToolDispatchMiddleware`` → …)
    and a ``context_schema`` that cannot be expressed generically here.

    Args:
        model: ``BaseChatModel`` instance (Bedrock, OpenAI, Gemini, …).
        tools: List of tools available to the agent.
        system_prompt: System prompt string.
        checkpointer: LangGraph checkpoint saver (PostgreSQL, memory, …).
        store: Optional initialised ``AsyncPostgresStore`` for persistent
            memory / document search.
        model_name: Model to use for indexing/chunking in the document store
            pipeline.  When ``None``, ``get_default_indexing_model()`` picks
            the cheapest available provider model automatically.
        response_format: Pre-computed structured-output strategy
            (``AutoStrategy``, ``ToolStrategy``, ``None``, …).  Pass the
            result of ``get_response_format()`` here.
        exclude_deep_agents_middlewares: When ``True``, omits
            ``FilesystemMiddleware`` and ``SummarizationMiddleware`` from the
            middleware stack (intended for ``agent-runner`` which manages its
            own file-system lifecycle).
        backend_factory: Optional pre-built backend instance
            (``BackendProtocol``).  When provided it is used directly
            instead of calling ``create_indexing_backend_factory``.
            Useful when the caller (e.g. ``DynamicLocalAgentRunnable``) has
            already received an injected backend from the orchestrator.
        **kwargs: Extra keyword arguments forwarded verbatim to
            ``create_agent`` (e.g. ``context_schema``,
            ``recursion_limit``).

    Returns:
        A compiled ``CompiledStateGraph`` ready for ``astream_events``.
    """
    backend = (
        backend_factory
        if backend_factory is not None
        else create_indexing_backend_factory(store, model_name=model_name, cost_logger=cost_logger)
    )
    all_tools = list(tools) + list(extra_tools or [])
    middleware = build_common_middleware_stack(
        model,
        backend,
        exclude_deep_agents_middlewares,
        add_docstore_hint=store is not None or backend_factory is not None,
        hitl_guarded_tools=hitl_guarded_tools,
        sandbox_enabled=sandbox_enabled,
        sandbox_home=sandbox_home,
        risk_scorer=risk_scorer,
        default_risk_threshold=default_risk_threshold,
        tool_risk_cache=tool_risk_cache,
        tool_server_map=tool_server_map,
        context_gated_tools=context_gated_tools,
        broaden_baseline_tools=all_tools,
    )
    if extra_middlewares:
        middleware = list(extra_middlewares) + middleware
    return create_agent(
        model,
        system_prompt=system_prompt,
        tools=all_tools,
        checkpointer=checkpointer,
        store=store,
        middleware=middleware,
        response_format=response_format,
        **kwargs,
    )
