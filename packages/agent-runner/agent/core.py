"""AgentRunner - A2A-pattern agent for executing scheduled sub-agent jobs.

Supports all sub-agent types:
- **automated/local**: LangGraph agents with MCP tools (multi-provider via agent-common model factory)
- **foundry**: Palantir Foundry query-API agents
- **remote**: A2A protocol agents at external URLs

Follows the same A2A pattern as agent-creator and alloy-agent:
- Extends BaseAgent and implements _stream_impl()
- JWT authentication enforced at the middleware layer
- Result is returned as JSON-encoded text in the artifact (for scheduler engine parsing)

Execution flow per call:
1. Extract scheduler metadata from the A2A message (task.history)
2. For watch jobs: call the check_tool via MCP and evaluate the JSONPath condition
3. If condition met (or task job): fetch sub-agent config from agent-console backend and
   dispatch to the appropriate agent runner (LangGraph / Foundry / remote A2A),
   capture result
4. Yield AgentStreamResponse with JSON-encoded result metadata
   (the scheduler engine handles push-notification delivery on its side)
"""

import json
import logging
import os
from collections.abc import AsyncIterable
from datetime import timedelta
from typing import Any

import httpx
from a2a.types import AgentCard, Message, Task, TaskState
from agent_common.a2a.base import SubAgentInput
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.factory import make_a2a_async_runnable
from agent_common.a2a.models import LocalFoundrySubAgentConfig
from agent_common.a2a.stream_events import ArtifactUpdate, ErrorEvent, TaskResponseData, TaskUpdate
from agent_common.a2a.structured_response import A2A_PROTOCOL_ADDENDUM, SubAgentResponseSchema, get_response_format
from agent_common.agents.foundry_agent import create_foundry_local_subagent
from agent_common.core.cost_tracking_embeddings import CostTrackingBedrockEmbeddings
from agent_common.core.document_store_tools import create_document_store_tools
from agent_common.core.graph_utils import build_sub_agent_graph
from agent_common.core.model_factory import DEFAULT_MODEL, _has_aws_credentials, create_model, is_valid_model
from agent_common.core.s3_service import get_s3_service
from jsonpath_ng.ext import parse as jsonpath_parse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph_checkpoint_aws import DynamoDBSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel as PydanticBaseModel
from ringier_a2a_sdk.agent import BaseAgent
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig
from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

logger = logging.getLogger(__name__)

_PLAYGROUND_BACKEND_URL = os.getenv("PLAYGROUND_BACKEND_URL", "http://localhost:5001")
_MCP_GATEWAY_URL = os.getenv("MCP_GATEWAY_URL", "https://alloych.gatana.ai/mcp")
_MCP_TIMEOUT_SECONDS = int(os.getenv("MCP_TIMEOUT_SECONDS", "300"))
_DOCUMENT_STORE_S3_BUCKET = os.getenv("DOCUMENT_STORE_S3_BUCKET", "")
_MAX_RECURSION_LIMIT = int(os.getenv("MAX_RECURSION_LIMIT", "50"))


# Structured output models for LLM operations
class ConditionEvaluationResult(PydanticBaseModel):
    """Structured output for LLM-based condition evaluation."""

    condition_met: bool
    reasoning: str


class GeneratedMessage(PydanticBaseModel):
    """Structured output for LLM-generated notification message."""

    message: str


def _build_postgres_conn() -> str | None:
    """Build a PostgreSQL connection string from environment variables.

    Returns None if POSTGRES_HOST is not set, disabling the document store.
    """
    host = os.getenv("POSTGRES_HOST")
    if not host:
        return None
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "playground")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _create_checkpointer() -> DynamoDBSaver | MemorySaver:
    """Create a checkpointer: DynamoDB if configured, else in-memory fallback."""
    checkpoint_table = os.getenv("CHECKPOINT_DYNAMODB_TABLE_NAME")
    if not checkpoint_table:
        logger.warning(
            "CHECKPOINT_DYNAMODB_TABLE_NAME not set — using in-memory checkpointer. "
            "Conversation history will be lost on restart."
        )
        return MemorySaver()

    checkpoint_region = os.getenv("CHECKPOINT_AWS_REGION", "eu-central-1")
    checkpoint_ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
    checkpoint_compression = os.getenv("CHECKPOINT_COMPRESSION_ENABLED", "true").lower() == "true"
    checkpoint_s3_bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")

    s3_config = {"bucket_name": checkpoint_s3_bucket} if checkpoint_s3_bucket else None

    return DynamoDBSaver(
        table_name=checkpoint_table,
        region_name=checkpoint_region,
        ttl_seconds=checkpoint_ttl_days * 24 * 60 * 60,
        enable_checkpoint_compression=checkpoint_compression,
        s3_offload_config=s3_config,
    )


