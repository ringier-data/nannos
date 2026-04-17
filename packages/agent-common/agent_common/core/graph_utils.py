"""Shared agent graph utilities.

Provides helpers shared between the main GP graph (GraphFactory._create_gp_graph)
and dynamic local sub-agents (DynamicLocalAgentRunnable._ensure_agent) to avoid
duplicating the common middleware stack.

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

import logging
from typing import Any, Callable, Optional

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.state import StateBackend
from deepagents.middleware import FilesystemMiddleware, SummarizationMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import compute_summarization_defaults
from langchain.agents import create_agent
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_aws.middleware.prompt_caching import BedrockPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.postgres.aio import AsyncPostgresStore
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.middleware.tool_schema_cleaning import ToolSchemaCleaningMiddleware

from agent_common.backends.indexing_store import IndexingStoreBackend
from agent_common.middleware.loop_detection_middleware import RepeatedToolCallMiddleware
from agent_common.middleware.storage_paths_middleware import StoragePathsInstructionMiddleware

logger = logging.getLogger(__name__)


_DOCSTORE_HINT = (
    "\n\nNote: This content has also been chunked and each chunk was indexed for semantic search. "
    "Use the `docstore_search` tool to find relevant sections without "
    "reading the full file."
)

# TODO: not all the conversations should have a channel configuration


class _FilesystemMiddlewareWithDocstoreHint(FilesystemMiddleware):
    """FilesystemMiddleware subclass that appends a docstore search hint to eviction messages.

    When ``FilesystemMiddleware`` evicts an oversized tool result to
    ``/large_tool_results/``, it replaces the ``ToolMessage`` content with a
    summary and a ``read_file`` instruction.  This subclass appends an
    additional line reminding the agent that the evicted content has been
    chunked and indexed by ``IndexingStoreBackend`` and can therefore be retrieved via
    ``docstore_search`` as well.

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
    3. ``AnthropicPromptCachingMiddleware`` - enables Anthropic prompt caching;
       silently ignored for non-Anthropic models
       (``unsupported_model_behavior="ignore"``).
    4. ``BedrockPromptCachingMiddleware`` - enables Bedrock prompt caching;
       silently ignored for non-Bedrock models
       (``unsupported_model_behavior="ignore"``).
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

    Returns:
        Ordered list of seven middleware instances ready to be included in a
        ``create_agent`` / ``create_deep_agent`` call.
    """
    middleware = []
    if not exclude_deep_agents_middlewares:
        summarization_defaults = compute_summarization_defaults(model)
        fs_cls = _FilesystemMiddlewareWithDocstoreHint if add_docstore_hint else FilesystemMiddleware

        middleware += [
            StoragePathsInstructionMiddleware(),  # right before FilesytemMiddleware
            fs_cls(backend=backend),
            SummarizationMiddleware(
                model=model,
                backend=backend,
                trigger=summarization_defaults["trigger"],
                keep=summarization_defaults["keep"],
                trim_tokens_to_summarize=None,
                truncate_args_settings=summarization_defaults["truncate_args_settings"],
            ),
            AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            BedrockPromptCachingMiddleware(unsupported_model_behavior="ignore"),
            PatchToolCallsMiddleware(),
            ToolRetryMiddleware(
                max_retries=5,
                backoff_factor=2.0,
            ),
        ]

    middleware += [
        RepeatedToolCallMiddleware(max_repeats=5, window_size=10),
        ToolSchemaCleaningMiddleware(),
    ]
    return middleware


def create_indexing_backend_factory(
    store: AsyncPostgresStore | None,
    bedrock_region: str | None = None,
    cost_logger: Optional[CostLogger] = None,
) -> Callable[[Any], Any]:
    """Return a backend factory for FilesystemMiddleware.

    When a document store is available the factory returns a
    ``CompositeBackend`` that routes ``/memories/`` and
    ``/large_tool_results/`` writes through ``IndexingStoreBackend`` for
    automatic semantic indexing, with everything else falling back to
    ephemeral ``StateBackend``.

    ``/large_tool_results/`` is the path used by ``FilesystemMiddleware``
    when it evicts oversized tool results.  Routing it through
    ``IndexingStoreBackend`` ensures evicted content is indexed for
    semantic search and persisted across turns, not lost in ephemeral
    ``StateBackend``.

    When no store is configured the factory simply wraps a ``StateBackend``
    (ephemeral, in-agent-state storage).

    Args:
        store: Initialised ``AsyncPostgresStore`` instance, or ``None`` when
            the document store is not configured.
        bedrock_region: AWS region for Bedrock model creation in the
            indexing pipeline.  When ``None`` the backend reads
            ``AWS_BEDROCK_REGION`` / ``AWS_REGION`` from the environment.
        cost_logger: Optional ``CostLogger`` for reporting LLM usage costs
            incurred by the indexing pipeline (contextualisation calls to
            Claude).

    Returns:
        A callable ``(ToolRuntime) -> Backend`` suitable for passing to
        ``FilesystemMiddleware``, ``SummarizationMiddleware``,
        ``build_common_middleware_stack``, or ``create_deep_agent``.
    """
    if store is not None:

        def _backend_with_indexing(rt: Any) -> CompositeBackend:
            # Create three IndexingStoreBackend instances with explicit path-based routing:
            # 1. /memories/ → user-scoped (user_id, "filesystem") - personal files
            # 2. /large_tool_results/ → conversation-scoped (conversation_id, "filesystem") - tool results
            # 3. /channel_memories/ → channel-scoped (assistant_id, "filesystem") - shared channel files
            #
            # Application logic decides which path to use:
            # - write_file("/memories/foo") → personal, user-scoped
            # - write_file("/channel_memories/foo") → shared, channel-scoped
            # - Tool results always go to /large_tool_results/ (conversation-scoped)

            # Personal files: user-scoped namespace
            user_documents_backend = IndexingStoreBackend(
                rt,
                bedrock_region=bedrock_region,
                cost_logger=cost_logger,
                namespace_factory=lambda ctx: _user_scoped_namespace(ctx),
            )

            # Tool results: conversation-scoped namespace for isolation
            tool_results_backend = IndexingStoreBackend(
                rt,
                bedrock_region=bedrock_region,
                cost_logger=cost_logger,
                namespace_factory=lambda ctx: _conversation_scoped_namespace(ctx),
            )

            # Channel files: channel-scoped namespace for shared access
            channel_documents_backend = IndexingStoreBackend(
                rt,
                bedrock_region=bedrock_region,
                cost_logger=cost_logger,
                namespace_factory=lambda ctx: _channel_scoped_namespace(ctx),
            )

            return CompositeBackend(
                default=StateBackend(rt),
                routes={
                    "/memories/": user_documents_backend,
                    "/large_tool_results/": tool_results_backend,
                    "/channel_memories/": channel_documents_backend,
                },
            )

        return _backend_with_indexing
    else:
        return lambda rt: StateBackend(rt)


def _conversation_scoped_namespace(ctx: Any) -> tuple[str, ...]:
    """Namespace factory for conversation-scoped files (tool results).

    Returns (conversation_id, "filesystem") to isolate files per conversation.
    Returns impossible-to-match sentinel if conversation_id missing (for graceful grep failure).
    """
    metadata = ctx.runtime.config.get("metadata", {})
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
    metadata = ctx.runtime.config.get("metadata", {})
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
    metadata = ctx.runtime.config.get("metadata", {})
    assistant_id = metadata.get("assistant_id")

    if not assistant_id:
        # Return sentinel namespace that won't match any real data
        # This allows grep to continue without failing, while preventing wrong-namespace access
        logger.warning("[NAMESPACE] assistant_id missing, using sentinel namespace")
        return ("__missing_assistant_id__", "filesystem")

    logger.info(f"[NAMESPACE] channel-scoped: ({assistant_id}, 'filesystem')")
    return (assistant_id, "filesystem")


def build_sub_agent_graph(
    model: BaseChatModel,
    tools: list,
    system_prompt: str,
    checkpointer: BaseCheckpointSaver | None,
    store: AsyncPostgresStore | None = None,
    bedrock_region: str | None = None,
    cost_logger: Optional[CostLogger] = None,
    response_format: Any = None,
    exclude_deep_agents_middlewares: bool = False,
    backend_factory: Optional[Callable[[Any], Any]] = None,
    **kwargs: Any,
) -> CompiledStateGraph:
    """Build a standard deep-agent LangGraph graph.

    Combines three steps that every non-orchestrator agent repeats:

    1. **Backend factory** — ``create_indexing_backend_factory(store,
       agent_settings)`` selects the right backend (indexing vs. ephemeral),
       unless *backend_factory* is provided directly (e.g. by
       ``DynamicLocalAgentRunnable`` which may receive a pre-built factory
       from the orchestrator).
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
        checkpointer: LangGraph checkpoint saver (DynamoDB, memory, …).
        store: Optional initialised ``AsyncPostgresStore`` for persistent
            memory / document search.
        bedrock_region: AWS region for Bedrock model creation.
            ``None`` triggers env-var fallback.
        response_format: Pre-computed structured-output strategy
            (``AutoStrategy``, ``ToolStrategy``, ``None``, …).  Pass the
            result of ``get_response_format()`` here.
        exclude_deep_agents_middlewares: When ``True``, omits
            ``FilesystemMiddleware`` and ``SummarizationMiddleware`` from the
            middleware stack (intended for ``agent-runner`` which manages its
            own file-system lifecycle).
        backend_factory: Optional pre-built backend factory
            ``Callable[[Runtime], Backend]``.  When provided it is used
            directly instead of calling ``create_indexing_backend_factory``.
            Useful when the caller (e.g. ``DynamicLocalAgentRunnable``) has
            already received an injected factory from the orchestrator.
        **kwargs: Extra keyword arguments forwarded verbatim to
            ``create_agent`` (e.g. ``context_schema``,
            ``recursion_limit``).

    Returns:
        A compiled ``CompiledStateGraph`` ready for ``astream_events``.
    """
    backend = (
        backend_factory
        if backend_factory is not None
        else create_indexing_backend_factory(store, bedrock_region, cost_logger=cost_logger)
    )
    middleware = build_common_middleware_stack(
        model,
        backend,
        exclude_deep_agents_middlewares,
        add_docstore_hint=store is not None or backend_factory is not None,
    )
    return create_agent(
        model,
        system_prompt=system_prompt,
        tools=tools,
        checkpointer=checkpointer,
        store=store,
        middleware=middleware,
        response_format=response_format,
        **kwargs,
    )
