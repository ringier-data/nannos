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
from collections.abc import AsyncIterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from a2a.types import AgentCard, Message, Task, TaskState
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.factory import make_a2a_async_runnable
from agent_common.a2a.models import LocalFoundrySubAgentConfig
from agent_common.a2a.stream_events import ArtifactUpdate, ErrorEvent, TaskResponseData, TaskUpdate
from agent_common.a2a.structured_response import A2A_PROTOCOL_ADDENDUM, SubAgentResponseSchema, get_response_format
from agent_common.agents.foundry_agent import create_foundry_local_subagent
from agent_common.core.document_store_tools import create_document_store_tools
from agent_common.core.graph_utils import build_sub_agent_graph
from agent_common.core.model_factory import (
    create_model,
    get_default_model,
    is_valid_model,
    require_default_model,
)
from agent_common.core.stream_watchdog import watch_stream_with_resume
from agent_common.core.token_provider import DEFAULT_LEEWAY_S, UserTokenProvider
from agent_common.core.tool_catalogue import sanitize_tool_name
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Struct
from object_storage import get_object_storage_service

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

from agent.mcp_tools import McpToolResolver

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
_MAX_RECURSION_LIMIT = int(os.getenv("MAX_RECURSION_LIMIT", "50"))


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


def _extract_message_metadata(task: Task) -> dict[str, Any]:
    """Extract scheduler metadata from the A2A task's message history.

    The scheduler engine injects metadata (user_access_token, sub_agent_id,
    scheduled_job_id, scheduled_job_run_id) into the A2A message.
    These end up in task.history[-1].metadata when the message is processed.

    SECURITY NOTE: user_id is NOT extracted from message metadata as it would be
    unverified user input. Instead, fetch it from agent-console backend using the
    verified user_sub from JWT authentication.

    Args:
        task: The A2A Task object from the executor.

    Returns:
        Dict of scheduler metadata, or empty dict if not found.
    """
    try:
        if task.history:
            last_msg = task.history[-1]
            if hasattr(last_msg, "metadata") and last_msg.metadata:
                meta = last_msg.metadata
                # Over gRPC the metadata is a protobuf Struct; dict() would only
                # convert the top level, leaving nested values as
                # Structs that support ["key"] but not .get(). Convert the whole
                # tree to plain Python instead.
                if isinstance(meta, Struct):
                    return MessageToDict(meta)
                return dict(meta)
    except Exception:
        pass
    return {}


# A2A task states worth reporting as a run's terminal task_state (see
# _collect_stream_text). Non-terminal states (working, ...) map to None:
# they carry no information about how the run ended.
_TERMINAL_TASK_STATE_NAMES = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_FAILED: "failed",
}


async def _collect_stream_text(runnable: Any, input_data: SubAgentInput) -> tuple[str | None, str | None]:
    """Collect the final text result from an A2A runnable's stream.

    Accumulates non-intermediate ``ArtifactUpdate`` content (the main
    response chunks).  Falls back
    to extracting text from the last ``TaskResponseData`` messages when
    neither artifact nor message content was streamed.

    Returns ``(text, task_state)``: the accumulated text (None if the stream
    produced no readable content) and the run's terminal task state as a
    scheduler-facing string (``completed`` | ``input_required`` | ``failed``),
    or None when the stream never reported one. ``input_required`` matters
    downstream: it tells a conversation adopting this run that the sub-agent
    asked the user a question and is waiting for the answer.
    """
    parts: list[str] = []
    last_data: TaskResponseData = TaskResponseData()

    async for item in runnable.astream(input_data.model_dump()):
        if isinstance(item, ArtifactUpdate) and item.event_metadata is None:
            if item.content:
                parts.append(item.content)
        elif isinstance(item, TaskUpdate):
            last_data = item.data
        elif isinstance(item, ErrorEvent):
            return (f"Error: {item.error}" if item.error else None), "failed"

    task_state = _TERMINAL_TASK_STATE_NAMES.get(last_data.state)

    if parts:
        return ("".join(parts).strip() or None), task_state

    # Fallback: extract text from the last TaskResponseData messages
    return _extract_text_from_messages(last_data.messages), task_state


