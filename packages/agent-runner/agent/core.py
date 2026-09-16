"""AgentRunner - A2A-pattern agent for executing scheduled sub-agent jobs.

Supports all sub-agent types:
- **automated/local**: LangGraph agents with MCP tools (multi-provider via agent-common model factory)
- **foundry**: Palantir Foundry query-API agents
- **remote**: A2A protocol agents at external URLs

Follows the same A2A pattern as the other A2A agents (e.g. alloy-agent):
- Extends BaseAgent and implements _stream_impl()
- JWT authentication enforced at the middleware layer
- Result is returned as JSON-encoded text in the artifact (for scheduler engine parsing)

Execution flow per call:
1. Extract scheduler metadata from the A2A message (task.history)
2. Nothing watch-specific: the scheduler decides whether a watch acts and what it says,
   then dispatches a plain prompt like any other job
3. If condition met (or task job): fetch sub-agent config from agent-console backend and
   dispatch to the appropriate agent runner (LangGraph / Foundry / remote A2A),
   capture result
4. Yield AgentStreamResponse with JSON-encoded result metadata
   (the scheduler engine handles push-notification delivery on its side)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from a2a.types import AgentCard, Message, Task, TaskState
from agent_common.a2a.authentication import AuthPayload
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.factory import make_a2a_async_runnable
from agent_common.a2a.models import LocalFoundrySubAgentConfig, LocalLangGraphSubAgentConfig
from agent_common.a2a.stream_events import ArtifactUpdate, ErrorEvent, TaskResponseData, TaskUpdate
from agent_common.a2a.structured_response import A2A_PROTOCOL_ADDENDUM
from agent_common.a2a.threads import local_sub_agent_thread_id
from agent_common.agents.dynamic_agent import DynamicLocalAgentRunnable
from agent_common.agents.foundry_agent import create_foundry_local_subagent
from agent_common.core.document_store_tools import create_document_store_tools
from agent_common.core.graph_utils import create_indexing_backend_factory
from agent_common.core.message_formatting import (
    formatting_prompt_block,
    formatting_rules,
    normalize_message_formatting,
)
from agent_common.core.model_factory import (
    create_model,
    get_default_model,
    is_valid_model,
    require_default_model,
)
from agent_common.core.step_budget import (
    DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS,
    resolve_max_model_calls,
)
from agent_common.core.token_provider import DEFAULT_LEEWAY_S, UserTokenProvider
from agent_common.core.tool_catalogue import sanitize_tool_name
from agent_common.middleware.auth_error_middleware import AuthErrorDetectionMiddleware
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct
from object_storage import get_object_storage_service

from agent.mcp_tools import McpToolResolver

if TYPE_CHECKING:
    from agent_common.core.sandbox_pool import SandboxPool
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig
from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

logger = logging.getLogger(__name__)

_CONSOLE_BACKEND_URL = os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001")
_CONSOLE_BACKEND_CLIENT_ID = os.getenv("CONSOLE_BACKEND_CLIENT_ID", "agent-console")
_MCP_GATEWAY_URL = os.getenv("MCP_GATEWAY_URL", "https://nannos.gatana.nannos.ringier.ch/mcp")
_MCP_GATEWAY_CLIENT_ID = os.getenv("MCP_GATEWAY_CLIENT_ID", "gatana")
# Stateless JSON-RPC tools/list (no SDK handshake/parse); off = always list through the SDK.
_MCP_CATALOGUE_STATELESS_LIST = os.getenv("MCP_CATALOGUE_STATELESS_LIST", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
# How much validity a memoised exchanged bearer must keep to be reused for a tool call. Set it
# above the exchanged tokens' lifetime to force one exchange per call (QA lever, see #170).
_MCP_TOKEN_LEEWAY_SECONDS = max(0.0, float(os.getenv("MCP_TOKEN_LEEWAY_SECONDS", str(DEFAULT_LEEWAY_S))))
_MCP_TIMEOUT_SECONDS = int(os.getenv("MCP_TIMEOUT_SECONDS", "300"))
_DOCUMENT_STORE_S3_BUCKET = os.getenv("DOCUMENT_STORE_S3_BUCKET", "")
# Turn budget for a scheduled sub-agent run, in **model calls**; the LangGraph
# `recursion_limit` is derived from the compiled graph where it is bound (see
# agent_common/core/step_budget.py). This used to be a hand-written 50 super-steps
# — a handful of model calls once the middleware stack's per-call node cost is paid
# — which was enough to kill a scheduled run mid-work on an agent that spends some
# of them resolving MCP tools, while the *same* sub-agent delegated from a
# conversation got 75.
#
# It is now the largest of the three default budgets, not the smallest. A scheduled
# run is unattended: nothing notices it stopping one model call short, nothing
# re-delegates it, and a truncated result is silently useless rather than visibly
# incomplete — so being too tight costs more here than anywhere else. See
# DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS, where the three sit together.
_MAX_MODEL_CALLS_ENV = "AGENT_RUNNER_MAX_MODEL_CALLS_PER_TURN"
_MAX_MODEL_CALLS_PER_TURN = resolve_max_model_calls(
    _MAX_MODEL_CALLS_ENV, DEFAULT_SCHEDULED_RUN_MAX_MODEL_CALLS
)




def _build_postgres_conn() -> str | None:
    """Build a PostgreSQL connection string from environment variables.

    Returns None if POSTGRES_HOST is not set, disabling the document store.
    """
    host = os.getenv("POSTGRES_HOST")
    if not host:
        return None
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "console")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _create_checkpointer() -> tuple[MemorySaver, AsyncConnectionPool | None]:
    """Create a connection pool (closed) and return a MemorySaver placeholder.

    AsyncPostgresSaver.__init__ calls asyncio.get_running_loop() and therefore
    cannot be instantiated in a synchronous context.  This function creates only
    the AsyncConnectionPool (open=False, safe to construct sync).
    setup_checkpointer() — called from the async lifespan — instantiates
    AsyncPostgresSaver and replaces self._checkpointer.

    Returns (placeholder, pool). Pool is None when falling back to MemorySaver.
    """
    from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import (
        build_checkpointer_pool,
        memory_fallback_allowed,
        missing_host_error,
    )

    # The checkpointer reuses the service's main POSTGRES_* connection (same DB/user as
    # the document store); POSTGRES_SCHEMA places its tables in the service's own schema.
    host = os.getenv("POSTGRES_HOST")
    if not host:
        if not memory_fallback_allowed():
            raise missing_host_error()
        logger.warning(
            "POSTGRES_HOST not set — using in-memory checkpointer. Conversation history will be lost on restart."
        )
        return MemorySaver(), None

    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "postgres")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    schema = os.getenv("POSTGRES_SCHEMA")

    pool = build_checkpointer_pool(host=host, port=port, db=db, user=user, password=password, schema=schema)
    logger.info(
        "Prepared PostgreSQL checkpointer pool (host=%s, db=%s, schema=%s) — "
        "AsyncPostgresSaver will be created in setup_checkpointer()",
        host,
        db,
        schema or "<role default>",
    )
    return MemorySaver(), pool


def _authorization_answer(messages: list[Message]) -> dict[str, Any] | None:
    """The ``{"authorization": {...}}`` DataPart, when this dispatch is an answer.

    The scheduler sends it when the owner has answered a parked run. Its presence is
    what tells this dispatch apart from fresh work: the same job, the same sub-agent
    and the same prompt would otherwise open a second task on a thread that is already
    waiting, and the executor would reject it.
    """
    for message in messages:
        for part in message.parts:
            if part.WhichOneof("content") != "data":
                continue
            data = MessageToDict(part.data)
            authorization = data.get("authorization") if isinstance(data, dict) else None
            if isinstance(authorization, dict):
                return authorization
    return None


def _extract_text_from_message(message: Message) -> str:
    """Extract text content from an A2A Message's parts."""
    return a2a_parts_to_content(message.parts or [], text_only=True).strip()


def _a2a_messages_to_human_messages(messages: list[Message]) -> list[HumanMessage]:
    """Convert A2A Messages to LangChain HumanMessages preserving all part types.

    Delegates to ``a2a_parts_to_content(text_only=False)`` from the SDK which maps:
    - TextPart → TextContentBlock
    - DataPart → NonStandardContentBlock (enables lossless A2A round-tripping)
    - FilePart → ImageContentBlock / AudioContentBlock / VideoContentBlock / FileContentBlock
    """
    result = []
    for msg in messages:
        if not msg.parts:
            continue
        blocks = a2a_parts_to_content(msg.parts, text_only=False)
        if blocks:
            result.append(HumanMessage(content=blocks))
    return result


