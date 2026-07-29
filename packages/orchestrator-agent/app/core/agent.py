"""Meta-Agent which can be instantiated with personalized configuration
for different users, enabling tailored interactions and responses.

* get_config: Retrieves and applies user-specific configuration settings to customize agent behavior.
* discover_sub_agents: Discovers and integrates sub-agents dynamically based on the user permissions.

Architecture:
- ONE universal graph per model type (not per capability set)
- User context (language, preferences) injected at runtime via `context` parameter
- Tools and sub-agents injected per-user via GraphRuntimeContext.tool_registry and subagent_registry
- DynamicToolDispatchMiddleware handles runtime tool binding and dispatch
- Dynamic system prompt personalizes responses based on GraphRuntimeContext
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Any, Optional

import httpx

if TYPE_CHECKING:
    from agent_common.core.sandbox_pool import SandboxPool

from a2a.types import Part, TaskState
from agent_common.backends.attachments_store import (
    build_attachments_backend_from_blocks,
    collect_attachment_blocks_from_messages,
    reset_current_attachments_backend,
    set_current_attachments_backend,
)
from agent_common.core.stream_watchdog import StreamStallError, watch_stream_with_resume
from agent_common.middleware.ptc_guard import PTC_CODE_INTERPRETER_TOOL_NAME
from agent_common.middleware.tool_status import TOOL_STATUS_EVENT
from agent_common.models.base import DEFAULT_THINKING_LEVEL, ModelType, ThinkingLevel
from langchain.messages import HumanMessage
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from object_storage import get_object_storage_service
from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.utils.streaming import (
    StreamBuffer,
    StructuredResponseStreamer,
    extract_text_from_content,
)

from ..core.graph_factory import GraphFactory
from ..handlers import StreamHandler
from ..models import AgentFrameworkAuthError, AgentStreamResponse
from ..models.config import AgentSettings, GraphRuntimeContext, UserConfig
from ..utils import build_runtime_context
from .content_builder import build_text_content
from .discovery import AgentDiscoveryService, ToolDiscoveryService
from .turn_state import TurnState

logger = logging.getLogger(__name__)


# Tool names that must NOT surface as a bare "Using {tool}…" activity-log entry
# on the orchestrator's own stream:
#   - response schemas: not real tool calls;
#   - ``task``: the dispatch middleware emits "Delegating to {subagent}…" instead;
#   - ``write_todos``: rendered as a work-plan, not an activity line;
#   - ``eval`` (the PTC code interpreter): excluded *here*, on the messages path,
#     because sub-agents run their tools *inside* ``eval`` and those sub-agent
#     ``eval`` tool-call chunks leak into the orchestrator's ``messages`` stream
#     via inherited streaming callbacks — arriving with the orchestrator's own
#     ``thread_id`` and an empty ``ns``, so the thread-id guard below cannot
#     distinguish them. Surfacing them here would render an unattributed, partial
#     "Using eval…". The orchestrator's *own* eval is instead surfaced from the
#     richer ``tool_status`` custom-event channel (see the TOOL_STATUS_EVENT
#     handler below), which fires only on the orchestrator's own stream and
#     carries a descriptive "Running …" message — mirroring how a sub-agent's
#     eval surfaces via its generic tool_status→ActivityLog forwarding.
_ACTIVITY_LOG_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {
        "FinalResponseSchema",
        "SubAgentResponseSchema",
        "task",
        "write_todos",
        PTC_CODE_INTERPRETER_TOOL_NAME,
    }
)


def _extract_conversation_origin(message_parts: list[Part]) -> dict[str, Any] | None:
    """Extract a conversation-origin descriptor from an incoming DataPart, if present.

    Implements the request side of CONVERSATION_ORIGIN_EXTENSION (see
    app.core.a2a_extensions for the contract): clients describe prior work a
    fresh conversation is about — a delivered scheduled-run notification, and
    other kinds over time — as a DataPart ``{"origin": {"kind": ..., ...}}``.
    The descriptor carries data, never conversation state: any contextId in it
    is provenance about another agent's conversation, not this one's.
    """
    from google.protobuf.json_format import MessageToDict

    for part in message_parts:
        if part.WhichOneof("content") == "data":
            data = MessageToDict(part.data)
            if isinstance(data, dict) and isinstance(data.get("origin"), dict):
                return data["origin"]
    return None


def _scheduled_run_frame_text(text: str) -> str:
    """Neutralize a closing tag inside text interpolated into the <scheduled_run> frame.

    Run prompts/outputs routinely contain untrusted content; a literal
    ``</scheduled_run>`` (in any casing — models treat XML-ish tags
    case-insensitively) would escape the frame and read as first-person user
    input on the conversation's first turn.
    """
    return re.sub(r"(?i)</(scheduled_run)", r"<\\/\1", text)


def _origin_int(value: Any) -> int | None:
    """Coerce an origin id field to int (MessageToDict renders protobuf numbers
    as floats). Single helper for every reading of the same DataPart, so
    rendering and adoption can't disagree on what counts as a valid id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _build_scheduled_run_history(
    run: dict[str, Any], *, delegation_label: str | None = None
) -> list[Any] | None:
    """Origin builder for kind ``scheduled_run`` (CONVERSATION_ORIGIN_EXTENSION).

    The scheduler dispatched the run directly to agent-runner, so this fresh
    orchestrator conversation has no record of it. Reconstruct the turn the
    orchestrator *would* have produced had it dispatched the run itself: the
    job prompt as the human request (first non-system message must be a human
    one — some providers reject a leading assistant tool-call), the ``task``
    tool call to the sub-agent, and the run's output as the tool result. This
    gives the model the run's prompt and output as real context; it does NOT
    by itself resume the run's checkpoint. Conversation adoption is seeded
    separately (_validate_scheduled_run_origin + _build_adoption_seed): a
    follow-up delegation resumes the run's conversation on the executing
    server for remote sub-agents, or on a fork of the run's checkpoint for
    local/automated ones.

    For a run without a sub-agent (a plain watch notification) there is no
    delegation to reconstruct — only the framing HumanMessage carrying the
    delivered notification text is returned, so the model still knows what
    the user is replying to.

    ``delegation_label`` is the DISPATCHABLE registry key of the run's
    sub-agent, when the caller resolved one (adoption): the synthetic tool
    call must show the label the model can actually re-use, which for remote
    agents (card name, spaces stripped) may differ from the console config
    name the provenance carries. Without it, the provenance name is the best
    available approximation (local/foundry registries are keyed by the
    unmodified config name).

    The provenance may carry the run's terminal ``task_state``. It matters for
    the framing: a run that ended ``input_required`` did not finish — the
    sub-agent asked the user a question and its conversation is waiting for
    the answer, so the reconstruction must steer the model toward forwarding
    the user's reply to the sub-agent instead of answering on its behalf
    (delivered as TASK_STATE_COMPLETED framing, the orchestrator role-played
    the sub-agent's side of an unfinished exchange — observed live: it
    invented its own "secret number" via eval rather than delegating).

    When adoption resolved a ``delegation_label``, the framing also states
    that delegating RESUMES the run's conversation with the sub-agent's
    memory intact — without it the model falls back on its "sub-agents are
    stateless" prior and reconstructs (i.e. fabricates) run-internal state
    itself.

    Returns None when the provenance carries nothing to reconstruct.
    """
    sub_agent_name = run.get("sub_agent_name") or ""
    prompt = run.get("prompt")
    result_summary = run.get("result_summary")
    failed = run.get("scheduler_status") == "failed"
    error_message = run.get("error_message")
    # Untrusted enrichment like prompt/result_summary; anything but the known
    # scheduler-facing states is ignored (forward compatibility).
    task_state = run.get("task_state")
    if task_state not in ("completed", "input_required", "failed"):
        task_state = None
    awaiting_input = task_state == "input_required" and not failed

    def _fmt_id(value: Any) -> str:
        parsed = _origin_int(value)
        return str(parsed) if parsed is not None else ""

    job_id = _fmt_id(run.get("scheduled_job_id"))
    run_id = _fmt_id(run.get("scheduled_job_run_id"))
    frame_attrs = f'source="scheduler" job_id="{job_id}" run_id="{run_id}"'
    if failed:
        frame_attrs += ' status="failed"'
    elif task_state:
        frame_attrs += f' task_state="{task_state}"'

    if not sub_agent_name:
        # Watch notification without a sub-agent: no delegation happened; give
        # the model the delivered notification as context.
        if not result_summary:
            return None
        return [
            HumanMessage(
                content=(
                    f"<scheduled_run {frame_attrs}>"
                    f"{_scheduled_run_frame_text(result_summary)}"
                    "</scheduled_run>\n"
                    "The message above was produced by a scheduled watch and delivered to the user; "
                    "they are now replying to it."
                )
            )
        ]

    prompt = prompt or "Execute your configured task."
    if failed:
        tool_content = f"The scheduled run FAILED: {error_message or 'unknown error'}"
        if result_summary:
            # For failed runs, result_summary is the user-facing failure
            # notification text, not partial task output.
            tool_content += f"\nNotification delivered to the user: {result_summary}"
    else:
        tool_content = result_summary or "(the output was delivered to the user)"

    tool_call_id = f"scheduled_run_{run_id or run.get('context_id') or 'unknown'}"

    if awaiting_input and delegation_label:
        # Forwarding is only honest advice when adoption validated: without
        # the a2a_tracking seed a delegation starts the sub-agent blank, and
        # the model would forward the answer into a void.
        framing = (
            "The request above ran on a schedule but did NOT finish: the sub-agent's delivered "
            "output asks the user a question and the run is waiting for their answer. The user "
            "is replying to that question — forward their reply to the sub-agent via the task "
            f"tool ({delegation_label!r}); do not answer the question or continue the exchange on "
            "the sub-agent's behalf."
        )
    elif awaiting_input:
        framing = (
            "The request above ran on a schedule but did NOT finish: the sub-agent's delivered "
            "output asks the user a question. Its conversation could NOT be resumed from here, "
            "so the sub-agent has no memory of asking — handle the user's reply yourself, using "
            "the delivered output above as the exchange so far, and be upfront about anything "
            "the run kept internal (it is unrecoverable)."
        )
    else:
        framing = (
            "The request above ran on a schedule and its output was already delivered to the user, "
            "who is now replying to it. When the reply needs content from that output, restate it "
            "explicitly in your answer — do not reference it via include_subagent_output (this "
            "restriction covers only the already-delivered output above; relay the result of any "
            "NEW delegation faithfully, as usual)."
        )
    if delegation_label:
        # Only rendered when adoption validated (see docstring): delegating
        # really does resume the run's conversation, so tell the model —
        # otherwise its "sub-agents are stateless" prior wins and it
        # reconstructs run-internal state itself.
        framing += (
            f"\nDelegating to {delegation_label!r} RESUMES the run's own conversation: the "
            "sub-agent retains the run's full memory, including internal state not shown here "
            "(values it computed, files it wrote, decisions it made). For any follow-up that "
            "depends on that state, delegate to it — never reconstruct, recompute, or simulate "
            "that state yourself."
        )

    human_msg = HumanMessage(
        content=(
            f"<scheduled_run {frame_attrs}>"
            f"{_scheduled_run_frame_text(prompt)}"
            "</scheduled_run>\n"
            f"{framing}"
        )
    )
    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": tool_call_id,
                "name": "task",
                "type": "tool_call",
                "args": {"subagent_type": delegation_label or sub_agent_name, "description": prompt},
            }
        ],
    )
    if failed:
        synthetic_state = "TASK_STATE_FAILED"
    elif awaiting_input:
        synthetic_state = "TASK_STATE_INPUT_REQUIRED"
    else:
        synthetic_state = "TASK_STATE_COMPLETED"
    tool_msg = ToolMessage(
        content=tool_content,
        tool_call_id=tool_call_id,
        additional_kwargs={
            "a2a_metadata": {
                # Message-level rendering only — the a2a_tracking continuity
                # state is seeded separately (_build_adoption_seed) and always
                # carries is_complete=True (there is no resumable task_id).
                "is_complete": not awaiting_input,
                "state": synthetic_state,
                "agent_name": sub_agent_name,
            }
        },
    )
    return [human_msg, ai_msg, tool_msg]