def _extract_text_from_message(message: Message) -> str:
    """Extract text content from an A2A Message's parts."""
    texts = []
    for part in message.parts or []:
        if hasattr(part, "root") and hasattr(part.root, "text"):
            texts.append(part.root.text)
        elif hasattr(part, "text"):
            texts.append(part.text)
    return "\n".join(texts).strip()


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


def _extract_message_metadata(task: Task) -> dict[str, Any]:
    """Extract scheduler metadata from the A2A task's message history.

    The scheduler engine injects metadata (user_access_token, sub_agent_id,
    watch, job_type, scheduled_job_id, scheduled_job_run_id) into the A2A message.
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
                return dict(last_msg.metadata)
    except Exception:
        pass
    return {}


async def _collect_stream_text(runnable: Any, input_data: SubAgentInput) -> str | None:
    """Collect the final text result from an A2A runnable's stream.

    Accumulates non-intermediate ``ArtifactUpdate`` content (the main
    response chunks).  Falls back
    to extracting text from the last ``TaskResponseData`` messages when
    neither artifact nor message content was streamed.

    Returns the accumulated text, or None if the stream produced no
    readable content.
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
            return f"Error: {item.error}" if item.error else None

    if parts:
        return "".join(parts).strip() or None

    # Fallback: extract text from the last TaskResponseData messages
    return _extract_text_from_messages(last_data.messages)


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
        self._checkpointer = _create_checkpointer()
        self._oauth2_client: OidcOAuth2Client | None = None
        # Enable cost tracking so get_langchain_callbacks() works for LangGraph runs.
        # report_usage() is overridden as a no-op below to avoid a spurious "requests: 1"
        # entry being logged for the agent-runner dispatcher itself.
        backend_url = os.getenv("PLAYGROUND_BACKEND_URL")
        if backend_url:
            try:
                self.enable_cost_tracking(backend_url=backend_url)
                logger.info("AgentRunner: cost tracking enabled")
            except Exception as ct_err:
                logger.warning(f"AgentRunner: failed to enable cost tracking: {ct_err}")

        # Document store (PostgreSQL + pgvector) — optional, shared with orchestrator.
        # Disabled when POSTGRES_HOST is not configured.
        self._postgres_conn: str | None = _build_postgres_conn()
        self._store: AsyncPostgresStore | None = None
        self._connection_pool: AsyncConnectionPool | None = None
        if self._postgres_conn:
            if _has_aws_credentials():
                self._embeddings_model = CostTrackingBedrockEmbeddings(
                    model_id="amazon.titan-embed-text-v2:0",
                    region_name=os.getenv("AWS_BEDROCK_REGION", "eu-central-1"),
                    cost_logger=getattr(self, "_cost_logger", None),
                )
                logger.info("AgentRunner: document store configured (PostgreSQL)")
            else:
                self._embeddings_model = None
                self._postgres_conn = None
                logger.warning("AgentRunner: document store disabled (no AWS credentials for embeddings)")
        else:
            self._embeddings_model = None
            logger.info("AgentRunner: document store disabled (POSTGRES_HOST not set)")

    @property
    def store(self) -> AsyncPostgresStore | None:
        """Lazy-initialise the shared AsyncPostgresStore.

        Returns None when POSTGRES_HOST is not configured so the graph runs
        without a persistent store (tools that rely on docstore simply won't
        be available).
        """
        if not self._postgres_conn:
            return None
        if self._store is None:
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
            self._store = AsyncPostgresStore(
                conn=self._connection_pool,
                index={
                    "dims": 1024,  # Titan Embeddings V2
                    "embed": self._embeddings_model,
                    "fields": ["contextualized_content"],  # description + chunk text combined, ≤50k chars
                },
            )
            logger.info("Initialised AsyncPostgresStore (Titan Embeddings V2, 1024 dims)")
        return self._store

    async def ensure_store_setup(self) -> None:
        """Open the connection pool and run store schema migrations.

        Safe to call multiple times — subsequent calls are no-ops.
        Should be called once from the application lifespan before serving requests.
        """
        if not self._postgres_conn:
            return
        store = self.store
        if store is None:
            return
        if self._connection_pool is not None and not self._connection_pool._opened:
            await self._connection_pool.open()
            logger.info("Opened AsyncConnectionPool for document store")
        try:
            await store.setup()
            logger.info("Document store schema ready")
        except Exception as exc:
            logger.warning(f"Document store setup failed (continuing without): {exc}")

    async def close(self) -> None:
        """Clean up resources."""
        if self._connection_pool is not None and self._connection_pool._opened:
            await self._connection_pool.close()
            logger.info("Closed document store connection pool")

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
        yield AgentStreamResponse(state=TaskState.working, content="Executing scheduled job...")

        # Extract scheduler-specific metadata from the message
        message_meta = _extract_message_metadata(task)

        sub_agent_id: int | None = message_meta.get("sub_agent_id")
        job_type: str = message_meta.get("job_type", "task")
        watch: dict | None = message_meta.get("watch")
        scheduled_job_id: int = message_meta.get("scheduled_job_id", 0)
        scheduled_job_run_id: int = message_meta.get("scheduled_job_run_id", "")

        # SECURITY: Use verified access token from JWT (validated by JWTValidatorMiddleware)
        # and fetch user_id from backend API to prevent privilege escalation
        user_access_token = user_config.access_token.get_secret_value() if user_config.access_token else ""
        user_id: str | None = await self._fetch_user_id_from_backend(user_access_token) if user_access_token else None

        # For tasks: query contains the prompt for the sub-agent
        # For watches: query contains the agent_message (what the agent delivers to user)
        # TODO: what about multi-modal inputs (files, data) in the message parts?
        #       For now we only extract text from the messages.
        message_text = "\n".join(_extract_text_from_message(m) for m in messages).strip()
        last_check_result: dict | None = None
        agent_message: str | None = None

        # --- 1. Watch condition evaluation (skips LLM if condition not met) ---
        if job_type == "watch" and watch:
            condition_met, check_result = await self._evaluate_watch(watch, user_access_token)
            last_check_result = check_result
            if not condition_met:
                logger.info(f"Watch condition NOT met for job {scheduled_job_id} — skipping execution")
                result_meta = {
                    "scheduler_status": "condition_not_met",
                    "last_check_result": last_check_result,
                }
                yield AgentStreamResponse(
                    state=TaskState.completed,
                    content=json.dumps(result_meta, default=str),
                )
                return

            # Generate agent message if none was provided
            # This is what gets delivered to the user
            agent_message = message_text or await self._generate_watch_message(check_result, user_config.user_sub)
            logger.info(f"Watch notification: {agent_message[:100]}...")

        # --- 2. Sub-agent execution (dispatched by type) ---
        if sub_agent_id:
            # For tasks, use the message_text as the prompt
            # For watches with sub-agents, use a generic prompt (notification is separate)
            if job_type == "watch":
                prompt = f"Watch condition triggered. Take appropriate action based on: {json.dumps(last_check_result, default=str)}"
            else:
                prompt = message_text or "Execute your configured task."

            try:
                agent_message = await self._execute_sub_agent(
                    sub_agent_id=sub_agent_id,
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
                    "last_check_result": last_check_result,
                    "agent_message": agent_message,
                }
                yield AgentStreamResponse(
                    state=TaskState.failed,
                    content=json.dumps(result_meta, default=str),
                )
                return

        result_meta = {
            "scheduler_status": "success",
            "agent_message": agent_message,
            "last_check_result": last_check_result,
        }
        yield AgentStreamResponse(
            state=TaskState.completed,
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
                    f"{_PLAYGROUND_BACKEND_URL}/api/v1/auth/me",
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

    async def _evaluate_watch(self, watch: dict, user_access_token: str) -> tuple[bool, dict]:
        """Call the check_tool via MCP gateway and evaluate the condition.

        Args:
            watch: Dict with keys check_tool, check_args, condition_expr, expected_value, llm_condition, last_check_result.
            user_access_token: orchestrator token for MCP gateway authentication.

        Returns:
            (condition_met, check_result_dict)
        """
        check_tool: str = watch["check_tool"]
        check_args: dict = watch.get("check_args") or {}
        condition_expr: str | None = watch.get("condition_expr")
        expected_value: str | None = watch.get("expected_value")
        llm_condition: str | None = watch.get("llm_condition")
        # token exchange: orchestrator -> gatana
        gatana_access_token = await (self._get_oauth2_client()).exchange_token(user_access_token, "gatana")

        mcp_timeout = timedelta(seconds=_MCP_TIMEOUT_SECONDS)
        connection = StreamableHttpConnection(
            transport="streamable_http",
            url=_MCP_GATEWAY_URL,
            headers={"Authorization": f"Bearer {gatana_access_token}"},
            timeout=mcp_timeout,
            sse_read_timeout=mcp_timeout,
        )

        check_result: dict = {}
        try:
            mcp_client = MultiServerMCPClient({"gateway": connection})
            async with mcp_client.session("gateway") as session:
                tools = await load_mcp_tools(session)
                tool_map = {t.name: t for t in tools}

                if check_tool not in tool_map:
                    raise ValueError(f"Watch check_tool '{check_tool}' not found in MCP gateway")

                raw = await tool_map[check_tool].ainvoke(check_args)
                # ainvoke returns list[TextContentBlock | ImageContentBlock | FileContentBlock]
                # (langchain_core TypedDicts, already converted from raw MCP content blocks).
                if isinstance(raw, list):
                    text_parts: list[str] = [
                        block["text"] for block in raw if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    combined = "\n".join(text_parts) if text_parts else ""
                    try:
                        check_result = json.loads(combined) if combined else {}
                    except json.JSONDecodeError:
                        check_result = {"output": combined}
                elif isinstance(raw, dict):
                    check_result = raw
                elif isinstance(raw, str):
                    try:
                        check_result = json.loads(raw)
                    except json.JSONDecodeError:
                        check_result = {"output": raw}
                else:
                    check_result = {"output": str(raw)}

        except Exception as exc:
            logger.exception("Watch check_tool '%s' call failed", check_tool)
            raise RuntimeError(f"Watch check failed: {exc}") from exc

        # Evaluate the JSONPath condition expression
        if not condition_expr:
            return True, check_result

        try:
            expr = jsonpath_parse(condition_expr)
            matches = expr.find(check_result)

            # Extract the value from JSONPath
            extracted_value = None
            if matches:
                extracted_value = matches[0].value if len(matches) == 1 else [m.value for m in matches]

            # Evaluate condition: LLM takes precedence if provided
            if llm_condition:
                # Use LLM-based evaluation with GPT-4o-mini
                try:
                    llm = create_model("gpt-4o-mini").bind(temperature=0)
                    structured_llm = llm.with_structured_output(ConditionEvaluationResult)

                    system_prompt = (
                        "You are a condition evaluator for a scheduling system. "
                        "Evaluate whether the given condition is met based on the provided data. "
                        "Be precise and objective in your evaluation."
                    )

                    user_prompt = f"""Evaluate this condition:
{llm_condition}

Extracted value from JSONPath:
{json.dumps(extracted_value, indent=2)}

Full tool response:
{json.dumps(check_result, indent=2)}

Evaluate whether the condition is met and provide brief reasoning."""

                    # Cost tracking callback (if cost logger is available)
                    callbacks = self.get_langchain_callbacks() or []

                    messages = [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ]

                    result: ConditionEvaluationResult = await structured_llm.ainvoke(
                        messages, config={"callbacks": callbacks}
                    )
                    condition_met = result.condition_met
                    logger.info(
                        "LLM condition '%s' met=%s (reasoning: %s)",
                        llm_condition,
                        condition_met,
                        result.reasoning,
                    )
                except Exception as exc:
                    logger.error("LLM condition evaluation failed: %s", exc)
                    condition_met = False
            elif expected_value is not None:
                # Use exact string comparison
                extracted_str = str(extracted_value) if extracted_value is not None else ""
                condition_met = extracted_str.lower() == expected_value.lower()
            else:
                # Default: check that extracted value is not null/empty
                condition_met = extracted_value is not None and extracted_value not in ("", 0, False, [], {})

        except Exception as exc:
            logger.error("JSONPath condition '%s' evaluation failed: %s", condition_expr, exc)
            condition_met = False

        logger.info("Watch condition met=%s", condition_met)
        return condition_met, check_result

    async def _generate_watch_message(self, check_result: dict, user_sub: str) -> str:
        """Generate a notification message using LLM when no explicit message was provided.

        Args:
            check_result: The watch check result dictionary from the MCP tool.
            user_sub: User subject from JWT for cost tracking (fallback when user_id unavailable).

        Returns:
            Generated notification message string.
        """
        try:
            llm = create_model("gpt-4o-mini").bind(temperature=0.7)  # Slightly creative for message generation
            structured_llm = llm.with_structured_output(GeneratedMessage)

            # Cost tracking callback (if cost logger is available)
            callbacks = self.get_langchain_callbacks() or []

            system_prompt = (
                "You are a notification message generator for a scheduling system. "
                "Generate clear, concise, and informative notification messages based on watch condition results. "
                "The message should be human-readable and highlight the key information from the result."
            )

            user_prompt = f"""Generate a notification message for this watch condition result:

{json.dumps(check_result, indent=2)}

Create a brief, actionable message (1-2 sentences) that a user would want to receive as a notification."""

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ]

            result: GeneratedMessage = await structured_llm.ainvoke(messages, config={"callbacks": callbacks})
            logger.info("Generated watch message: %s", result.message)
            return result.message
        except Exception as exc:
            logger.error("LLM message generation failed: %s", exc)
            # Fallback to a simple default message
            return f"Watch condition triggered. Result: {json.dumps(check_result, default=str)[:200]}"

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
        url = f"{_PLAYGROUND_BACKEND_URL}/api/v1/sub-agents/{sub_agent_id}"
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
            "description": cfg_version.get("description", ""),
            "system_prompt": cfg_version.get("system_prompt", ""),
            "mcp_tools": cfg_version.get("mcp_tools") or [],
            "model": cfg_version.get("model") or DEFAULT_MODEL,
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
        }

    async def _execute_sub_agent(
        self,
        sub_agent_id: int,
        prompt: str,
        user_access_token: str,
        scheduled_job_id: int,
        scheduled_job_run_id: int,
        user_config: UserConfig,
        user_id: str | None = None,
        context_id: str | None = None,
        raw_a2a_messages: list[Message] | None = None,
    ) -> str | None:
        """Fetch sub-agent config and dispatch to the appropriate execution method.

        Args:
            sub_agent_id: ID of the sub-agent to run.
            prompt: The user message to process (used for local/foundry agents).
            user_access_token: Token passed through for authentication.
            scheduled_job_id: The ID of the scheduled job.
            scheduled_job_run_id: The ID of the scheduled job run, used for checkpoint isolation and logging.
            user_config: Authenticated user context.
            user_id: Verified database user UUID (fetched from backend, not from message metadata).
            context_id: Natural A2A context_id for thread isolation (conversation_id).
            raw_a2a_messages: Original A2A messages (used for remote agents to preserve DataParts).

        Returns:
            agent_message (str | None)
        """
        sub_agent_cfg = await self._fetch_sub_agent_config(sub_agent_id, user_access_token)
        agent_type = sub_agent_cfg["type"]

        if agent_type in ("automated", "local"):
            return await self._run_langgraph_agent(
                sub_agent_cfg=sub_agent_cfg,
                prompt=prompt,
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
            )
        else:
            raise ValueError(f"Unsupported sub-agent type '{agent_type}' for sub-agent {sub_agent_id}")

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
    ) -> str | None:
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
            agent_message (str | None)
        """
        system_prompt: str = sub_agent_cfg["system_prompt"]
        mcp_tool_names: list[str] = sub_agent_cfg["mcp_tools"]
        model_name: str = sub_agent_cfg["model"]

        # Validate and create LLM via agent-common model factory
        if not is_valid_model(model_name):
            logger.warning(
                f"Invalid model '{model_name}' in sub-agent config for job {scheduled_job_id} — defaulting to {DEFAULT_MODEL}",
            )

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

        async def _run_graph(tools: list) -> None:
            nonlocal result_summary

            # Determine structured output strategy (mutates tools in-place for Bedrock+thinking)
            response_format = get_response_format(llm, tools, thinking_enabled=bool(thinking_level))

            graph = build_sub_agent_graph(
                model=llm,
                tools=tools,
                system_prompt=full_system_prompt,
                checkpointer=self._checkpointer,
                store=self.store,
                cost_logger=self._cost_logger,
                response_format=response_format,
                exclude_deep_agents_middlewares=False,
            ).with_config({"recursion_limit": _MAX_RECURSION_LIMIT})

            config = self.create_runnable_config(
                user_sub=user_sub,
                conversation_id=thread_id,
                thread_id=thread_id,
                scheduled_job_id=scheduled_job_id,
                sub_agent_id=sub_agent_cfg["sub_agent_id"],
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
            messages = [HumanMessage(content=prompt)]

            # Use astream for proper streaming support (respects recursion_limit set with .with_config())
            # stream_mode="values" with version="v2" yields StreamPart dicts:
            #   {"type": "values", "ns": (), "data": <state snapshot>}
            # We consume all and use the final state.
            final_state = None
            async for part in graph.astream({"messages": messages}, config=config, stream_mode="values", version="v2"):
                if part["type"] == "values":
                    final_state = part["data"]
                # Future: could yield progress events here for streaming execution

            output_messages = final_state.get("messages", []) if final_state else []

            # 1. Check for structured_response (AutoStrategy / ToolStrategy output)
            structured_response = final_state.get("structured_response") if final_state else None
            if structured_response and isinstance(structured_response, SubAgentResponseSchema):
                result_summary = structured_response.message
            elif isinstance(output_messages, list):
                # 2. Check message tool_calls for SubAgentResponseSchema (Bedrock + thinking)
                for msg in reversed(output_messages):
                    if hasattr(msg, "tool_calls"):
                        for tool_call in msg.tool_calls:
                            if tool_call.get("name") == "SubAgentResponseSchema":
                                try:
                                    schema = SubAgentResponseSchema(**tool_call.get("args", {}))
                                    result_summary = schema.message
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
                s3_service=get_s3_service(),
                s3_bucket=_DOCUMENT_STORE_S3_BUCKET,
                user_id=docstore_user_id,
            )
            logger.info(
                "Added %d docstore tools for job %d: %s",
                len(docstore_tools),
                scheduled_job_id,
                [t.name for t in docstore_tools],
            )

        if mcp_tool_names:
            # Keep the MCP session open for the entire graph execution so that
            # tool closures can call back into the session when invoked.
            gatana_access_token = await (self._get_oauth2_client()).exchange_token(user_access_token, "gatana")
            connection = StreamableHttpConnection(
                transport="streamable_http",
                url=_MCP_GATEWAY_URL,
                headers={"Authorization": f"Bearer {gatana_access_token}"},
                timeout=mcp_timeout,
                sse_read_timeout=mcp_timeout,
            )
            mcp_client = MultiServerMCPClient({"gateway": connection})
            async with mcp_client.session("gateway") as session:
                all_tools = await load_mcp_tools(session)
                allowed = set(mcp_tool_names)
                tools = [t for t in all_tools if t.name in allowed]
                logger.info(
                    "Loaded %d/%d MCP tools for job %d: %s",
                    len(tools),
                    len(all_tools),
                    scheduled_job_id,
                    [t.name for t in tools],
                )
                await _run_graph(tools + docstore_tools)
        else:
            await _run_graph(docstore_tools)

        logger.info(
            "LangGraph agent execution complete for job %d: %d chars",
            scheduled_job_id,
            len(result_summary or ""),
        )
        return result_summary

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
    ) -> str | None:
        """Run a Foundry query-API agent using agent-common's foundry module.

        Args:
            sub_agent_cfg: Result of _fetch_sub_agent_config() with foundry_* fields.
            prompt: The user message to process.
            user_config: Authenticated user context.
            scheduled_job_id: For logging.
            scheduled_job_run_id: For tracking the conversation.
        Returns:
            result_summary (str | None)
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
            backend_url=_PLAYGROUND_BACKEND_URL,
            sub_agent_id=sub_agent_cfg.get("sub_agent_id"),
        )

        # Stream the foundry runnable via the A2A SubAgentInput interface
        input_data = SubAgentInput(
            messages=[{"role": "user", "content": prompt}],
        )
        result_summary = await _collect_stream_text(compiled_subagent["runnable"], input_data)

        logger.info(
            "Foundry agent execution complete for job %d: %d chars",
            scheduled_job_id,
            len(result_summary or ""),
        )
        return result_summary

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
    ) -> str | None:
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

        Returns:
            result_summary (str | None)
        """
        agent_url: str | None = sub_agent_cfg.get("agent_url")
        if not agent_url:
            raise ValueError(f"Remote sub-agent '{sub_agent_cfg['name']}' has no agent_url configured")

        # Discover the remote agent's card
        agent_card_url = f"{agent_url.rstrip('/')}/.well-known/agent-card.json"
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            resp = await client.get(agent_card_url)
            resp.raise_for_status()
            agent_card = AgentCard(**resp.json())

        logger.info(
            "Discovered remote agent '%s' at %s for job %d",
            agent_card.name,
            agent_card.url,
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

        input_data = SubAgentInput(
            messages=messages_input,
            scheduled_job_id=scheduled_job_id,
        )
        result_summary = await _collect_stream_text(runnable, input_data)

        logger.info(
            "Remote agent execution complete for job %d: %d chars",
            scheduled_job_id,
            len(result_summary or ""),
        )
        return result_summary