def _current_time_context(timezone_name: str | None) -> str:
    """Render "now" for tool-less LLM prompts (condition eval, message generation).

    Those calls cannot consult date tools, so without an anchor the model latches
    onto whatever timestamp appears in the data (e.g. a stale snapshot date).
    """
    now_utc = datetime.now(UTC)
    line = f"Current time: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    if timezone_name:
        try:
            local = now_utc.astimezone(ZoneInfo(timezone_name))
            line += f" ({local.strftime('%Y-%m-%d %H:%M:%S')} {timezone_name})"
        except Exception:
            pass
    return line


def _build_sub_agent_system_prompt(system_prompt: str, message_formatting: str) -> str:
    """The sub-agent's own prompt, plus the response protocol and the channel's rules.

    The rendering rules cannot live in the stored system prompt: it is written once and
    reused across every job, while the same agent may notify Slack for one job and the
    web console for the next. Assembling them per run is what makes correct formatting
    built in rather than something each job's author has to remember to ask for.

    This is the stand-alone case, and the only one that wants the rules. A scheduled run
    has no orchestrator in between: whatever the sub-agent writes here is delivered to
    the channel verbatim. When the orchestrator routes instead, it composes the delivered
    message and applies the channel's rules to that, so its sub-agents are given no
    formatting instructions at all — theirs is raw material for an answer someone else
    writes, and rules about a medium they never write to would only spend their prompt.
    """
    parts = [system_prompt, A2A_PROTOCOL_ADDENDUM]
    formatting_block = formatting_prompt_block(message_formatting)
    if formatting_block:
        parts.append(formatting_block)
    return "\n\n".join(parts)


def _extract_message_metadata(task: Task, messages: list[Message] | None = None) -> dict[str, Any]:
    """Extract scheduler metadata from the message being handled.

    The scheduler engine injects metadata (sub_agent_id, scheduled_job_id,
    scheduled_job_run_id, messageFormatting) into the A2A message it sends.

    *messages* — what this invocation was actually given — is read first, and the task's
    history only as a fallback. Reading history alone was correct exactly while every
    dispatch opened a fresh task, and silently wrong the moment one CONTINUED a task:
    a resumed run's history ends with the AGENT's own last message (the auth_required
    payload), not the user's new one, so every id came back None. The run then took the
    no-sub-agent branch and echoed the authorization answer back as its result — a
    "successful" run that did none of the work it was resumed to finish.

    SECURITY NOTE: user_id is NOT extracted from message metadata as it would be
    unverified user input. Instead, fetch it from agent-console backend using the
    verified user_sub from JWT authentication.

    Args:
        task: The A2A Task object from the executor.

    Returns:
        Dict of scheduler metadata, or empty dict if not found.
    """
    def _as_dict(meta: Any) -> dict[str, Any]:
        # Over gRPC the metadata is a protobuf Struct; dict() would only convert the
        # top level, leaving nested values as Structs that support ["key"] but not
        # .get(). Convert the whole tree to plain Python instead.
        return MessageToDict(meta) if isinstance(meta, Struct) else dict(meta)

    try:
        for message in reversed(messages or []):
            if getattr(message, "metadata", None):
                return _as_dict(message.metadata)
        # Fallback: the last message in history that carries any. Walked backwards
        # rather than taking [-1], so an agent message appended after the user's does
        # not hide it.
        for last_msg in reversed(list(task.history or [])):
            if hasattr(last_msg, "metadata") and last_msg.metadata:
                meta = last_msg.metadata
                return _as_dict(meta)
    except Exception:
        logger.exception("Could not read scheduler metadata off the incoming message")
    return {}


# A2A task states worth reporting as a run's terminal task_state (see
# _collect_sub_agent_run). Non-terminal states (working, ...) map to None:
# they carry no information about how the run ended.
#
# ``auth_required`` is terminal for the RUN while being non-terminal for the TASK,
# and that is the whole mechanism: the run stops and reports, the task stays open
# so the owner's answer has something to address. See
# docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.
_TERMINAL_TASK_STATE_NAMES = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
}

#: Namespace for the A2A task id of a scheduled run's sub-agent delegation.
#: The orchestrator derives its delegation task ids from the tool call, because
#: LangGraph replays that call byte-identical. agent-runner has no outer graph and
#: no replay, so it derives from the thing that IS deterministic here: the run.
_SCHEDULED_RUN_TASK_NAMESPACE = uuid.UUID("b3a7c15e-4d92-4f8a-9c61-7e2d8f0a3b54")


def _is_full_catalogue_agent(sub_agent_cfg: dict) -> bool:
    """Whether an empty tool whitelist means "everything" for this sub-agent.

    Mirrors the orchestrator's rule (``app/utils.py``): the general-purpose agent, or a
    sub-agent whose authority deliberately left the list open. Kept as one predicate so
    the two services cannot drift into disagreeing about what an empty list means — the
    drift is invisible from either side and shows up only as an agent insisting a tool
    does not exist.
    """
    return bool(sub_agent_cfg.get("all_tools")) or sub_agent_cfg.get("name") == "general-purpose"


def scheduled_run_task_id(context_id: str) -> str:
    """The sub-agent task id for a scheduled run's conversation, derived not stored.

    Proposed when the first run opens the task, and addressed again by every
    authorization answer that continues it — so a resume finds the parked task instead
    of opening a second one on a thread that is already waiting. Nothing persists it.

    Keyed on the CONTEXT alone, not on the run. A resumed run can park again (one
    authorization leading to another), and each link in that chain is a new run row on
    the SAME context and the same ``{ctx}::dynamic-{name}`` thread — so there is one
    sub-agent task throughout. Deriving per run gave the second answer an id nothing had
    ever opened, and the resume died on "Task ... not found" with the chain unfinishable.
    One context is one sub-agent conversation: a scheduled run has a single sub-agent, and
    every run of a job gets its own context.
    """
    return str(uuid.uuid5(_SCHEDULED_RUN_TASK_NAMESPACE, context_id))


@dataclass(frozen=True)
class SubAgentRun:
    """What one sub-agent execution reported back.

    A tuple until a parked run had a third thing to carry home. The ask is not an
    optional extra: a run that reports ``auth_required`` without one leaves the
    scheduler with a stopped job and no question to put to its owner.
    """

    message: str | None = None
    #: Terminal A2A task state as a scheduler-facing string, or None if never reported.
    task_state: str | None = None
    #: The in-task-auth client payload, on an ``auth_required`` run only.
    auth_payload: dict[str, Any] | None = None


async def _collect_sub_agent_run(
    runnable: Any, input_data: SubAgentInput, config: dict[str, Any] | None = None
) -> SubAgentRun:
    """Run one A2A exchange with a sub-agent and reduce its stream to a result.

    Accumulates non-intermediate ``ArtifactUpdate`` content (the main response
    chunks), falling back to text from the last ``TaskResponseData`` messages when
    neither artifact nor message content was streamed.

    The terminal task state is reported as a scheduler-facing string. Two of them
    mean the run stopped waiting for a person rather than finishing:
    ``input_required`` tells a conversation adopting this run that the sub-agent
    asked a question, and ``auth_required`` means the run is parked on a credential
    only its owner can grant — the ask travels with it.
    """
    parts: list[str] = []
    last_data: TaskResponseData = TaskResponseData()

    payload = input_data.model_dump()
    # A local runnable REQUIRES the caller's config (it carries the checkpointer and the
    # cost-tracking context); a remote one takes the message and ignores it.
    stream = runnable.astream(payload, config) if config is not None else runnable.astream(payload)

    async for item in stream:
        if isinstance(item, ArtifactUpdate) and item.event_metadata is None:
            if item.content:
                parts.append(item.content)
        elif isinstance(item, TaskUpdate):
            last_data = item.data
        elif isinstance(item, ErrorEvent):
            return SubAgentRun(
                message=(f"Error: {item.error}" if item.error else None),
                task_state="failed",
            )

    task_state = _TERMINAL_TASK_STATE_NAMES.get(last_data.state)
    text = ("".join(parts).strip() or None) if parts else _extract_text_from_messages(last_data.messages)
    return SubAgentRun(message=text, task_state=task_state, auth_payload=_client_auth_payload(last_data))