# Origin-kind registry for CONVERSATION_ORIGIN_EXTENSION: each builder turns a
# validated origin descriptor into synthetic history for a fresh conversation.
# A builder may return None when the descriptor carries nothing to reconstruct.
_ORIGIN_HISTORY_BUILDERS: dict[str, Any] = {
    "scheduled_run": _build_scheduled_run_history,
}


def _build_origin_history(
    origin: dict[str, Any], *, delegation_label: str | None = None
) -> list[Any] | None:
    """Dispatch an origin descriptor to its kind's history builder.

    ``delegation_label`` is the resolved dispatchable label of the origin's
    sub-agent when adoption validated one (see _build_adoption_seed); builders
    render it in the synthetic delegation so the model re-uses a label that
    actually dispatches.

    Unknown kinds are skipped with a log line rather than an error — the
    descriptor is an optional context enrichment, and a newer client must be
    able to talk to an older orchestrator.
    """
    kind = origin.get("kind")
    builder = _ORIGIN_HISTORY_BUILDERS.get(kind) if isinstance(kind, str) else None
    if builder is None:
        logger.info(f"Ignoring conversation origin of unknown kind {kind!r}")
        return None
    try:
        return builder(origin, delegation_label=delegation_label)
    except Exception:
        # A malformed descriptor must degrade like an unknown kind — the
        # origin is optional enrichment; never fail the turn over it.
        logger.warning(f"Failed to build history for conversation origin kind {kind!r}; skipping", exc_info=True)
        return None


# Overall deadline for the console-backend ownership lookups behind
# conversation adoption. Adoption is an enrichment on the conversation's first
# turn only; a slow backend must degrade to "no adoption", not stall the turn
# — asyncio.timeout enforces this across BOTH lookups combined.
_ADOPTION_LOOKUP_TIMEOUT_S = 3.0


async def _validate_scheduled_run_origin(
    origin: dict[str, Any],
    access_token: str,
    console_backend_url: str,
) -> dict[str, Any] | None:
    """Validate a scheduled_run origin server-side and resolve its run conversation.

    Cross-service conversation adoption, step 1 of 2 (see _build_adoption_seed
    for step 2 and the delegation-path mechanics). The client-forwarded
    DataPart is treated as an untrusted hint: the job and run are re-resolved
    from console-backend under the AUTHENTICATED user's token (a job another
    user owns simply 404s), the sub-agent binding is checked server-side, and
    the SERVER-stored conversation_id is what adoption uses — the forwarded
    ``context_id`` plays no part.

    Expected failures (backend down/slow, non-2xx, malformed body) degrade to
    None with a warning; programming errors propagate to the caller so they
    stay visible rather than masquerading as pre-adoption behavior.

    Returns ``{"sub_agent_id", "conversation_id", "job_id", "run_id"}`` or
    None when the origin does not resolve to an owned run with a stored
    conversation.
    """
    if origin.get("kind") != "scheduled_run":
        return None

    job_id = _origin_int(origin.get("scheduled_job_id"))
    run_id = _origin_int(origin.get("scheduled_job_run_id"))
    sub_agent_id = _origin_int(origin.get("sub_agent_id"))
    if job_id is None or run_id is None or sub_agent_id is None:
        return None

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with asyncio.timeout(_ADOPTION_LOOKUP_TIMEOUT_S):
            async with httpx.AsyncClient(base_url=console_backend_url) as client:
                # The binding check post-filters, so both lookups can run
                # concurrently under the shared deadline. return_exceptions
                # keeps a second in-flight failure from becoming an
                # "exception was never retrieved" warning; re-raise the first
                # so the except below handles both lookups uniformly.
                job_resp, run_resp = await asyncio.gather(
                    client.get(f"/api/v1/scheduler/jobs/{job_id}", headers=headers),
                    client.get(f"/api/v1/scheduler/jobs/{job_id}/runs/{run_id}", headers=headers),
                    return_exceptions=True,
                )
                for resp in (job_resp, run_resp):
                    if isinstance(resp, BaseException):
                        raise resp
        if job_resp.status_code != 200:
            logger.info(
                f"Conversation adoption skipped: job {job_id} not resolvable for this user "
                f"(HTTP {job_resp.status_code})"
            )
            return None
        if _origin_int(job_resp.json().get("sub_agent_id")) != sub_agent_id:
            logger.warning(
                f"Conversation adoption skipped: origin sub_agent_id {sub_agent_id} does not match "
                f"job {job_id}'s server-side sub-agent binding"
            )
            return None
        if run_resp.status_code != 200:
            logger.info(
                f"Conversation adoption skipped: run {run_id} not resolvable on job {job_id} "
                f"(HTTP {run_resp.status_code})"
            )
            return None
        run = run_resp.json()
    except (httpx.HTTPError, TimeoutError, ValueError):
        # ValueError also covers .json() on a non-JSON 200 (e.g. an auth proxy
        # interposing an HTML page).
        logger.warning("Conversation adoption lookup failed; continuing without it", exc_info=True)
        return None
    conversation_id = run.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None

    return {
        "sub_agent_id": sub_agent_id,
        "conversation_id": conversation_id,
        "job_id": job_id,
        "run_id": run_id,
    }