def _extract_text_from_messages(messages: list) -> str | None:
    """Extract human-readable text from A2A response messages.

    Messages produced by ``_wrap_message_with_metadata`` are AIMessages
    whose ``content`` is a JSON string ``{"content": "...", "a2a": {...}}``.
    This helper unwraps that JSON, falling back to plain text content.
    """
    for msg in reversed(messages):
        raw = getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                text = json.loads(raw).get("content", "")
            except (json.JSONDecodeError, AttributeError):
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
        message_meta = _extract_message_metadata(task)

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

        # Correlation ids echoed back in every result so the delivery channel can
        # link a notification (and later thread replies) to this job/run/sub-agent.
        correlation_meta = {
            "scheduled_job_id": scheduled_job_id,
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
                agent_message, sub_agent_task_state = await self._execute_sub_agent(
                    sub_agent_cfg=sub_agent_cfg,
                    prompt=prompt,
                    raw_a2a_messages=messages,
                    user_access_token=user_access_token,
                    scheduled_job_id=scheduled_job_id,
                    scheduled_job_run_id=scheduled_job_run_id,
                    user_config=user_config,
                    user_id=user_id,
                    context_id=task.context_id,
                )
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

        result_meta = {
            "scheduler_status": "success",
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
            "user_sub": user_config.user_sub,
            "sub_agent_name": sub_agent_name,
            "prompt": prompt,
            **correlation_meta,
        }
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
    ) -> tuple[str | None, str | None]:
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

        Returns:
            (agent_message, task_state) — task_state is the sub-agent's
            terminal A2A task state ("completed" | "input_required" |
            "failed") when it reported one, else None. It rides the result
            metadata so a conversation later adopting this run knows whether
            the run finished or is waiting for the user's answer.
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
            )
        elif agent_type == "foundry":
            return await self._run_foundry_agent(
                sub_agent_cfg=sub_agent_cfg,
                prompt=prompt,
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
    ) -> tuple[str | None, str | None]:
        """Run a one-shot LangGraph agent using agent-common's model factory.

        Uses create_model() for multi-provider support (Bedrock, OpenAI, Google)
        instead of hardcoded ChatBedrockConverse.

        Args:
            sub_agent_cfg: Result of _fetch_sub_agent_config().
            prompt: The user message to process.
            user_access_token: Token passed through to the MCP gateway.
            user_sub: OIDC subject identifier for cost tracking.
            scheduled_job_id: The ID of the scheduled job.
            scheduled_job_run_id: The ID of the scheduled job run, used for checkpoint isolation and logging.
            user_id: Verified database user UUID (fetched from backend, used for docstore namespace).
            context_id: Natural A2A context_id for thread isolation (conversation_id).

        Returns:
            (agent_message, task_state) — task_state is the sub-agent's
            structured-response state ("completed" | "input_required" |
            "failed"), or None when no structured response was produced.
        """
        # Ensure the document store is ready before building the graph (which binds self.store).
        # Idempotent and cheap once set up; on a cold start it retries until the gateway/embedding
        # default resolves, so semantic memory self-heals without a restart.
        await self.ensure_store_ready()

        system_prompt: str = sub_agent_cfg["system_prompt"]
        mcp_tool_names: list[str] = sub_agent_cfg["mcp_tools"]
        model_name: str = sub_agent_cfg["model"]

        # Validate and create LLM via agent-common model factory
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

        # Determine thinking level
        thinking_level = None
        if sub_agent_cfg.get("enable_thinking") and sub_agent_cfg.get("thinking_level"):
            thinking_level = sub_agent_cfg["thinking_level"]

        llm = create_model(model_name, thinking_level=thinking_level)

        # Append the A2A response protocol addendum so the LLM knows to use SubAgentResponseSchema
        full_system_prompt = system_prompt + "\n\n" + A2A_PROTOCOL_ADDENDUM

        mcp_timeout = timedelta(seconds=_MCP_TIMEOUT_SECONDS)

        # Use natural A2A context_id as thread_id for conversation tracking.
        # context_id should always be present in A2A protocol - fail loudly if missing.
        if not context_id:
            raise ValueError(f"Missing context_id in A2A task for scheduled job {scheduled_job_id}")

        thread_id = context_id

        result_summary: str | None = None
        task_state: str | None = None

        # Sandbox lifecycle
        sandbox_active = sub_agent_cfg.get("sandbox_enabled", False) and self._sandbox_pool is not None
        if sub_agent_cfg.get("sandbox_enabled", False) and not self._sandbox_pool:
            logger.warning(
                "Sub-agent '%s' has sandbox_enabled=true but no SANDBOX_PROVIDER configured; "
                "running without sandbox for job %s",
                sub_agent_cfg["name"],
                scheduled_job_id,
            )

        pooled_sandbox = None

        async def _run_graph(tools: list) -> None:
            nonlocal result_summary, task_state, pooled_sandbox

            extra_middlewares = None
            sandbox_backend_factory = None

            if sandbox_active:
                pooled_sandbox = await self._sandbox_pool.acquire(thread_id, sub_agent_cfg["name"])

                from agent_common.core.graph_utils import create_sandboxed_backend_factory
                from deepagents.backends import StateBackend

                sandbox_backend_factory = create_sandboxed_backend_factory(
                    sandbox_backend=pooled_sandbox.backend,
                    base_backend=StateBackend(),
                )

            # Determine structured output strategy (mutates tools in-place for Bedrock/Anthropic+thinking)
            response_format = get_response_format(model_name, tools, thinking_enabled=bool(thinking_level))

            graph = build_sub_agent_graph(
                model=llm,
                tools=tools,
                system_prompt=full_system_prompt,
                checkpointer=self._checkpointer,
                store=self.store,
                cost_logger=self._cost_logger,
                response_format=response_format,
                exclude_deep_agents_middlewares=False,
                backend_factory=sandbox_backend_factory,
                extra_middlewares=extra_middlewares,
            ).with_config({"recursion_limit": _MAX_RECURSION_LIMIT})

            config = self.create_runnable_config(
                user_sub=user_sub,
                conversation_id=thread_id,
                thread_id=thread_id,
                scheduled_job_id=scheduled_job_id,
                sub_agent_id=sub_agent_cfg["sub_agent_id"],
                sub_agent_config_version_id=sub_agent_cfg.get("sub_agent_config_version_id"),
            )
            # Inject metadata consumed by IndexingStoreBackend and document-store tools.
            # user_id  — verified database UUID (fetched from backend) for docstore namespace.
            # assistant_id — scopes the filesystem namespace per-user (mirrors personal
            #               conversation scope used by the orchestrator when no Slack channel).
            if self.store is not None:
                config["metadata"] = {
                    "user_id": user_id or user_sub,
                    "assistant_id": user_id or user_sub,
                }
            # For local LLM execution, convert all parts (including DataParts) to text.
            # text_only=True serializes DataParts as JSON strings which LLMs can read.
            # (NonStandardContentBlock from text_only=False is rejected by Bedrock Converse)
            if raw_a2a_messages:
                text_content = "\n".join(
                    a2a_parts_to_content(msg.parts, text_only=True) for msg in raw_a2a_messages if msg.parts
                ).strip()
                messages = [HumanMessage(content=text_content)] if text_content else [HumanMessage(content=prompt)]
            else:
                messages = [HumanMessage(content=prompt)]

            # Use astream for proper streaming support (respects recursion_limit set with .with_config())
            # stream_mode="values" with version="v2" yields StreamPart dicts:
            #   {"type": "values", "ns": (), "data": <state snapshot>}
            # We consume all and use the final state.
            final_state = None
            # Auto-resume once on a watchdog stall (e.g. slow cold-cache prompt
            # ingestion tripping the first-token budget) instead of hard-failing the
            # whole sub-agent run; the checkpointer (Postgres, or the MemorySaver
            # placeholder) lets the resume pick up pending work with input=None.
            async for part in watch_stream_with_resume(
                lambda resuming: graph.astream(
                    None if resuming else {"messages": messages}, config=config, stream_mode="values", version="v2"
                ),
                label="agent-runner",
            ):
                if part["type"] == "values":
                    final_state = part["data"]
                # Future: could yield progress events here for streaming execution

            output_messages = final_state.get("messages", []) if final_state else []

            # 1. Check for structured_response (AutoStrategy / ToolStrategy output)
            structured_response = final_state.get("structured_response") if final_state else None
            if structured_response and isinstance(structured_response, SubAgentResponseSchema):
                result_summary = structured_response.message
                task_state = structured_response.task_state
            elif isinstance(output_messages, list):
                # 2. Check message tool_calls for SubAgentResponseSchema (Bedrock + thinking)
                for msg in reversed(output_messages):
                    if hasattr(msg, "tool_calls"):
                        for tool_call in msg.tool_calls:
                            if tool_call.get("name") == "SubAgentResponseSchema":
                                try:
                                    schema = SubAgentResponseSchema(**tool_call.get("args", {}))
                                    result_summary = schema.message
                                    task_state = schema.task_state
                                except Exception:
                                    pass
                    if result_summary:
                        break

                # # 3. Fallback: plain AIMessage text content
                # if not result_summary:
                #     for msg in reversed(output_messages):
                #         if isinstance(msg, AIMessage) and msg.content:
                #             content = msg.content
                #             if isinstance(content, list):
                #                 result_summary = " ".join(
                #                     c.get("text", "")
                #                     for c in content
                #                     if isinstance(c, dict) and c.get("type") == "text"
                #                 ).strip()
                #             elif isinstance(content, str):
                #                 result_summary = content.strip()
                #             if result_summary:
                #                 break

        # Build docstore tools if postgres store is configured
        docstore_tools: list = []
        if self.store is not None and _DOCUMENT_STORE_S3_BUCKET:
            # Use verified database user_id (fetched from backend) to match orchestrator's namespace.
            # Fall back to user_sub if backend fetch failed or user_id is None.
            docstore_user_id = user_id or user_sub
            docstore_tools = create_document_store_tools(
                store=self.store,
                storage=get_object_storage_service(),
                s3_bucket=_DOCUMENT_STORE_S3_BUCKET,
                user_id=docstore_user_id,
            )
            logger.info(
                "Added %d docstore tools for job %s: %s",
                len(docstore_tools),
                scheduled_job_id,
                [t.name for t in docstore_tools],
            )

        try:
            if mcp_tool_names:
                # Tools come from the shared catalogue (stateless tools/list, SDK fallback) as
                # LazyMcpTools on token-free connections; a per-run UserTokenProvider mints
                # the bearer at call time, so a token expiring mid-run is re-exchanged.
                resolver = McpToolResolver(
                    token_provider=UserTokenProvider(
                        user_access_token,
                        self._get_oauth2_client().exchange_token,
                        leeway_seconds=_MCP_TOKEN_LEEWAY_SECONDS,
                    ),
                    gateway_url=_MCP_GATEWAY_URL,
                    gateway_client_id=_MCP_GATEWAY_CLIENT_ID,
                    console_mcp_url=f"{_CONSOLE_BACKEND_URL}/mcp",
                    console_client_id=_CONSOLE_BACKEND_CLIENT_ID,
                    timeout=mcp_timeout,
                    stateless_list=_MCP_CATALOGUE_STATELESS_LIST,
                )
                tools = await resolver.resolve(mcp_tool_names)  # logs what was resolved and how
                await _run_graph(tools + docstore_tools)
            else:
                await _run_graph(docstore_tools)
        finally:
            if pooled_sandbox is not None and self._sandbox_pool is not None:
                await self._sandbox_pool.release(thread_id, sub_agent_cfg["name"])

        logger.info(
            "LangGraph agent execution complete for job %s: %d chars (task_state=%s)",
            scheduled_job_id,
            len(result_summary or ""),
            task_state,
        )
        return result_summary, task_state

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
    ) -> tuple[str | None, str | None]:
        """Run a Foundry query-API agent using agent-common's foundry module.

        Args:
            sub_agent_cfg: Result of _fetch_sub_agent_config() with foundry_* fields.
            prompt: The user message to process.
            user_config: Authenticated user context.
            scheduled_job_id: For logging.
            scheduled_job_run_id: For tracking the conversation.
        Returns:
            (result_summary, task_state) — see _collect_stream_text.
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

        # Stream the foundry runnable via the A2A SubAgentInput interface
        input_data = SubAgentInput(
            messages=[{"role": "user", "content": prompt}],
        )
        result_summary, task_state = await _collect_stream_text(compiled_subagent["runnable"], input_data)

        logger.info(
            "Foundry agent execution complete for job %d: %d chars (task_state=%s)",
            scheduled_job_id,
            len(result_summary or ""),
            task_state,
        )
        return result_summary, task_state

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
    ) -> tuple[str | None, str | None]:
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

        Returns:
            (result_summary, task_state) — see _collect_stream_text.
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
        input_data = SubAgentInput(
            messages=messages_input,
            scheduled_job_id=scheduled_job_id,
            orchestrator_conversation_id=context_id,
        )
        result_summary, task_state = await _collect_stream_text(runnable, input_data)

        logger.info(
            "Remote agent execution complete for job %d: %d chars (task_state=%s)",
            scheduled_job_id,
            len(result_summary or ""),
            task_state,
        )
        return result_summary, task_state