def _client_auth_payload(data: TaskResponseData) -> dict[str, Any] | None:
    """The half of an ``auth_required`` task's ask that may cross to an end user.

    ``A2AStreamTranslator`` leaves the parsed requirement on the task metadata as
    ``auth_info`` (a full ``AuthPayload`` dump). Round-tripping it through
    ``client_payload`` rather than forwarding it is not ceremony: that method builds
    its output field by field from the requirement, so a client secret cannot be
    emitted even if a future field carries one. The payload is stored on the run row
    and rendered by three chat clients and the console, which is exactly the blast
    radius that argues for a serializer that *cannot* leak rather than a convention
    that says not to.
    """
    if data.state != TaskState.TASK_STATE_AUTH_REQUIRED:
        return None
    auth_info = data.metadata.get("auth_info")
    if not isinstance(auth_info, dict):
        logger.warning("auth_required task carried no auth_info metadata; the ask will have no URL")
        return None
    try:
        return AuthPayload(**auth_info).client_payload()
    except Exception:
        logger.exception("Could not read the in-task-auth payload off an auth_required task")
        return None


def _extract_text_from_messages(messages: list) -> str | None:
    """Extract human-readable text from A2A response messages.

    A ``TaskResponseData``'s messages are AIMessages carrying the agent's plain
    text (a string, or a list of content blocks); nothing is wrapped in them.
    """
    for msg in reversed(messages):
        raw = getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        if not raw:
            continue
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, list):
            text = " ".join(c.get("text", "") for c in raw if isinstance(c, dict) and c.get("type") == "text").strip()
        else:
            continue
        text = text.strip()
        if text:
            return text
    return None