def _build_adoption_seed(
    validated: dict[str, Any],
    subagent_registry: dict[str, Any],
) -> tuple[str, str, dict[str, Any]] | None:
    """Build the a2a_tracking adoption record for a server-validated run.

    Cross-service conversation adoption, step 2 of 2. One contract, two
    continuity mechanisms — the record seeded under the runnable's
    tracking_key tells the next delegation to that sub-agent to continue the
    run's conversation instead of starting blank:

    - REMOTE agents: ``{"context_id": <run conversation_id>}``. agent-runner
      dispatched the run with the run task's contextId on the wire, so the
      remote server checkpoints the run's conversation under exactly this id;
      the A2AClientRunnable waterfall puts a seeded context_id back on the
      wire unchanged.
    - LOCAL/AUTOMATED agents: ``{"adopt_thread_from": <run conversation_id>}``.
      The run's checkpoint lives at bare ``thread_id = conversation_id`` in
      the SHARED checkpoint tables (both services point at the same
      Postgres schema); DynamicToolDispatchMiddleware forks it into the
      conversation's own ``{ctx}::dynamic-{name}`` thread on first
      delegation. The run ctx must NEVER be seeded as ``context_id`` for a
      local runnable — that changes its execution thread while the HITL
      checkpoint probe still probes the conversation-derived thread,
      silently breaking tool-approval resume (PR #161 round 1).
    - Foundry: not adoptable — continuity is a session rid the provenance
      does not carry.

    Returns ``(registry_key, tracking_key, record)`` or None. registry_key is
    the ``task`` tool label (raw config name for local agents, card name with
    spaces stripped for remote); tracking_key is where the record lives in
    a2a_tracking (identical to registry_key for local agents, whose names
    cannot contain spaces).
    """
    from agent_common.a2a.client_runnable import A2AClientRunnable
    from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable

    sub_agent_id = validated["sub_agent_id"]
    conversation_id = validated["conversation_id"]

    for registry_key, entry in subagent_registry.items():
        if entry.get("sub_agent_id") != sub_agent_id:
            continue
        runnable = entry.get("runnable")
        # sub_agent_id makes the record self-describing: later turns (and HITL
        # resumes) re-derive adopted_sub_agent_ids from the persisted tracking
        # state, keeping the adopted agent registered for the whole
        # conversation (_adopted_sub_agent_ids_from_tracking).
        if isinstance(runnable, A2AClientRunnable):
            return registry_key, runnable.tracking_key, {
                "context_id": conversation_id,
                "is_complete": True,
                "sub_agent_id": sub_agent_id,
            }
        if isinstance(runnable, DynamicLocalAgentRunnable):
            return registry_key, runnable.tracking_key, {
                "adopt_thread_from": conversation_id,
                "is_complete": True,
                "sub_agent_id": sub_agent_id,
            }
        logger.info(
            f"Conversation adoption skipped: sub-agent {sub_agent_id} "
            f"({type(runnable).__name__}) has no adoptable continuity mechanism"
        )
        return None

    logger.info(f"Conversation adoption skipped: sub-agent {sub_agent_id} is not registered")
    return None


def _adopted_sub_agent_ids_from_tracking(a2a_tracking: dict[str, Any]) -> set[int] | None:
    """Recover the conversation's adopted sub-agent ids from persisted state.

    The adoption seed (_build_adoption_seed) stamps ``sub_agent_id`` into the
    a2a_tracking record, which lives in the checkpoint from the first turn on.
    Re-deriving the ids from it on EVERY turn — HITL resumes included — keeps
    the adopted automated agent registered for the conversation's whole
    lifetime; deriving them only from the origin DataPart would deregister the
    agent after turn one (and silently drop the approval of its own first
    delegation's interrupt).
    """
    adopted: set[int] = set()
    for record in a2a_tracking.values():
        if not isinstance(record, dict):
            continue
        sub_agent_id = _origin_int(record.get("sub_agent_id"))
        if sub_agent_id is not None:
            adopted.add(sub_agent_id)
    return adopted or None


# **Role:** You are an expert Routing Delegator. Your primary function is to accurately delegate user inquiries to the appropriate specialized remote agents.

# **Instructions:**
# YOU MUST NOT literally repeat what the agent responds unless asked to do so. Add context, summarize the conversation, and add your own thoughts.
# YOU MUST engage in multi-turn conversations with the agents. NEVER ask the user for permission to engage multiple times with the same agent.
# YOU MUST ALWAYS, UNDER ALL CIRCUMSTANCES, COMMUNICATE WITH ALL AGENTS NECESSARY TO COMPLETE THE TASK.
# NEVER STOP COMMUNICATING WITH THE AGENTS UNTIL THE TASK IS COMPLETED.

# If you have tools available to display information to the user, you MUST use them.

# ${
#   additionalInstructions
#     ? `**Additional Instructions:**\n${additionalInstructions}`
#     : ""
# }

# **Core Directives:**

# * **Task Delegation:** Utilize the \`sendMessage\` function to assign actionable tasks to remote agents.
# * **Contextual Awareness for Remote Agents:** If a remote agent repeatedly requests user confirmation, assume it lacks access to the full conversation history. In such cases, enrich the task description with all necessary contextual information relevant to that specific agent.
# * **Autonomous Agent Engagement:** Never seek user permission before engaging with remote agents. If multiple agents are required to fulfill a request, connect with them directly without requesting user preference or confirmation.
# * **Transparent Communication:** Always present the complete and detailed response from the remote agent to the user.
# * **User Confirmation Relay:** If a remote agent asks for confirmation, and the user has not already provided it, relay this confirmation request to the user.
# * **Focused Information Sharing:** Provide remote agents with only relevant contextual information. Avoid extraneous details.
# * **No Redundant Confirmations:** Do not ask remote agents for confirmation of information or actions.
# * **Tool Reliance:** Strictly rely on available tools to address user requests. Do not generate responses based on assumptions. If information is insufficient, request clarification from the user.
# * **Prioritize Recent Interaction:** Focus primarily on the most recent parts of the conversation when processing requests.
# * **Active Agent Prioritization:** If an active agent is already engaged, route subsequent related requests to that agent using the appropriate task update tool.

# **Agent Roster:**

# * Available Agents:


class OrchestratorDeepAgent:
    """
    OrchestratorDeepAgent - a specialized assistant for planning and orchestration.
    It should be instantiated with user-specific configuration to tailor its behavior.

    Architecture:
    - ONE universal graph per model type (Bedrock vs OpenAI)
    - User context (language, preferences) injected at runtime via `context` parameter
    - Tools and sub-agents injected per-user via GraphRuntimeContext registries
    - DynamicToolDispatchMiddleware handles runtime tool binding and dispatch
    - Dynamic system prompt personalizes responses based on GraphRuntimeContext
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(
        self,
        model: ModelType | None = None,
        thinking_level: ThinkingLevel | None = None,
        cost_logger=None,
    ):
        self.config = AgentSettings()
        self._default_thinking_level: ThinkingLevel | None = thinking_level or DEFAULT_THINKING_LEVEL
        # Left unresolved (may be None) so construction never depends on a configured default —
        # the concrete fallback is applied at request time in GraphFactory.get_graph (via
        # require_default_model), keeping cold-start/boot resilient to an unset chat default.
        self._default_model_type: ModelType | None = model

        # Initialize GraphFactory - centralizes all graph-related concerns
        # (model creation, checkpointer, middleware, graph caching)
        # Pass cost_logger during initialization for proper dependency injection
        self._graph_factory = GraphFactory(config=self.config, cost_logger=cost_logger)

        # Initialize client credentials auth for agent-to-agent communication (optional for local dev)
        oidc_client_id = self.config.get_oidc_client_id()
        oidc_client_secret = self.config.get_oidc_client_secret()
        oidc_issuer = self.config.get_oidc_issuer()

        if oidc_client_id and oidc_client_secret and oidc_issuer:
            self.oauth2_client = OidcOAuth2Client(
                client_id=oidc_client_id,
                client_secret=oidc_client_secret.get_secret_value(),
                issuer=oidc_issuer,
            )
            logger.info("Initialized OAuth2 client credentials authenticator")
        else:
            self.oauth2_client = None
            logger.warning("OIDC credentials not configured - agent-to-agent authentication disabled (local dev mode)")

        # Discovery services for tools and sub-agents
        # NOTE: A2A middleware is shared from GraphFactory to track task status
        self.tool_discovery_service = ToolDiscoveryService(self.config, oauth2_client=self.oauth2_client)
        self.agent_discovery_service = AgentDiscoveryService(self.config, oauth2_client=self.oauth2_client)

        # Sandbox pool (set externally via app lifespan if SANDBOX_PROVIDER configured)
        self.sandbox_pool: SandboxPool | None = None

    def _get_graph(
        self, model_type: ModelType | None = None, thinking_level: ThinkingLevel | None = None
    ) -> CompiledStateGraph:
        """Get a graph for the specified model type.

        Delegates to GraphFactory which handles model creation, caching,
        middleware setup, and graph creation.

        Args:
            model_type: The type of model ('gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6', or 'claude-haiku-4-5')

        Returns:
            CompiledStateGraph: The graph instance (cached or newly created)
        """
        return self._graph_factory.get_graph(model_type, thinking_level=thinking_level)

    def build_runtime_context(
        self,
        user_config: UserConfig,
        sandbox_pool: SandboxPool | None = None,
        adopted_sub_agent_ids: set[int] | None = None,
    ) -> GraphRuntimeContext:
        """Build GraphRuntimeContext from enriched user config.

        Transforms discovered tools and subagents into registries for dynamic
        tool dispatch at runtime. Call discover_capabilities() first to populate
        tools and sub_agents.

        Args:
            user_config: User configuration enriched with discovered tools/agents
            sandbox_pool: Optional SandboxPool for sandbox-enabled sub-agents
            adopted_sub_agent_ids: Console ids of sub-agents this conversation
                adopted a scheduled run of. Validated server-side on the blank
                first turn (_validate_scheduled_run_origin), then re-derived on
                every later turn — HITL resumes included — from the a2a_tracking
                records persisted in the checkpoint
                (_adopted_sub_agent_ids_from_tracking). Unlocks registration of
                the matching automated (interactive=False) sub-agents for this
                conversation; see build_runtime_context in app/utils.py for the
                gating.

        Returns:
            GraphRuntimeContext: Ready for graph invocation with all registries populated
        """

        # Pass static tools from orchestrator to sub-agents (e.g., get_current_time). We do not pass the
        # response tool since sub-agents have their own response strategy depending on their model.
        static_tools = self._graph_factory.get_static_tools(with_response_tool=False)

        # Extract backend_url from cost_logger if available
        backend_url = None
        if self._graph_factory.cost_logger and hasattr(self._graph_factory.cost_logger, "backend_url"):
            backend_url = self._graph_factory.cost_logger.backend_url

        return build_runtime_context(
            user_config,
            agent_settings=self.config,
            oauth2_client=self.oauth2_client,
            checkpointer=self._graph_factory.checkpointer,
            static_tools=static_tools,
            document_store=self._graph_factory.store,
            storage=get_object_storage_service(),
            document_store_bucket=self.config.DOCUMENT_STORE_S3_BUCKET or None,
            backend_factory=self._graph_factory.backend_factory,
            cost_logger=self._graph_factory.cost_logger,
            backend_url=backend_url,
            sandbox_pool=sandbox_pool,
            tool_risk_cache=getattr(self, "tool_risk_cache", None),
            adopted_sub_agent_ids=adopted_sub_agent_ids,
        )

    async def get_or_create_graph(
        self, model_type: ModelType, thinking_level: Optional[ThinkingLevel]
    ) -> CompiledStateGraph:
        """Get or create a graph for the given user configuration.

        Architecture: ONE universal graph per model type with dynamic tool injection.
        - Tools are NOT baked into the graph
        - User tools/subagents come from GraphRuntimeContext at runtime via DynamicToolDispatchMiddleware

        Args:
            model_type: The type of model ('gpt-4o', 'gpt-4o-mini', 'claude-sonnet-4.5', 'claude-sonnet-4.6' or 'claude-haiku-4-5')

        Returns:
            CompiledStateGraph: The compiled LangGraph for this model type
        """
        # Ensure the document store is ready before building/serving the graph. This is
        # idempotent and cheap once set up; on a cold start it retries (and rebuilds a
        # store-less graph) until the gateway/embedding default resolves, so semantic memory
        # self-heals without a restart instead of latching off for the process lifetime.
        await self._graph_factory.ensure_store_ready()

        # Get the graph (created lazily if needed)
        # Tools/subagents are NOT passed here - they come from GraphRuntimeContext at runtime
        return self._get_graph(model_type, thinking_level)

    async def stream(
        self,
        message_parts: list[Part],
        user_config: UserConfig,
        config: dict[str, Any],
        resume: Any = None,
        turn_state: "TurnState | None" = None,
    ) -> AsyncIterable[AgentStreamResponse]:
        """
        Stream agent responses with runtime user context injection.

        Args:
            message_parts: User message parts (text + files)
            user_config: User configuration with credentials and preferences
            config: graph config from executor (contains metadata like user_sub, assistant_id).
            resume: Optional resume value for interrupt handling

        ARCHITECTURE:
        - GraphRuntimeContext: Injected at runtime via `context` parameter for personalization
        - thread_id: Used for conversation isolation in checkpointer
        - ONE graph per model type: Shared across users, customized via runtime context

        ZERO-TRUST PRINCIPLES:
        - user_config: Verified user configuration from OIDC provider
        - context_id: Conversation identifier (used for thread isolation in checkpointer)
        - No credentials in checkpoints (GraphRuntimeContext passed at runtime, not persisted)

        FILE HANDLING:
        - message_parts: A2A message parts containing text and optionally files
        - Text is extracted from TextParts
        - Files (FileParts with S3 URIs) are converted to text descriptions
        - The orchestrator decides via tools whether to:
          1. Read file content (to understand and decide next steps)
          2. Generate presigned URL and dispatch to sub-agents

        Args:
            message_parts: List of A2A message parts (text, files, etc.)
            user_config: Verified user configuration with tokens
            context_id: Context identifier for conversation continuity (for thread isolation)
            resume: Optional resume value for continuing from an interrupt.
                   If provided, creates Command(resume=value) instead of normal input.

        Yields:
            AgentStreamResponse: Structured response with state and content

        Examples:
            # Normal execution with text parts
            async for response in agent.stream(message.parts, user_config, "conv-456"):
                print(response.content)

            # Resume from interrupt
            async for response in agent.stream(message.parts, user_config, "conv-456", resume="auth token"):
                print(response.content)

            # Execution with file parts (files are described as text references)
            async for response in agent.stream(parts_with_files, user_config, "conv-456"):
                print(response.content)
        """
        logger.debug(
            f"Processing {len(message_parts)} message parts, "
            f"User sub: {user_config.user_sub}, "
            f"Context ID: {config.get('configurable', {}).get('thread_id')}"
        )

        try:
            # Get or create graph for this model type
            # Graph is shared across users, isolated by thread_id and customized by GraphRuntimeContext
            graph = await self.get_or_create_graph(
                model_type=config["metadata"].get("model_type", self._default_model_type),
                thinking_level=config["metadata"].get("thinking_level", self._default_thinking_level),
            )
        except AgentFrameworkAuthError as e:
            logger.error(f"Authorization error while initializing: {e}")
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content="Authorization error. Please check your credentials and try again.",
            )
            return

        # Conversation-origin adoption, resolved BEFORE the runtime context is
        # built: an automated (scheduler-only) sub-agent is registered into
        # this conversation only when the origin validated server-side as one
        # of the user's own runs (see _validate_scheduled_run_origin), so the
        # adopted ids must be known at registry-build time. The expensive
        # validation runs on the blank first turn only; EVERY turn — including
        # HITL resumes, where the adopted agent's own interrupt is being
        # answered — re-derives the adopted ids from the a2a_tracking records
        # persisted in the checkpoint, or the agent would vanish from the
        # registry after turn one.
        checkpoint_state = await graph.aget_state(config)
        checkpoint_msgs: list[Any] = list(checkpoint_state.values.get("messages") or [])
        adopted_ids = _adopted_sub_agent_ids_from_tracking(
            checkpoint_state.values.get("a2a_tracking") or {}
        )
        origin: dict[str, Any] | None = None
        validated_origin: dict[str, Any] | None = None
        if resume is None and not checkpoint_msgs:
            origin = _extract_conversation_origin(message_parts)
            if origin:
                validated_origin = await _validate_scheduled_run_origin(
                    origin,
                    user_config.access_token.get_secret_value(),
                    self.config.CONSOLE_BACKEND_URL or "http://localhost:5001",
                )
                if validated_origin:
                    adopted_ids = (adopted_ids or set()) | {validated_origin["sub_agent_id"]}

        # Build GraphRuntimeContext for runtime injection (personalizes system prompt, etc.)
        # UserConfig should already have tools/agents discovered by executor via discover_capabilities()
        runtime_context = self.build_runtime_context(
            user_config,
            sandbox_pool=self.sandbox_pool,
            adopted_sub_agent_ids=adopted_ids,
        )

        # Token for the per-turn attachments context registration (reset in finally).
        _attachments_token = None

        # Determine input based on whether we're resuming or starting fresh
        if resume is not None:
            # Resume from interrupt with the provided resume value.
            # Reconstruct the attachments backend from the checkpointed HumanMessage's
            # additional_kwargs["file_blocks"] — stored there on the first turn so
            # this works across nodes without any separate persistence layer.
            input_data = Command(resume=resume)
            logger.info(f"Resume input data: Command(resume={resume})")
            # checkpoint_state was loaded by the adoption pre-pass above.
            blocks = collect_attachment_blocks_from_messages(checkpoint_msgs)
            attachments_backend = build_attachments_backend_from_blocks(blocks)
            if attachments_backend is not None:
                logger.info(
                    "Restored %d attachment(s) at /attachments/ for orchestrator HITL resume",
                    len(attachments_backend._attachments),
                )
                _attachments_token = set_current_attachments_backend(attachments_backend)
        else:
            # Build text content from parts
            # Files are described as references - orchestrator decides via tools whether to:
            # 1. Read file content (to understand and decide next steps)
            # 2. Generate presigned URL and dispatch to sub-agents

            # Build user prefix for multi-user attribution (Slack or Google Chat)
            user_prefix = None
            if runtime_context.client_user_handle:
                user_prefix = f"{runtime_context.name} {runtime_context.client_user_handle}"

            text_content, pending_file_blocks = await build_text_content(
                parts=message_parts,
                user_prefix=user_prefix,
            )

            # Store file content blocks on runtime context for deterministic
            # forwarding to sub-agents (bypasses the LLM entirely)
            runtime_context.pending_file_blocks = pending_file_blocks

            # Serialize file blocks into additional_kwargs so they survive in the
            # checkpoint (stripped by the model serialization layer — LLM never sees them).
            # NOTE: we intentionally do NOT include file content in the message text
            # visible to the orchestrator's LLM. Sub-agents handle file analysis via
            # the /attachments/ virtual filesystem and their own multimodal inputs.
            serialized_blocks = [b if isinstance(b, dict) else b.model_dump() for b in pending_file_blocks]
            current_msg = HumanMessage(
                content=text_content,
                additional_kwargs={"file_blocks": serialized_blocks} if serialized_blocks else {},
            )

            # Mount the conversation's attachments at /attachments/ for THIS turn.
            # The compiled graph is shared/cached per model — its CompositeBackend
            # routes cannot be mutated per turn (unlike DynamicLocalAgentRunnable,
            # which rebuilds its graph when attachments are present).
            # A ContextScopedAttachmentsBackend proxy reads from the ContextVar
            # registered here.  Reset in the finally block.
            #
            # Attachments are conversation-scoped: merge the current message's blocks
            # with any blocks from the last 20 checkpoint messages.  The current
            # message is appended as newest so its files win on filename collisions
            # while still preserving attachments from prior turns.
            # checkpoint_msgs was loaded before the runtime context was built
            # (the adoption pre-pass above).
            all_blocks = collect_attachment_blocks_from_messages(checkpoint_msgs + [current_msg])
            attachments_backend = build_attachments_backend_from_blocks(all_blocks)
            if attachments_backend is not None:
                logger.info(
                    "Mounting %d attachment(s) at /attachments/ for the orchestrator turn",
                    len(attachments_backend._attachments),
                )
                _attachments_token = set_current_attachments_backend(attachments_backend)
                config.setdefault("metadata", {})["has_attachments"] = True

            input_data = {"messages": [current_msg]}

            # Conversation origin (CONVERSATION_ORIGIN_EXTENSION): on the FIRST
            # turn of a conversation opened about prior work the orchestrator
            # never saw (e.g. a reply under a scheduled-run notification),
            # prepend the kind's synthetic-history reconstruction so the model
            # sees what it is being asked about. The synthetic turn MUST precede
            # current_msg: the stream handler treats everything after the last
            # HumanMessage as "this turn", and a trailing synthetic pair would
            # trip its blocked-agent heuristics. Never inject into a
            # conversation that already has history — the origin is only
            # meaningful for the message that opened it (clients attach it on
            # every message of its context precisely so a first turn that failed
            # before any checkpoint was written still gets it on retry).
            if not checkpoint_msgs and origin:
                # Cross-service conversation adoption: seed the a2a_tracking
                # record so the next delegation to the run's sub-agent
                # continues the run's own conversation instead of starting
                # blank — wire-level contextId resume for remote agents,
                # checkpoint fork for local/automated ones (see
                # _build_adoption_seed). Only for a server-validated origin.
                # Resolved BEFORE the synthetic history is built so the
                # reconstruction renders the dispatchable delegation label.
                seed = (
                    _build_adoption_seed(validated_origin, runtime_context.subagent_registry)
                    if validated_origin
                    else None
                )
                synthetic_msgs = _build_origin_history(
                    origin, delegation_label=seed[0] if seed else None
                )
                if synthetic_msgs:
                    input_data = {"messages": [*synthetic_msgs, current_msg]}
                    logger.info(
                        f"Injected synthetic conversation-origin context (kind={origin.get('kind')!r})"
                    )
                if seed:
                    registry_key, tracking_key, record = seed
                    input_data["a2a_tracking"] = {tracking_key: record}
                    logger.info(
                        f"Adopted scheduled-run conversation "
                        f"{record.get('context_id') or record.get('adopt_thread_from')} "
                        f"for sub-agent {registry_key!r}"
                    )
        try:
            # Use streaming with memory for multi-turn conversation support
            chunk_count = 0
            emitted_updates = set()  # Track emitted updates to avoid duplicates

            # Shared streaming helpers for buffer management and structured response parsing
            response_streamer = StructuredResponseStreamer("FinalResponseSchema")
            stream_buffer = StreamBuffer()

            logger.debug("Starting graph.astream with runtime context injection...")

            # The orchestrator's thread_id is used to filter out callback events
            # leaked from sub-agent graphs (GP agent, dynamic agents) that run
            # inside tool calls. Their metadata has a different thread_id
            # (e.g., "{context_id}::general-purpose") while the orchestrator's
            # own model events match the config's thread_id exactly.
            orchestrator_thread_id = config.get("configurable", {}).get("thread_id")

            # Stream the response with CUSTOM EVENTS for progressive A2A status updates
            # and MESSAGE CHUNKS for token-by-token streaming
            # Using stream_mode=['custom', 'messages'] with version="v2":
            # - 'custom': receives progressive status events from middleware
            # - 'messages': receives AIMessageChunk tokens from the LLM
            # v2 format: every chunk is a StreamPart dict:
            #   {"type": "messages"|"custom", "ns": (), "data": ...}
            # CRITICAL: Pass BOTH config and context parameters:
            # - config: Infrastructure (checkpointing via thread_id, metadata for LangSmith)
            # - context: Runtime data (tools, user preferences, sub-agents)
            # Auto-resume-once wrapper (defense-in-depth for a genuine orchestrator
            # model hang). The inter-chunk watchdog should not trip during a long
            # sub-agent step now that sub-agent dispatch emits keepalives (see
            # DynamicToolDispatchMiddleware), but if the orchestrator's OWN model
            # stream hangs, watch_stream_with_resume transparently resumes from the
            # checkpoint a single time (input=None) with the tripped budget scaled
            # up, surfacing a "recovering" status. A second stall propagates to the
            # warm hand-off handler below instead of cold-failing.
            def _make_stream(resuming: bool):
                return graph.astream(  # type: ignore
                    None if resuming else input_data,
                    config,
                    stream_mode=["custom", "messages"],
                    context=runtime_context,
                    version="v2",
                )

            _stream_with_resume = watch_stream_with_resume(
                _make_stream,
                label="orchestrator",
                # Synthetic custom part: tells the consumer loop below to reset its
                # parse/buffer state and surface a "recovering" status.
                recovery_part={"type": "custom", "ns": (), "data": ("stream_recovering", {})},
            )

            async for part in _stream_with_resume:
                chunk_count += 1
                part_type = part["type"]

                if part_type == "messages":
                    # Token-level streaming from LLM
                    # v2 data: (message_chunk, metadata) tuple
                    msg_chunk, _metadata = part["data"]
                    if not isinstance(msg_chunk, AIMessageChunk):
                        continue

                    # Only process messages from the orchestrator's own graph.
                    # Sub-agent graphs (GP agent, dynamic agents) run inside tool
                    # calls with a different thread_id (e.g., "{ctx}::general-purpose").
                    # Their callback events leak into the orchestrator's stream but
                    # must be filtered out — sub-agents emit their own thinking blocks
                    # via artifact_update events through the middleware.
                    if _metadata.get("thread_id") != orchestrator_thread_id:
                        continue

                    # --- Extended-thinking reasoning ---
                    # The gateway streams Claude's reasoning as a non-standard
                    # ``reasoning_content`` delta which base ChatOpenAI drops; our
                    # gateway-aware subclass (see model_factory) preserves it in
                    # ``additional_kwargs``. Surface it as an orchestrator thinking block.
                    reasoning_delta = (msg_chunk.additional_kwargs or {}).get("reasoning_content")
                    if reasoning_delta:
                        yield AgentStreamResponse(
                            state=TaskState.TASK_STATE_WORKING,
                            content=reasoning_delta,
                            metadata={
                                "streaming_chunk": True,
                                "intermediate_output": True,
                                "agent_name": "orchestrator",
                            },
                        )

                    # --- Tool call detection for status history ---
                    # Capture tool calls for activity-log display. Internal/leaked
                    # tools are excluded (see _ACTIVITY_LOG_EXCLUDED_TOOLS) — notably
                    # ``eval``, whose sub-agent tool-call chunks leak in here with the
                    # orchestrator's own thread_id and would render unattributed.
                    if msg_chunk.tool_call_chunks:
                        for tc_chunk in msg_chunk.tool_call_chunks:
                            tool_name = tc_chunk.get("name")
                            # Emit status for actual tool calls (not response schemas, not task tool)
                            if (
                                tool_name
                                and tool_name not in _ACTIVITY_LOG_EXCLUDED_TOOLS
                                and tool_name not in emitted_updates
                            ):
                                emitted_updates.add(tool_name)
                                yield AgentStreamResponse(
                                    state=TaskState.TASK_STATE_WORKING,
                                    content=f"Using {tool_name}\u2026",
                                    metadata={"activity_log": True},
                                )
                            # Incremental structured response streaming
                            delta = response_streamer.feed(tc_chunk)
                            if delta:
                                if response_streamer.is_working:
                                    # Intermediate "working" narration (e.g. emitted
                                    # while delegating to a sub-agent) — not the final
                                    # answer. Surface as an orchestrator thinking block,
                                    # not as visible response content.
                                    yield AgentStreamResponse(
                                        state=TaskState.TASK_STATE_WORKING,
                                        content=delta,
                                        metadata={
                                            "streaming_chunk": True,
                                            "intermediate_output": True,
                                            "agent_name": "orchestrator",
                                        },
                                    )
                                else:
                                    stream_buffer.append(delta)
                                    for chunk in stream_buffer.flush_ready():
                                        yield AgentStreamResponse(
                                            state=TaskState.TASK_STATE_WORKING,
                                            content=chunk,
                                            metadata={"streaming_chunk": True},
                                        )
                        continue

                    # --- Regular content streaming ---
                    if msg_chunk.content:
                        token_text, thinking_blocks = extract_text_from_content(msg_chunk.content)
                        for tb in thinking_blocks:
                            yield AgentStreamResponse(
                                state=TaskState.TASK_STATE_WORKING,
                                content=tb["thinking"],
                                metadata={
                                    "streaming_chunk": True,
                                    "intermediate_output": True,
                                    "agent_name": "orchestrator",
                                },
                            )
                        if token_text:
                            # Filter out FinalResponseSchema JSON that some models
                            # (e.g. Gemini) emit as plain text instead of tool calls.
                            filtered = response_streamer.feed_content(token_text)
                            if filtered:
                                if response_streamer.content_tracking and not response_streamer.is_working:
                                    # Structured response emitted as plain text
                                    # (Gemini): the extracted ``message`` delta IS
                                    # the final answer — stream it visibly.
                                    stream_buffer.append(filtered)
                                    for chunk in stream_buffer.flush_ready():
                                        yield AgentStreamResponse(
                                            state=TaskState.TASK_STATE_WORKING,
                                            content=chunk,
                                            metadata={"streaming_chunk": True},
                                        )
                                else:
                                    # "Working" narration (e.g. while delegating) or
                                    # plain text outside the structured response. The
                                    # visible answer comes exclusively from the
                                    # structured response — text the model emits
                                    # alongside a FinalResponseSchema call would
                                    # otherwise duplicate it in the streaming
                                    # artifact. Route to the thinking channel; if the
                                    # model never calls the response tool,
                                    # parse_agent_response's fallback still surfaces
                                    # this text in the final status message.
                                    yield AgentStreamResponse(
                                        state=TaskState.TASK_STATE_WORKING,
                                        content=filtered,
                                        metadata={
                                            "streaming_chunk": True,
                                            "intermediate_output": True,
                                            "agent_name": "orchestrator",
                                        },
                                    )
                    continue

                if part_type == "custom":
                    # Handle custom events emitted by middleware
                    # v2 data: the raw payload from stream_writer()
                    event = part["data"]
                    if not isinstance(event, tuple) or len(event) != 2:
                        logger.warning(f"Ignoring unexpected custom event: {type(event)}, value: {event}")
                        continue
                    event_type, event_data = event

                    if event_type == "stream_recovering":
                        # Synthetic event from _astream_with_resume on a one-shot
                        # auto-resume. Reset per-stream parse/buffer state so the
                        # resumed (regenerated) segment streams cleanly without
                        # colliding with the aborted stream's partial deltas, and
                        # surface a warm "still working" status to the user.
                        response_streamer = StructuredResponseStreamer("FinalResponseSchema")
                        stream_buffer = StreamBuffer()
                        yield AgentStreamResponse(
                            state=TaskState.TASK_STATE_WORKING,
                            content="Still working… recovering a slow step.",
                            metadata={"activity_log": True},
                        )
                        continue

                    if event_type == "keepalive":
                        # Sub-agent dispatch heartbeat. Its only job is to be a graph
                        # stream part so the inter-chunk watchdog timer resets while a
                        # long, legitimately-silent sub-agent step runs. Nothing is
                        # surfaced to the user — merely consuming it here is enough.
                        continue

                    if event_type == "a2a_status":
                        # PROGRESSIVE STATUS UPDATE from A2A middleware
                        status_msg = event_data.get("message", "")
                        if status_msg and status_msg not in emitted_updates:
                            emitted_updates.add(status_msg)
                            logger.info(f"[ORCHESTRATOR] Progressive A2A status: {status_msg}")

                            # Yield immediately to client using A2A protocol state
                            yield AgentStreamResponse(
                                state=TaskState.TASK_STATE_WORKING,
                                content=status_msg,
                            )
                        continue  # Process next event

                    elif event_type == "todo_status":
                        # STRUCTURED WORK PLAN from todo middleware
                        todos = event_data.get("todos", [])
                        if todos:
                            logger.info(f"[ORCHESTRATOR] Work plan: {len(todos)} items")
                            yield AgentStreamResponse(
                                state=TaskState.TASK_STATE_WORKING,
                                content="",
                                metadata={"work_plan": True, "todos": todos},
                            )
                        continue  # Process next event

                    elif event_type == "client_action":
                        # CLIENT-ACTION DIRECTIVE (Embedded Nannos): emitted by the
                        # client_action tool via the custom stream; forwarded to the
                        # client as an extension-tagged status update.
                        directive = event_data.get("directive")
                        if directive:
                            logger.info(f"[ORCHESTRATOR] Client-action directive: {directive}")
                            yield AgentStreamResponse(
                                state=TaskState.TASK_STATE_WORKING,
                                content="",
                                metadata={"client_action": directive},
                            )
                        continue  # Process next event

                    elif event_type == "status_history":
                        # ACTIVITY LOG from tool calls (orchestrator or sub-agents via middleware)
                        status_msg = event_data.get("message", "")
                        source = event_data.get("source")  # sub-agent name if from sub-agent, None if orchestrator
                        if status_msg:
                            metadata = {"activity_log": True}
                            if source:
                                metadata["source"] = source
                            yield AgentStreamResponse(
                                state=TaskState.TASK_STATE_WORKING,
                                content=status_msg,
                                metadata=metadata,
                            )
                        continue  # Process next event

                    elif event_type == TOOL_STATUS_EVENT:
                        # DESCRIPTIVE TOOL STATUS from ToolStatusMiddleware, on the
                        # orchestrator's own stream. Regular tools already surface via
                        # the ``messages`` tool_call_chunks path above, so forwarding
                        # every status here would duplicate them. We surface only
                        # ``eval`` (the PTC code interpreter), which the messages path
                        # deliberately drops (see _ACTIVITY_LOG_EXCLUDED_TOOLS) and
                        # would otherwise be silent — mirroring how a sub-agent's eval
                        # surfaces via its generic tool_status→ActivityLog forwarding.
                        if event_data.get("tool") == PTC_CODE_INTERPRETER_TOOL_NAME:
                            status_msg = event_data.get("status", "")
                            if status_msg and status_msg not in emitted_updates:
                                emitted_updates.add(status_msg)
                                yield AgentStreamResponse(
                                    state=TaskState.TASK_STATE_WORKING,
                                    content=status_msg,
                                    metadata={"activity_log": True},
                                )
                        continue  # Process next event

                    elif event_type == "subagent_chunk":
                        # STREAMING CONTENT CHUNK from a sub-agent (via TaskArtifactUpdateEvent)
                        # These are INTERMEDIATE OUTPUTS - the orchestrator will decide whether to
                        # use them as-is, modify them, or completely rewrite them in its final response.
                        # Frontend should display these in a collapsible "Thinking..." section.
                        chunk_content = event_data.get("content", "")
                        subagent_name = event_data.get("agent_name", "sub-agent")
                        if chunk_content:
                            yield AgentStreamResponse(
                                state=TaskState.TASK_STATE_WORKING,
                                content=chunk_content,
                                metadata={
                                    "streaming_chunk": True,
                                    "intermediate_output": True,
                                    "agent_name": subagent_name,
                                },
                            )
                        continue  # Process next event

            # Flush any remaining buffered content
            remaining = stream_buffer.flush_all()
            if remaining:
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_WORKING,
                    content=remaining,
                    metadata={"streaming_chunk": True},
                )

            # Propagate pending bypass rules from runtime context to user_config
            # so the executor can persist them after the turn completes.
            pending_bypass = getattr(runtime_context, "_pending_bypass_rules", None)
            if pending_bypass:
                user_config._pending_bypass_rules = pending_bypass

            logger.debug("===== STREAM PROCESSING COMPLETE =====")
            logger.debug(f"Total chunks processed: {chunk_count}")

            # Check if the graph was interrupted. Use the native async API: the sync
            # get_state() on an async saver takes a slow sync-bridge path
            # (fresh, unpooled connection).
            logger.debug("Getting final state...")
            final_state = await graph.aget_state(config)  # type: ignore
            logger.debug(f"Final state type: {type(final_state)}")
            logger.debug(f"Final state: {final_state}")

            # Store this single end-of-stream read on the per-turn carrier so the
            # executor can reuse it instead of issuing its own get_state() re-reads
            # (phantom / feedback / terminal checks). Nothing mutates the graph
            # between here and those checks, so the executor sees identical state.
            if turn_state is not None:
                turn_state.final_values = getattr(final_state, "values", None)
                turn_state.interrupts = tuple(getattr(final_state, "interrupts", ()) or ())
                turn_state.captured = True

            # Check for general interrupt conditions (pending nodes without specific interrupts)
            # Note: Specific interrupt handling is done in agent_executor for proper A2A task state management
            if hasattr(final_state, "interrupts") and final_state.interrupts:
                logger.debug(f"[ORCHESTRATOR] Found interrupt in final state: {final_state.interrupts[-1].value}")
                yield AgentStreamResponse.from_interrupt(
                    final_state.interrupts[-1].value,
                    pending_nodes=list(final_state.next) if hasattr(final_state, "next") else None,
                )
                return
            if hasattr(final_state, "next") and final_state.next:
                logger.warning(f"graph in final state but no interrupt: {final_state}")
                # If there are pending nodes, the graph was likely interrupted
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                    content="Process interrupted. Human intervention required.",
                    interrupt_reason="graph_interrupted",
                    pending_nodes=list(final_state.next),
                )
                # we don't handle it with a proper interrupt() since is an unexpected state, and resuming the graph
                # might not help if the underlying issue is not resolved.
                return

            if final_state and final_state.values:
                logger.debug("Processing final state values...")
                logger.debug(f"Final state values: {final_state.values}")
                response = self.get_agent_response(final_state.values)
                logger.debug(f"Generated response: {response}")
                yield response
            else:
                logger.debug("No final state or values found")
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_FAILED,
                    content="We are unable to process your request at the moment. Please try again.",
                )

        except StreamStallError as stall:
            # The stream stalled again after the one-shot auto-resume (or a first-token
            # stall before any progress). The graph is checkpointed at the last
            # completed step, so hand off warmly as input_required — the user's next
            # message resumes from the checkpoint — instead of the cold generic failure
            # in the broad handler below.
            logger.warning(
                "[ORCHESTRATOR] Stream stalled after auto-resume (%s); handing off to user for continuation",
                stall,
            )
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                content=(
                    "This is taking longer than I expected and I paused partway through. "
                    "I've saved my progress — reply “continue” and I'll pick up where I left off."
                ),
                interrupt_reason="stream_stall",
            )

        except GraphRecursionError as e:
            # Recursion limit reached — the graph state is already checkpointed at the
            # last completed step.  Surface as input_required so the user can send a
            # follow-up message to continue; the next astream() call will resume from
            # the checkpoint with a fresh step counter.
            logger.error(f"Recursion limit reached during stream processing: {e}", exc_info=True)
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                content="I've been working on this task for a while and need to take a break. "
                "I've made some progress, but the task requires more steps than I can complete in one go. "
                "Would you like me to continue from where I left off, or would you prefer to break this down into smaller tasks?",
                interrupt_reason="recursion_limit",
            )

        except Exception as e:
            # We are handling here unexpected exceptions during streaming, not handled by middlewares
            # Note: Configuration discovery and graph creation is handled by the executor
            # before calling stream(), so we don't need to re-discover here
            logger.error(f"Exception during stream processing: {e}", exc_info=True)
            # Return as failed
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content="An unexpected error occurred while processing your request. Please try again.",
            )

        finally:
            # Clear the per-turn attachments context registration.
            if _attachments_token is not None:
                reset_current_attachments_backend(_attachments_token)

    async def stream_subagent(
        self,
        runnable: Any,
        message_parts: list[Part],
        config: dict[str, Any],
        context_id: str,
        resume: Any = None,
        turn_state: "TurnState | None" = None,
        client_objects: list | None = None,
    ) -> AsyncIterable[AgentStreamResponse]:
        """Stream a scoped domain sub-agent as the top-level graph (Embedded Nannos, execute-only).

        Mirrors ``stream()``'s contract — yields ``AgentStreamResponse`` items the
        executor's streaming/extension loop already understands — but drives a
        ``DynamicLocalAgentRunnable`` directly instead of the routing orchestrator
        graph. This is the execute-only substrate (ADR-0004): the embedded
        entrypoint sub-agent (``client_action_enabled=True``) runs in-process with
        the orchestrator's interactive executor (streaming + A2A extensions + HITL),
        skipping the routing main-graph turn.

        Rather than re-implement token/structured-response parsing, this reuses the
        sub-agent's own tested ``astream`` pipeline (attachments, sandbox, pregel
        de-nesting, structured ``SubAgentResponseSchema`` streaming, interrupt
        suppress+re-raise) and adapts its typed ``StreamEvent`` output into the
        ``AgentStreamResponse`` shape ``_handle_stream_item`` consumes.

        Args:
            runnable: A built ``DynamicLocalAgentRunnable`` (from the runtime
                context's ``subagent_registry``), already ``_ensure_agent()``-ed.
            message_parts: User message parts (text; files via attachment blocks).
            config: Sub-agent RunnableConfig — ``configurable.thread_id`` must be
                the sub-agent thread (``{context_id}::dynamic-{name}``) and
                ``metadata`` the per-turn context. ``client_objects`` is injected here.
            context_id: Conversation id (orchestrator conversation id for the
                sub-agent's tracking waterfall).
            resume: Optional HITL resume value → fed as ``Command(resume=...)``.
            turn_state: Per-turn carrier (populated best-effort for executor reuse).
            client_objects: On-screen manifest for ``ClientObjectsMiddleware``.

        Yields:
            AgentStreamResponse: same shape as ``stream()`` (streaming chunks,
            activity-log / work-plan / client-action status, terminal result, or a
            HITL ``input_required`` pause).
        """
        from agent_common.a2a.base import SubAgentInput
        from agent_common.a2a.stream_events import (
            ActivityLogMeta,
            ArtifactUpdate,
            ClientActionMeta,
            ErrorEvent,
            IntermediateOutputMeta,
            TaskUpdate,
            WorkPlanMeta,
        )
        from langgraph.errors import GraphInterrupt

        # Surface the on-screen manifest to ClientObjectsMiddleware, which reads it
        # from the RunnableConfig metadata (keys "client_objects"/"clientObjects").
        if client_objects:
            config.setdefault("metadata", {})["client_objects"] = client_objects

        # Build the stream input: a Command for HITL resume (bypasses message
        # extraction inside the runnable), else a fresh SubAgentInput. Embedded is
        # single-user (identity-bound), so there is no channel user prefix.
        if resume is not None:
            stream_input: Any = Command(resume=resume)
            logger.info("[EMBEDDED] Resuming sub-agent '%s' from interrupt", getattr(runnable, "name", "?"))
        else:
            text_content, pending_file_blocks = await build_text_content(parts=message_parts, user_prefix=None)
            serialized_blocks = [b if isinstance(b, dict) else b.model_dump() for b in pending_file_blocks]
            human = HumanMessage(
                content=text_content,
                additional_kwargs={"file_blocks": serialized_blocks} if serialized_blocks else {},
            )
            stream_input = SubAgentInput(
                messages=[human],
                orchestrator_conversation_id=context_id,
                a2a_tracking={},
            )

        try:
            async for ev in runnable.astream(stream_input, config):
                # --- Streaming content chunk ---
                if isinstance(ev, ArtifactUpdate):
                    if not ev.content:
                        continue
                    md: dict[str, Any] = {"streaming_chunk": True}
                    if isinstance(ev.event_metadata, IntermediateOutputMeta):
                        md["intermediate_output"] = True
                        md["agent_name"] = getattr(runnable, "name", "assistant")
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_WORKING,
                        content=ev.content,
                        metadata=md,
                    )
                    continue

                # --- Error signal ---
                if isinstance(ev, ErrorEvent):
                    yield AgentStreamResponse(
                        state=TaskState.TASK_STATE_FAILED,
                        content=ev.error or "The assistant hit an error. Please try again.",
                    )
                    continue

                # --- Task updates: work-plan / client-action / activity-log / terminal ---
                if isinstance(ev, TaskUpdate):
                    meta = ev.event_metadata
                    if isinstance(meta, WorkPlanMeta):
                        yield AgentStreamResponse(
                            state=TaskState.TASK_STATE_WORKING,
                            content="",
                            metadata={"work_plan": True, "todos": meta.todos},
                        )
                        continue
                    if isinstance(meta, ClientActionMeta):
                        yield AgentStreamResponse(
                            state=TaskState.TASK_STATE_WORKING,
                            content="",
                            metadata={"client_action": meta.client_action},
                        )
                        continue
                    if isinstance(meta, ActivityLogMeta) or ev.status_text:
                        yield AgentStreamResponse(
                            state=TaskState.TASK_STATE_WORKING,
                            content=ev.status_text or "",
                            metadata={"activity_log": True},
                        )
                        continue
                    # Terminal result (no event_metadata, no status_text): the final answer.
                    data = ev.data
                    answer = ""
                    if data is not None and data.messages:
                        last = data.messages[-1]
                        answer = last.content if isinstance(last.content, str) else str(last.content)
                    if turn_state is not None:
                        turn_state.captured = True
                    yield AgentStreamResponse(
                        state=data.state if data is not None else TaskState.TASK_STATE_COMPLETED,
                        content=answer,
                    )

        except GraphInterrupt as gi:
            # Resumable pause: the sub-agent's astream re-raises the suppressed
            # interrupt. from_interrupt maps it exactly like the orchestrator path —
            # HITL → input_required approval card, auth → auth_required with the
            # authorize URL in content + metadata; the next turn resumes via Command.
            interrupts = gi.args[0] if gi.args else ()
            last_intr = interrupts[-1] if interrupts else None
            value = getattr(last_intr, "value", {}) if last_intr is not None else {}
            yield AgentStreamResponse.from_interrupt(value)

    def get_agent_response(self, final_state) -> AgentStreamResponse:
        """Parse the agent response to extract structured information and check for auth requirements."""
        return StreamHandler.parse_agent_response(final_state)

    async def close(self) -> None:
        """Close and clean up agent resources.

        This method should be called when the agent is no longer needed (e.g., on application shutdown).
        It delegates to the GraphFactory to handle cleanup of cost logger, database connections, etc.
        """
        await self._graph_factory.close()