class AgentRunner(BaseAgent):
    """A2A agent that executes scheduled sub-agent jobs of any type.

    Supports automated (LangGraph), local (LangGraph), foundry, and remote (A2A)
    sub-agent types. Uses agent-common's model factory for multi-provider LLM support.

    Follows the BaseAgent interface:
    - stream() is the template method (provided by BaseAgent)
    - _stream_impl() is the implementation (defined here)
    - close() cleans up resources
    """

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self) -> None:
        super().__init__()
        self._checkpointer, self._checkpointer_pool = _create_checkpointer()
        self._oauth2_client: OidcOAuth2Client | None = None
        self._sandbox_pool: SandboxPool | None = None
        # Enable cost tracking so get_langchain_callbacks() works for LangGraph runs.
        # report_usage() is overridden as a no-op below to avoid a spurious "requests: 1"
        # entry being logged for the agent-runner dispatcher itself.
        backend_url = os.getenv("CONSOLE_BACKEND_URL")
        if backend_url:
            try:
                self.enable_cost_tracking(backend_url=backend_url)
                logger.info("AgentRunner: cost tracking enabled")
            except Exception as ct_err:
                logger.warning(f"AgentRunner: failed to enable cost tracking: {ct_err}")

        # Document store (PostgreSQL + pgvector) — optional, shared with orchestrator.
        # Disabled when POSTGRES_HOST is not configured.
        #
        # Embeddings are resolved LAZILY (see _resolve_store_mode / ensure_store_ready), NOT
        # here: at construction the gateway/console caches can be cold (pod boots before they
        # are reachable), and resolving eagerly would latch the store off for the whole
        # process lifetime on a transient cold-start failure. The store self-heals instead —
        # it retries a cold gateway and upgrades to a semantic index once an embedding default
        # is configured, without a restart.
        self._postgres_conn: str | None = _build_postgres_conn()
        self._store: AsyncPostgresStore | None = None
        self._connection_pool: AsyncConnectionPool | None = None
        self._embeddings_model = None
        # Store readiness state: None = undecided/transient (retry), "absent" = no embedding
        # default configured (stable, store-less, watched for one), "indexed" = embeddings
        # resolved (terminal).
        self._store_mode: str | None = None
        self._store_setup_complete = False
        if not self._postgres_conn:
            logger.info("AgentRunner: document store disabled (POSTGRES_HOST not set)")

    def _resolve_store_mode(self) -> None:
        """Lazily resolve the embedding model that backs the document store's semantic index.

        Sets self._store_mode to one of:
          - "indexed": a default embedding model resolved → semantic index available (terminal).
          - "absent":  the defaults endpoint answered and no embedding default is set — a
                       stable, supported state. Cacheable, but ensure_store_ready() upgrades it
                       to "indexed" if an admin sets one later.
          - None:      transient/cold (gateway or console not reachable yet) — the caller must
                       NOT build a store and should retry on the next readiness check.

        This turns a cold-start hiccup into a retry instead of a process-lifetime latch."""
        if self._store_mode == "indexed":
            return
        from agent_common.core.model_factory import (
            EmbeddingModelNotConfigured,
            create_embeddings,
            embedding_default_known_absent,
        )

        try:
            self._embeddings_model = create_embeddings()
            self._store_mode = "indexed"
            logger.info("AgentRunner: gateway embeddings resolved; document store semantic index enabled")
        except EmbeddingModelNotConfigured:
            if embedding_default_known_absent():
                if self._store_mode != "absent":
                    logger.info(
                        "AgentRunner: no default embedding model configured; semantic index disabled until one is set"
                    )
                self._store_mode = "absent"
            else:
                self._store_mode = None  # transient/cold — retry later
        except Exception as e:
            self._store_mode = None  # transient (gateway unreachable, etc.) — retry later
            logger.debug("AgentRunner: embeddings not resolvable yet (%s); will retry", e)

    def _reset_store(self) -> None:
        """Drop the cached store so the next readiness check rebuilds with a semantic index.
        Used when an embedding default appears after we settled store-less. The connection
        pool is index-agnostic and is reused."""
        self._store = None
        self._store_mode = None
        self._store_setup_complete = False
        self._embeddings_model = None

    @property
    def store(self) -> AsyncPostgresStore | None:
        """Lazy-initialise the shared AsyncPostgresStore.

        Returns None when POSTGRES_HOST is not configured, AND when embeddings are still
        resolving on a cold start (transient) — in that case nothing is built, so a later
        access (driven by ensure_store_ready) retries. Builds the store once the mode is
        decided ("indexed" → with semantic index, "absent" → store-less).
        """
        if not self._postgres_conn:
            return None
        if self._store is not None:
            return self._store

        if self._store_mode not in ("indexed", "absent"):
            self._resolve_store_mode()
        if self._store_mode is None:
            return None  # transient — don't build; ensure_store_ready() retries

        if self._connection_pool is None:
            self._connection_pool = AsyncConnectionPool(
                self._postgres_conn,
                min_size=1,
                max_size=5,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
        from agent_common.core.model_factory import get_embedding_dimension
        from langgraph.store.postgres.base import PostgresIndexConfig

        index_config: PostgresIndexConfig | None = None
        if self._embeddings_model is not None:
            index_config = {
                # Single source of truth: same dimension create_embeddings() requests,
                # so the index and the produced vectors always agree.
                "dims": get_embedding_dimension(),
                "embed": self._embeddings_model,
                "fields": ["contextualized_content"],  # description + chunk text combined, ≤50k chars
            }

        self._store = AsyncPostgresStore(conn=self._connection_pool, index=index_config)
        if index_config is not None:
            logger.info("Initialised AsyncPostgresStore (gateway embeddings, %d dims)", get_embedding_dimension())
        else:
            logger.info("Initialised AsyncPostgresStore without semantic indexing (no embedding default)")
        return self._store

    async def setup_checkpointer(self) -> None:
        """Instantiate AsyncPostgresSaver, open pool, verify PG ≥ 11, run migrations.

        Replaces the MemorySaver placeholder in self._checkpointer with the real saver.
        """
        pool = self._checkpointer_pool
        if pool is None:
            return  # permanent MemorySaver — nothing to do

        from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import (
            _verify_postgres_version,
            open_pool_if_closed,
        )

        await open_pool_if_closed(pool)
        logger.info("Checkpoint connection pool open")

        await _verify_postgres_version(pool)

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        serde = None
        s3_bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")
        if s3_bucket:
            from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import S3OffloadingSerde

            threshold = int(float(os.getenv("CHECKPOINT_S3_THRESHOLD_MB", "1")) * 1024 * 1024)
            serde = S3OffloadingSerde(bucket=s3_bucket, threshold_bytes=threshold)

        # AsyncPostgresSaver v3.x does not support custom schema names — tables are
        # always created in the public schema.
        checkpointer = AsyncPostgresSaver(pool, serde=serde)
        await checkpointer.setup()
        self._checkpointer = checkpointer
        logger.info("PostgreSQL checkpointer ready (tables in public schema, s3_offload=%s)", bool(serde))

    async def teardown_checkpointer(self) -> None:
        """Close the checkpoint connection pool."""
        pool = self._checkpointer_pool
        if pool is not None:
            from ringier_a2a_sdk.agent.postgres_checkpointer_mixin import close_pool_if_open

            await close_pool_if_open(pool)
            logger.info("Closed checkpoint connection pool")

    async def ensure_store_setup(self) -> None:
        """Startup hook: best-effort document-store setup before serving requests.

        If the gateway/console is cold at boot the store stays unready and ensure_store_ready()
        (called per request) retries it. Safe to call multiple times.
        """
        await self.ensure_store_ready()

    async def ensure_store_ready(self) -> None:
        """Per-request, idempotent document-store readiness check. Cheap once set up.

        Makes the store self-heal without a restart:
          - a transient cold-start (gateway/console unreachable at boot) leaves the mode
            undecided, so this is a no-op now and retried on the next request;
          - if we settled store-less because no embedding default was configured, but an admin
            has since set one, the cached store is dropped and rebuilt with a semantic index.
        """
        if not self._postgres_conn:
            return

        from agent_common.core.model_factory import is_embeddings_configured

        # Self-heal: an embedding default appeared after we settled store-less → rebuild indexed.
        if self._store_mode == "absent" and is_embeddings_configured():
            logger.info("AgentRunner: embedding default now configured; rebuilding store with semantic index")
            self._reset_store()

        if self._store_setup_complete:
            return

        self._resolve_store_mode()
        if self._store_mode is None:
            return  # transient/cold — retry on the next request

        store = self.store
        if store is None:
            return
        if self._connection_pool is not None and not self._connection_pool._opened:
            await self._connection_pool.open()
            logger.info("Opened AsyncConnectionPool for document store")
        try:
            await store.setup()
            self._store_setup_complete = True
            logger.info("Document store ready (mode=%s)", self._store_mode)
        except Exception as exc:
            logger.warning(f"Document store setup failed (continuing without): {exc}")

    async def close(self) -> None:
        """Clean up resources."""
        if self._connection_pool is not None and self._connection_pool._opened:
            await self._connection_pool.close()
            logger.info("Closed document store connection pool")

    async def init_sandbox_pool(self) -> None:
        """Initialize sandbox pool if SANDBOX_PROVIDER is configured.

        Same mechanics as the orchestrator: reads SANDBOX_PROVIDER, SANDBOX_WARM_TTL,
        SANDBOX_POOL_CAPACITY, and provider-specific env vars (GATANA_*).
        """
        sandbox_provider_name = os.environ.get("SANDBOX_PROVIDER")
        if not sandbox_provider_name:
            return

        try:
            from agent_common.core.sandbox_pool import SandboxPool as _SandboxPool

            warm_ttl = float(os.environ.get("SANDBOX_WARM_TTL", "300"))

            if sandbox_provider_name == "gatana":
                import asyncio as _aio

                from gatana_client import GatanaClient
                from gatana_langchain import GatanaSandbox

                if not os.getenv("GATANA_API_KEY") or not os.getenv("GATANA_ORG_ID"):
                    raise ValueError("GATANA_ORG_ID and GATANA_API_KEY are required")
                org_capacity = int(os.environ.get("GATANA_ORG_CAPACITY", "10"))

                async def _create_sandbox():
                    client = GatanaClient()
                    return await _aio.to_thread(
                        GatanaSandbox,
                        client=client,
                    )

                capacity = int(os.environ.get("SANDBOX_POOL_CAPACITY", "0")) or max(1, org_capacity - 2)
            else:
                raise ValueError(f"Unknown sandbox provider: {sandbox_provider_name!r}. Available: gatana")

            self._sandbox_pool = _SandboxPool(
                create_fn=_create_sandbox,
                capacity=capacity,
                warm_ttl=warm_ttl,
                home="/home/ubuntu",
            )
            await self._sandbox_pool.start_reaper()
            logger.info(
                "Sandbox pool initialized (provider=%s, capacity=%d)",
                sandbox_provider_name,
                self._sandbox_pool.capacity,
            )
        except Exception as e:
            logger.error("Failed to initialize sandbox pool: %s", e)
            self._sandbox_pool = None

    async def shutdown_sandbox_pool(self) -> None:
        """Shut down sandbox pool if initialized."""
        if self._sandbox_pool:
            await self._sandbox_pool.shutdown()
            logger.info("Sandbox pool shut down")

    async def report_usage(self, user_config: UserConfig, task: Task) -> None:
        """No-op: agent-runner is a dispatcher and has no LLM usage of its own to report.
        Cost entries are logged by the sub-agents it dispatches to.

        If ever required to be enabled, we need to consider that the executor will create its own context id, this
        requires us to rethink how we log usage for the agent-runner vs the sub-agents, and how to link them together.
        """
        pass

    async def _stream_impl(
        self,
        messages: list[Message],
        user_config: UserConfig,
        task: Task,
    ) -> AsyncIterable[AgentStreamResponse]:
        """Execute a scheduled job and yield the result as AgentStreamResponse.

        Routes to the appropriate execution strategy based on the sub-agent type:
        - automated/local → LangGraph agent with agent-common model factory
        - foundry → Foundry query-API agent via agent-common
        - remote → A2A protocol call via agent-common

        The scheduler engine parses the content of the final artifact as JSON
        to extract structured metadata (scheduler_status, agent_message, etc.).

        Args:
            messages: List of A2A Messages from the user (each may contain text, files, data).
            user_config: Authenticated user context from JWT middleware.
            task: The A2A task with message history and metadata.

        Yields:
            AgentStreamResponse with JSON-encoded result metadata.
        """
        yield AgentStreamResponse(state=TaskState.TASK_STATE_WORKING, content="Executing scheduled job...")

        # Extract scheduler-specific metadata from the message
        message_meta = _extract_message_metadata(task, messages)

        # Struct numbers arrive as floats (protobuf doubles); coerce the ids back
        # to int — e.g. the sub-agent config URL path rejects "42.0". Numeric strings are
        # accepted too: a caller that stringifies the id must not silently turn into a
        # no-op run (the sub-agent branch below is skipped when this returns None).
        def _meta_int(key: str) -> int | None:
            value = message_meta.get(key)
            if isinstance(value, bool):
                return None
            if isinstance(value, int | float):
                return int(value)
            if isinstance(value, str):
                try:
                    return int(float(value))
                except ValueError:
                    logger.warning("Ignoring non-numeric %s in message metadata: %r", key, value)
            return None

        # How the delivering channel renders text. A scheduled run has no client on the
        # other end to say so per turn, so the scheduler resolves it from the job's
        # delivery channel and sends it here under the same key an interactive client
        # uses. Nothing downstream rewrites the agent's output, so an unset/unknown value
        # means Markdown — which is what a Slack notification used to arrive as.
        message_formatting = normalize_message_formatting(
            message_meta.get("messageFormatting") or message_meta.get("message_formatting")
        )

        sub_agent_id: int | None = _meta_int("sub_agent_id")
        scheduled_job_id: int | None = _meta_int("scheduled_job_id")
        scheduled_job_run_id: int | str = _meta_int("scheduled_job_run_id") or ""

        # SECURITY: Use verified access token from JWT (validated by JWTValidatorMiddleware)
        # and fetch user_id from backend API to prevent privilege escalation
        user_access_token = user_config.access_token.get_secret_value() if user_config.access_token else ""
        user_id: str | None = await self._fetch_user_id_from_backend(user_access_token) if user_access_token else None

        message_text = "\n".join(_extract_text_from_message(m) for m in messages).strip()
        agent_message: str | None = None
        sub_agent_task_state: str | None = None
        sub_agent_name: str | None = None
        prompt: str | None = None
        auth_payload: dict[str, Any] | None = None

        # An answer to a run this service parked earlier. The scheduler addressed THIS
        # task by id — which the handler accepted only because the parked run left it
        # non-terminal — so the sub-agent task to continue is the one that run derived,
        # not a new one.
        authorization = _authorization_answer(messages)
        resume_task_id = (
            scheduled_run_task_id(task.context_id) if authorization and task.context_id else None
        )
        if authorization:
            logger.info(
                "Job %s run %s: resuming parked task %s (%s)",
                scheduled_job_id,
                scheduled_job_run_id,
                resume_task_id,
                authorization.get("decision"),
            )

        # Correlation ids echoed back in every result so the delivery channel can
        # link a notification (and later thread replies) to this job/run/sub-agent.
        correlation_meta = {
            "scheduled_job_id": scheduled_job_id,
            "scheduled_job_name": message_meta.get("scheduled_job_name"),
            # Echoed untouched so the delivery channel can post this run's result as a
            # reply to the ask that unblocked it. Meaningless here; only the client that
            # rendered the card can read it.
            "reply_to_message": message_meta.get("reply_to_message"),
            "scheduled_job_run_id": scheduled_job_run_id or None,
            "sub_agent_id": sub_agent_id,
        }

        # --- 2. Sub-agent execution ---
        if sub_agent_id:
            prompt = message_text or "Execute your configured task."

            try:
                # Fetched here (not inside _execute_sub_agent) so the failure
                # branch below still knows which sub-agent was targeted.
                sub_agent_cfg = await self._fetch_sub_agent_config(sub_agent_id, user_access_token)
                sub_agent_name = sub_agent_cfg["name"]
                run = await self._execute_sub_agent(
                    sub_agent_cfg=sub_agent_cfg,
                    prompt=prompt,
                    raw_a2a_messages=messages,
                    user_access_token=user_access_token,
                    scheduled_job_id=scheduled_job_id,
                    scheduled_job_run_id=scheduled_job_run_id,
                    user_config=user_config,
                    user_id=user_id,
                    context_id=task.context_id,
                    message_formatting=message_formatting,
                    resume_task_id=resume_task_id,
                )
                agent_message, sub_agent_task_state = run.message, run.task_state
                auth_payload = run.auth_payload
            except Exception as exc:
                logger.exception(f"Sub-agent execution failed for job {scheduled_job_id}")
                error_message = str(exc)
                result_meta = {
                    "scheduler_status": "failed",
                    "error_message": error_message,
                    "agent_message": agent_message,
                    "user_sub": user_config.user_sub,
                    "sub_agent_name": sub_agent_name,
                    "prompt": prompt,
                    **correlation_meta,
                }
                yield AgentStreamResponse(
                    state=TaskState.TASK_STATE_FAILED,
                    content=json.dumps(result_meta, default=str),
                )
                return

        # A run blocked on the owner's credential is neither a success nor a failure,
        # and it is the one outcome that leaves work to come back to. The ask travels
        # INSIDE this payload rather than replacing the status message: every delivery
        # client parses part zero as this JSON, and a client that has not learned the
        # in-task-auth card still has to be able to post something the owner can act on.
        parked = sub_agent_task_state == "auth_required"
        if parked and not auth_payload:
            logger.warning(
                "Job %s parked on authorization but produced no ask; the owner would have "
                "nothing to answer, so this is reported as a failure instead",
                scheduled_job_id,
            )

        # A sub-agent that FAILED must not be recorded green. Going through
        # ``LocalA2ARunnable.astream`` changed how a crash arrives here: it catches every
        # exception and yields an ErrorEvent instead of raising, so the exception branch
        # above — the only thing that used to write ``failed`` — is never reached for a
        # local agent any more. Left alone that turns "the agent blew up" into a success
        # row whose result text happens to start with "Error:", which is exactly the
        # silently-green run this ADR exists to abolish, arriving through the ADR's own
        # refactor. The task state is the authority on how the run ended; the status
        # follows it.
        failed = sub_agent_task_state == "failed"

        result_meta = {
            "scheduler_status": (
                "auth_required" if (parked and auth_payload) else "failed" if failed else "success"
            ),
            # No sub-agent means there is nothing to run: the dispatch carries the text
            # to deliver and echoing it back is what the delivery channel picks up. That
            # is a watch whose outcome is a notification — the scheduler decided the
            # condition was met and wrote what to say before dispatching.
            "agent_message": agent_message or message_text or None,
            # The sub-agent's terminal A2A task state — notably
            # "input_required" (the run asked the user a question and is
            # waiting). Delivery channels persist it with the run's provenance
            # and forward it in the conversation-origin DataPart so the
            # adopting orchestrator can frame the user's reply correctly.
            "task_state": sub_agent_task_state,
            # Same text as agent_message, under the key a failure is read from. The
            # error branch above sets both for a raised exception; a sub-agent that
            # reported its own failure has to be told apart the same way.
            **({"error_message": agent_message} if failed else {}),
            "user_sub": user_config.user_sub,
            "sub_agent_name": sub_agent_name,
            "prompt": prompt,
            **correlation_meta,
        }
        if failed:
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_FAILED,
                content=json.dumps(result_meta, default=str),
            )
            return

        if parked and auth_payload:
            result_meta["auth_payload"] = auth_payload
            # The task the answer is addressed to: this one, the OUTER task, not the
            # sub-agent's. The sub-agent's is derived from the run and never leaves
            # this process; this is the only id anything outside can reach.
            result_meta["parked_task_id"] = task.id
            # Where the answer goes, declared rather than hardcoded in three clients. A
            # pushed A2A task carries no statement of where its server lives, and the
            # one A2A server a chat client is configured with is the orchestrator —
            # right for a chat turn, wrong for a scheduled run. Logical, not a URL: the
            # client resolves its own console-backend base from its own configuration,
            # so there is no address off a webhook for anyone to trust.
            result_meta["reply_to"] = {
                "service": "console-backend",
                "endpoint": "scheduled_run_resume",
                "scheduled_job_id": scheduled_job_id,
                "scheduled_job_run_id": scheduled_job_run_id or None,
            }
            # AUTH_REQUIRED, not COMPLETED, and deliberately non-terminal: the A2A
            # request handler accepts a message addressed to this task only while it has
            # not reached a terminal state, and that message is how the owner's answer
            # gets back in. The push sender fires on every event, not only terminal
            # ones, so the ask is delivered by the same path a result is.
            yield AgentStreamResponse(
                state=TaskState.TASK_STATE_AUTH_REQUIRED,
                content=json.dumps(result_meta, default=str),
            )
            return

        yield AgentStreamResponse(
            state=TaskState.TASK_STATE_COMPLETED,
            content=json.dumps(result_meta, default=str),
        )

    async def _fetch_user_id_from_backend(self, user_access_token: str) -> str | None:
        """Fetch the verified user_id from agent-console backend using JWT authentication.

        SECURITY: This method ensures we use the database user ID that corresponds
        to the verified JWT user_sub, preventing privilege escalation attacks where
        a user could send arbitrary user_id values in message metadata.

        Args:
            user_access_token: Orchestrator JWT token for authentication.

        Returns:
            Database user UUID string, or None if fetch fails.
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{_CONSOLE_BACKEND_URL}/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {user_access_token}"},
                )
                response.raise_for_status()
                user_data = response.json()
                user_id = user_data.get("id")
                if user_id:
                    logger.info(f"[SECURITY] Fetched verified user_id from backend: {user_id}")
                    return user_id
                else:
                    logger.error("[SECURITY] Backend /auth/me response missing 'id' field")
                    return None
        except httpx.HTTPStatusError as exc:
            logger.error(f"[SECURITY] Failed to fetch user_id from backend: HTTP {exc.response.status_code}")
            return None
        except Exception as exc:
            logger.error(f"[SECURITY] Failed to fetch user_id from backend: {exc}")
            return None

    async def _fetch_sub_agent_config(self, sub_agent_id: int, user_access_token: str) -> dict:
        """Fetch sub-agent configuration from the agent-console API.

        Returns the full sub-agent record including type and config_version fields
        so the dispatcher can route to the correct execution strategy.

        Args:
            sub_agent_id: ID of the sub-agent.
            user_access_token: User's access token for authentication.

        Returns:
            Dict with keys: type, name, config_version (dict with model, system_prompt,
            agent_url, mcp_tools, foundry_*, enable_thinking, thinking_level, etc.)
        """
        url = f"{_CONSOLE_BACKEND_URL}/api/v1/sub-agents/{sub_agent_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {user_access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        agent_type = data.get("type", "")
        cfg_version = data.get("config_version") or {}

        return {
            "type": agent_type,
            "name": data.get("name", f"sub-agent-{sub_agent_id}"),
            "sub_agent_id": sub_agent_id,
            # Exact running config-version id, for precise cost attribution
            "sub_agent_config_version_id": cfg_version.get("id"),
            "description": cfg_version.get("description", ""),
            "system_prompt": cfg_version.get("system_prompt", ""),
            # Sanitised here, the boundary where the job's whitelist enters the run: a
            # stored name may be a tool's wire name, while the catalogue exposes it under
            # its sanitised one (see ``sanitize_tool_name``). Everything downstream then
            # compares exposed names only.
            "mcp_tools": [sanitize_tool_name(n) for n in (cfg_version.get("mcp_tools") or [])],
            # Prefer effective_model: the backend (annotate_models) resolves a tier-bound config
            # (model is None, model_tier set) to its current alias here, so a tier-bound sub-agent
            # honors its tier instead of silently falling back to the standard default.
            "model": cfg_version.get("effective_model") or cfg_version.get("model") or require_default_model(),
            "agent_url": cfg_version.get("agent_url"),
            "enable_thinking": cfg_version.get("enable_thinking", False),
            "thinking_level": cfg_version.get("thinking_level"),
            # Foundry-specific fields
            "foundry_hostname": cfg_version.get("foundry_hostname"),
            "foundry_client_id": cfg_version.get("foundry_client_id"),
            "foundry_client_secret_ssmkey": cfg_version.get("foundry_client_secret_ssmkey"),
            "foundry_ontology_rid": cfg_version.get("foundry_ontology_rid"),
            "foundry_query_api_name": cfg_version.get("foundry_query_api_name"),
            "foundry_scopes": cfg_version.get("foundry_scopes") or [],
            "foundry_version": cfg_version.get("foundry_version"),
            # Sandbox
            "sandbox_enabled": cfg_version.get("sandbox_enabled", False),
        }

    async def _execute_sub_agent(
        self,
        sub_agent_cfg: dict,
        prompt: str,
        user_access_token: str,
        scheduled_job_id: int,
        scheduled_job_run_id: int,
        user_config: UserConfig,
        user_id: str | None = None,
        context_id: str | None = None,
        raw_a2a_messages: list[Message] | None = None,
        message_formatting: str = "markdown",
        resume_task_id: str | None = None,
    ) -> SubAgentRun:
        """Dispatch a sub-agent config to the appropriate execution method.

        Args:
            sub_agent_cfg: Result of _fetch_sub_agent_config().
            prompt: The user message to process (used for local/foundry agents).
            user_access_token: Token passed through for authentication.
            scheduled_job_id: The ID of the scheduled job.
            scheduled_job_run_id: The ID of the scheduled job run, used for checkpoint isolation and logging.
            user_config: Authenticated user context.
            user_id: Verified database user UUID (fetched from backend, not from message metadata).
            context_id: Natural A2A context_id for thread isolation (conversation_id).
            raw_a2a_messages: Original A2A messages (used for remote agents to preserve DataParts).
            message_formatting: Rendering rules of the channel this run's message is
                delivered to ("slack", "google-chat", "plain", "markdown").

            resume_task_id: The task a previous run parked, when this dispatch is an
                authorization answer rather than fresh work. LangGraph agents only —
                nothing else can park.

        Returns:
            The run's message, its terminal A2A task state, and the ask when it parked.
            The task state rides the result metadata so a conversation later adopting
            this run knows whether it finished, asked a question, or is waiting on the
            owner's credential.
        """
        agent_type = sub_agent_cfg["type"]

        if agent_type in ("automated", "local"):
            return await self._run_langgraph_agent(
                sub_agent_cfg=sub_agent_cfg,
                prompt=prompt,
                raw_a2a_messages=raw_a2a_messages,
                user_access_token=user_access_token,
                user_sub=user_config.user_sub,
                user_id=user_id,
                scheduled_job_id=scheduled_job_id,
                scheduled_job_run_id=scheduled_job_run_id,
                context_id=context_id,
                message_formatting=message_formatting,
                resume_task_id=resume_task_id,
            )
        elif agent_type == "foundry":
            return await self._run_foundry_agent(
                sub_agent_cfg=sub_agent_cfg,
                prompt=prompt,
                message_formatting=message_formatting,
                user_config=user_config,
                scheduled_job_id=scheduled_job_id,
                scheduled_job_run_id=scheduled_job_run_id,
            )
        elif agent_type == "remote":
            return await self._run_remote_agent(
                sub_agent_cfg=sub_agent_cfg,
                raw_a2a_messages=raw_a2a_messages or [],
                prompt=prompt,
                user_access_token=user_access_token,
                scheduled_job_id=scheduled_job_id,
                scheduled_job_run_id=scheduled_job_run_id,
                context_id=context_id,
                message_formatting=message_formatting,
            )
        else:
            raise ValueError(
                f"Unsupported sub-agent type '{agent_type}' for sub-agent {sub_agent_cfg.get('sub_agent_id')}"
            )

    async def _run_langgraph_agent(
        self,
        sub_agent_cfg: dict,
        prompt: str,
        user_access_token: str,
        user_sub: str,
        scheduled_job_id: int,
        scheduled_job_run_id: int,
        user_id: str | None = None,
        context_id: str | None = None,
        raw_a2a_messages: list[Message] | None = None,
        message_formatting: str = "markdown",
        resume_task_id: str | None = None,
    ) -> SubAgentRun:
        """Run the scheduled sub-agent behind the in-process A2A server.

        agent-runner used to assemble a graph here and call ``graph.astream``. It now
        builds the same ``DynamicLocalAgentRunnable`` the orchestrator's local
        sub-agents are, and drives it through ``LocalA2ARunnable.astream`` — which is a
        client of ``LocalA2AServer``. That is what makes a run blocked on a credential
        an A2A task in ``auth_required`` carrying the in-task-auth DataPart: the same
        bytes an interactive chat produces, produced by the same code. See
        docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.

        *resume_task_id* addresses the task a previous run PARKED instead of proposing
        a new one. Both ids are the same derived value; which field it travels in is
        what tells the executor "answer the pending question" from "start work".

        Returns the run's message, its terminal task state, and — when it parked — the
        ask to put to the owner.
        """
        # Idempotent and cheap once set up; on a cold start it retries until the
        # gateway/embedding default resolves, so semantic memory self-heals.
        await self.ensure_store_ready()

        model_name: str = sub_agent_cfg["model"]
        if not is_valid_model(model_name):
            default_model = get_default_model()
            if not default_model:
                raise ValueError(
                    f"Invalid model '{model_name}' in sub-agent config for job {scheduled_job_id} "
                    "and no chat default is configured on the gateway to fall back to.",
                )
            logger.warning(
                f"Invalid model '{model_name}' in sub-agent config for job {scheduled_job_id} — defaulting to {default_model}",
            )
            model_name = default_model

        thinking_level = None
        if sub_agent_cfg.get("enable_thinking") and sub_agent_cfg.get("thinking_level"):
            thinking_level = sub_agent_cfg["thinking_level"]

        llm = create_model(model_name, thinking_level=thinking_level)

        # context_id should always be present in the A2A protocol — fail loudly if not.
        # It is the run's conversation, and with the agent name it is the checkpoint
        # thread a conversation adopting this run later continues.
        if not context_id:
            raise ValueError(f"Missing context_id in A2A task for scheduled job {scheduled_job_id}")

        # The channel's rendering rules are baked into the system prompt here rather
        # than passed alongside it, because the shared runnable does not read
        # ``message_formatting`` — it only forwards it on the wire. Left to the shared
        # path, a Slack notification would arrive as raw Markdown again.
        config = LocalLangGraphSubAgentConfig(
            name=sub_agent_cfg["name"],
            description=sub_agent_cfg.get("description") or f"Scheduled sub-agent {sub_agent_cfg['name']}",
            system_prompt=_build_sub_agent_system_prompt(sub_agent_cfg["system_prompt"], message_formatting),
            mcp_tools=sub_agent_cfg.get("mcp_tools") or None,
            model_name=model_name,
            sub_agent_id=sub_agent_cfg.get("sub_agent_id"),
            sub_agent_config_version_id=sub_agent_cfg.get("sub_agent_config_version_id"),
            sandbox_enabled=bool(sub_agent_cfg.get("sandbox_enabled", False)),
        )

        if config.sandbox_enabled and self._sandbox_pool is None:
            logger.warning(
                "Sub-agent '%s' has sandbox_enabled=true but no SANDBOX_PROVIDER configured; "
                "running without sandbox for job %s",
                config.name,
                scheduled_job_id,
            )

        # One provider per run: every exchange goes through it, and tools mint their
        # bearer at call time so a token expiring mid-run is re-exchanged.
        token_provider = UserTokenProvider(
            user_access_token,
            self._get_oauth2_client().exchange_token,
            leeway_seconds=_MCP_TOKEN_LEEWAY_SECONDS,
        )

        docstore_user_id = user_id or user_sub
        orchestrator_tools: list = []
        if self.store is not None and _DOCUMENT_STORE_S3_BUCKET:
            orchestrator_tools = create_document_store_tools(
                store=self.store,
                storage=get_object_storage_service(),
                s3_bucket=_DOCUMENT_STORE_S3_BUCKET,
                user_id=docstore_user_id,
            )

        # Built here rather than left to the shared runnable's default so the embedding
        # spend of document indexing still reaches this service's cost logger — the one
        # thing the old inline graph passed that the shared path does not.
        backend_factory = None
        if self.store is not None:
            backend_factory = create_indexing_backend_factory(
                store=self.store,
                model_name=model_name,
                cost_logger=self._cost_logger,
            )

        # An EMPTY whitelist does not mean "no tools" for a full-catalogue agent. The
        # general-purpose agent is configured with no tool list, and in a conversation
        # that means everything — the orchestrator hands it the whole registry as a lazy
        # catalog (utils.py, ``config.name == "general-purpose" or config.all_tools``).
        # A scheduled run read the same empty list as "nothing" and ran with no MCP tools
        # at all, so the agent answered that the tools it was asked to use do not exist:
        # same agent, same configuration, opposite capability depending on who started it.
        #
        # Handed over as a CATALOG rather than bound: these are lazy tools the model
        # reaches through search/describe, and binding a whole gateway is what OOM-killed
        # the orchestrator before catalog mode existed.
        tool_catalog: dict[str, Any] | None = None
        if not config.mcp_tools and _is_full_catalogue_agent(sub_agent_cfg):
            resolver = McpToolResolver(
                token_provider=token_provider,
                gateway_url=_MCP_GATEWAY_URL,
                gateway_client_id=_MCP_GATEWAY_CLIENT_ID,
                console_mcp_url=f"{_CONSOLE_BACKEND_URL}/mcp",
                console_client_id=_CONSOLE_BACKEND_CLIENT_ID,
                timeout=timedelta(seconds=_MCP_TIMEOUT_SECONDS),
                stateless_list=_MCP_CATALOGUE_STATELESS_LIST,
            )
            catalogue_tools = await resolver.resolve_all()
            tool_catalog = {tool.name: tool for tool in catalogue_tools}
            logger.info(
                "Sub-agent '%s' has no whitelist and takes the full catalogue: %d tools for job %s",
                config.name,
                len(tool_catalog),
                scheduled_job_id,
            )

        runnable = DynamicLocalAgentRunnable(
            config=config,
            model=llm,
            orchestrator_tools=orchestrator_tools,
            oauth2_client=self._get_oauth2_client(),
            user_token=user_access_token,
            checkpointer=self._checkpointer,
            store=self.store,
            backend_factory=backend_factory,
            sub_agent_id=config.sub_agent_id,
            user_id=docstore_user_id,
            mcp_gateway_url=_MCP_GATEWAY_URL,
            mcp_gateway_client_id=_MCP_GATEWAY_CLIENT_ID,
            console_backend_client_id=_CONSOLE_BACKEND_CLIENT_ID,
            sandbox_pool=self._sandbox_pool,
            # A per-run provider so a token expiring mid-run is re-exchanged at call
            # time rather than baked into the tools at discovery.
            token_provider=token_provider,
            tool_catalog=tool_catalog,
            # Detecting the gateway's ``need-credentials`` is what this whole path
            # exists for; without the interrupt there is nothing for the executor to
            # publish as ``auth_required``.
            extra_middlewares=[AuthErrorDetectionMiddleware()],
            # NO risk_scorer, and this is load-bearing rather than an omission: with one,
            # ``build_sub_agent_graph`` installs ConditionalHumanInTheLoopMiddleware and a
            # risky tool would park the run as ``input_required`` — an approval nobody is
            # there to give, on a job that would then stop until a human it never asked
            # answers. Only KIND_AUTH may park a scheduled run (ADR-0009 Constraints).
            risk_scorer=None,
            # The scheduled-run budget, not the delegation one. Converging on the shared
            # runnable would otherwise have cut an unattended turn from this service's
            # deployed number to the interactive sub-agent default — a budget change
            # nobody asked for, visible only as runs quietly stopping short.
            max_model_calls=_MAX_MODEL_CALLS_PER_TURN,
        )

        task_id = resume_task_id or None
        derived_task_id = scheduled_run_task_id(context_id)
        tracking: dict[str, Any] = {
            runnable.tracking_key: {
                "context_id": context_id,
                "task_id": task_id or "",
                "is_complete": False,
            }
        }

        messages = self._input_messages(prompt, raw_a2a_messages)
        input_data = SubAgentInput(
            messages=messages,
            a2a_tracking=tracking,
            orchestrator_conversation_id=context_id,
            scheduled_job_id=scheduled_job_id,
            message_formatting=message_formatting,
            # Honoured only when opening a task. On a resume the live task_id above is
            # what addresses the parked one; a proposal would be dropped as taken.
            proposed_task_id=None if task_id else derived_task_id,
        )

        parent_config = self.create_runnable_config(
            user_sub=user_sub,
            conversation_id=context_id,
            thread_id=local_sub_agent_thread_id(context_id, config.name),
            scheduled_job_id=scheduled_job_id,
            sub_agent_id=config.sub_agent_id,
            sub_agent_config_version_id=config.sub_agent_config_version_id,
        )
        if self.store is not None:
            # Consumed by IndexingStoreBackend and the document-store tools.
            parent_config["metadata"] = {
                "user_id": docstore_user_id,
                "assistant_id": docstore_user_id,
            }
        run = await _collect_sub_agent_run(runnable, input_data, parent_config)
        logger.info(
            "LangGraph agent execution complete for job %s: %d chars (task_state=%s)",
            scheduled_job_id,
            len(run.message or ""),
            run.task_state,
        )
        return run

    @staticmethod
    def _input_messages(prompt: str, raw_a2a_messages: list[Message] | None) -> list[HumanMessage]:
        """The turn's input, with DataParts flattened to text.

        ``text_only=True`` serialises DataParts as JSON strings the model can read;
        the alternative (NonStandardContentBlock) is rejected by Bedrock Converse.
        This is also how an authorization answer reaches a resumed run on the
        fallback path — the DataPart the executor routes to the parked interrupt is
        read there, not here.
        """
        if raw_a2a_messages:
            text_content = "\n".join(
                a2a_parts_to_content(msg.parts, text_only=True) for msg in raw_a2a_messages if msg.parts
            ).strip()
            if text_content:
                return [HumanMessage(content=text_content)]
        return [HumanMessage(content=prompt)]

    def int_to_uuid(self, value: int) -> str:
        """Convert an integer ID to a UUID string format used by Foundry.

        This is a placeholder implementation. The actual conversion logic should
        match how the Foundry agent expects the sub_agent_id to be formatted.
        """
        return f"00000000-0000-0000-0000-{value:012d}"

    async def _run_foundry_agent(
        self,
        sub_agent_cfg: dict,
        prompt: str,
        user_config: UserConfig,
        scheduled_job_id: int,
        scheduled_job_run_id: int,
        message_formatting: str = "markdown",
    ) -> SubAgentRun:
        """Run a Foundry query-API agent using agent-common's foundry module.

        Args:
            sub_agent_cfg: Result of _fetch_sub_agent_config() with foundry_* fields.
            prompt: The user message to process.
            user_config: Authenticated user context.
            scheduled_job_id: For logging.
            scheduled_job_run_id: For tracking the conversation.
            message_formatting: Rendering rules of the delivery channel. The query API
                takes a single `userInput` string and no system prompt, so they can only
                go into the prompt; whether the Foundry-side agent honours them is its
                own business, but a run that is never told cannot get it right.
        Returns:
            The run's message and terminal task state. Neither a Foundry nor a
            remote agent can park: only a local sub-agent's KIND_AUTH interrupt does.
        """
        # Build LocalFoundrySubAgentConfig from the backend response
        foundry_config = LocalFoundrySubAgentConfig(
            name=sub_agent_cfg["name"],
            description=sub_agent_cfg.get("description", ""),
            hostname=sub_agent_cfg.get("foundry_hostname", "https://blumen.palantirfoundry.de"),
            client_id=sub_agent_cfg["foundry_client_id"],
            client_secret_ref=sub_agent_cfg["foundry_client_secret_ssmkey"],
            ontology_rid=sub_agent_cfg["foundry_ontology_rid"],
            query_api_name=sub_agent_cfg["foundry_query_api_name"],
            scopes=sub_agent_cfg.get("foundry_scopes", []),
            version=sub_agent_cfg.get("foundry_version"),
        )

        user_dict = {
            "sub": user_config.user_sub,
            "name": user_config.name,
            "email": user_config.email,
        }

        compiled_subagent = create_foundry_local_subagent(
            config=foundry_config,
            user=user_dict,
            backend_url=_CONSOLE_BACKEND_URL,
            sub_agent_id=sub_agent_cfg.get("sub_agent_id"),
            sub_agent_config_version_id=sub_agent_cfg.get("sub_agent_config_version_id"),
        )

        formatting_block = formatting_prompt_block(message_formatting)
        foundry_prompt = f"{prompt}\n\n{formatting_block}" if formatting_block else prompt

        # Stream the foundry runnable via the A2A SubAgentInput interface
        input_data = SubAgentInput(
            messages=[{"role": "user", "content": foundry_prompt}],
        )
        run = await _collect_sub_agent_run(compiled_subagent["runnable"], input_data)
        result_summary, task_state = run.message, run.task_state

        logger.info(
            "Foundry agent execution complete for job %d: %d chars (task_state=%s)",
            scheduled_job_id,
            len(result_summary or ""),
            task_state,
        )
        return SubAgentRun(message=result_summary, task_state=task_state)

    def _get_oauth2_client(self) -> OidcOAuth2Client:
        """Lazily create an OAuth2 client for outbound A2A agent communication.

        Uses OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / OIDC_ISSUER — the dedicated
        agent-runner Keycloak client. This client is authorised for the
        token-exchange grant that SmartTokenInterceptor needs when calling
        remote A2A agents (e.g. voice-agent).
        """
        if self._oauth2_client is None:
            self._oauth2_client = OidcOAuth2Client(
                client_id=os.environ["OIDC_CLIENT_ID"],
                client_secret=os.environ["OIDC_CLIENT_SECRET"],
                issuer=os.environ["OIDC_ISSUER"],
            )
            logger.info("Initialized OAuth2 client for remote A2A communication")
        return self._oauth2_client

    async def _run_remote_agent(
        self,
        sub_agent_cfg: dict,
        raw_a2a_messages: list[Message],
        prompt: str,
        user_access_token: str,
        scheduled_job_id: int,
        scheduled_job_run_id: int,
        context_id: str | None = None,
        message_formatting: str = "markdown",
    ) -> SubAgentRun:
        """Run a remote A2A agent by discovering its agent card and invoking it.

        Uses lossless A2A→HumanMessage conversion so DataParts and TextParts
        from the scheduler engine are preserved end-to-end.  Falls back to
        plain text prompt when no raw messages are available.

        TODO: we could just pass the a2a message without the need of the whole A2AClientRunnable machinery,
              the A2AClientRunnable is needed just for the orchestrator in order work as a deepagents
              sub-agent. In case we would completely migrate the orchestrator to use the agent-runner, we need
              to consider this aspect carefully.
        Args:
            sub_agent_cfg: Result of _fetch_sub_agent_config() with agent_url.
            raw_a2a_messages: Original A2A messages with DataParts/TextParts intact.
            prompt: Fallback text prompt (used when raw_a2a_messages is empty).
            user_access_token: User's token for auth (passed to SmartTokenInterceptor).
            scheduled_job_id: For logging.
            scheduled_job_run_id: ID of the scheduled job run.
            context_id: The run task's own contextId. Sent as the outgoing A2A
                message's contextId so the remote agent checkpoints the run's
                conversation under an id this side actually stores
                (scheduled_job_runs.conversation_id) — the prerequisite for a
                later orchestrator delegation to resume that conversation via
                the conversation-origin extension.
            message_formatting: Rendering rules of the delivery channel, forwarded as
                A2A message metadata so the remote agent applies them itself.

        Returns:
            The run's message and terminal task state. Neither a Foundry nor a
            remote agent can park: only a local sub-agent's KIND_AUTH interrupt does.
        """
        agent_url: str | None = sub_agent_cfg.get("agent_url")
        if not agent_url:
            raise ValueError(f"Remote sub-agent '{sub_agent_cfg['name']}' has no agent_url configured")

        # Discover the remote agent's card
        agent_card_url = f"{agent_url.rstrip('/')}/.well-known/agent-card.json"
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            resp = await client.get(agent_card_url)
            resp.raise_for_status()
            # A2A v1.0+ uses protobuf AgentCard (ProtoJSON), parsed via ParseDict.
            agent_card = ParseDict(resp.json(), AgentCard(), ignore_unknown_fields=True)

        card_url = agent_card.supported_interfaces[0].url if agent_card.supported_interfaces else ""
        logger.info(
            "Discovered remote agent '%s' at %s for job %d",
            agent_card.name,
            card_url,
            scheduled_job_id,
        )

        # Create the A2A runnable with authentication
        oauth2_client = self._get_oauth2_client()
        config = A2AClientConfig(sub_agent_id=sub_agent_cfg.get("sub_agent_id"))
        runnable = make_a2a_async_runnable(
            agent_card,
            oauth2_client,
            user_token=user_access_token,
            config=config,
        )

        # Build HumanMessages from raw A2A messages (preserves DataParts + TextParts).
        # _from_human_messages_to_a2a in A2AClientRunnable natively converts
        # non_standard blocks → DataPart, text blocks → TextPart.
        human_messages = _a2a_messages_to_human_messages(raw_a2a_messages)
        if human_messages:
            messages_input: list = human_messages
        else:
            # Fallback to plain text prompt
            messages_input = [{"role": "user", "content": prompt}]

        # orchestrator_conversation_id feeds A2AClientRunnable's contextId
        # waterfall (_extract_tracking_ids), putting the run task's contextId on
        # the wire. The remote keys its checkpoints by the contextId it
        # receives, so the run's stored conversation_id then names a real,
        # resumable conversation on the executing side.
        # The channel's rendering rules travel as message metadata, not as an extra
        # message: a remote agent owns its system prompt, and A2AClientRunnable puts
        # `messageFormatting` on the wire under the key an interactive client uses, so the
        # remote applies them through its own request-metadata path. Appending an
        # instruction message instead would land in the remote's checkpointed
        # conversation, where a later turn can read it as part of the task.
        input_data = SubAgentInput(
            messages=messages_input,
            scheduled_job_id=scheduled_job_id,
            orchestrator_conversation_id=context_id,
            # Only when there is something to say: plain Markdown is the remote's default
            # too, so sending it would put a no-op instruction on the wire.
            message_formatting=message_formatting if formatting_rules(message_formatting) else None,
        )
        run = await _collect_sub_agent_run(runnable, input_data)
        result_summary, task_state = run.message, run.task_state

        logger.info(
            "Remote agent execution complete for job %d: %d chars (task_state=%s)",
            scheduled_job_id,
            len(result_summary or ""),
            task_state,
        )
        return SubAgentRun(message=result_summary, task_state=task_state)
