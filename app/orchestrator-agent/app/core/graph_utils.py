"""Shared agent graph utilities.

Provides helpers shared between the main GP graph (GraphFactory._create_gp_graph)
and dynamic local sub-agents (DynamicLocalAgentRunnable._ensure_agent) to avoid
duplicating the common middleware stack.
"""

from typing import Any

from deepagents.middleware import FilesystemMiddleware, SummarizationMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import _compute_summarization_defaults
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel

from ..middleware import RepeatedToolCallMiddleware, ToolSchemaCleaningMiddleware


def build_common_middleware_stack(model: BaseChatModel, backend: Any) -> list:
    """Build the common middleware stack shared by every LangGraph agent in this project.

    Creates seven middlewares that every agent should run beneath its
    tool-selection / dispatch layer:

    1. ``FilesystemMiddleware`` - virtual file-system backed by *backend*.
       When *backend* is a ``CompositeBackend`` with an ``IndexingStoreBackend``
       route for ``/memories/``, written files are automatically indexed for
       semantic search.
    2. ``SummarizationMiddleware`` - summarises old messages to stay within the
       model's context-window limit.  Trigger / keep values are computed from
       the model's token profile via ``_compute_summarization_defaults``.
    3. ``AnthropicPromptCachingMiddleware`` - enables Anthropic prompt caching;
       silently ignored for non-Anthropic models
       (``unsupported_model_behavior="ignore"``).
    4. ``PatchToolCallsMiddleware`` - normalises tool-call format across
       providers (Bedrock, OpenAI, Gemini, …).
    5. ``ToolRetryMiddleware`` - retries failed tool calls with exponential
       back-off (max 5 retries, factor 2.0).
    6. ``RepeatedToolCallMiddleware`` - detects and breaks tool-call loops
       (max 5 identical calls within a window of 10).
    7. ``ToolSchemaCleaningMiddleware`` - cleans tool schemas at model-binding
       time for Gemini compatibility.

    Args:
        model: The ``BaseChatModel`` instance used to compute summarization
            defaults and to pass into ``SummarizationMiddleware``.
        backend: A backend instance **or** a backend factory
            ``Callable[[Runtime], Backend]``.  Passed directly to both
            ``FilesystemMiddleware`` and ``SummarizationMiddleware``.

    Returns:
        Ordered list of seven middleware instances ready to be included in a
        ``create_agent`` / ``create_deep_agent`` call.
    """
    summarization_defaults = _compute_summarization_defaults(model)
    return [
        FilesystemMiddleware(backend=backend),
        SummarizationMiddleware(
            model=model,
            backend=backend,
            trigger=summarization_defaults["trigger"],
            keep=summarization_defaults["keep"],
            trim_tokens_to_summarize=None,
            truncate_args_settings=summarization_defaults["truncate_args_settings"],
        ),
        AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
        PatchToolCallsMiddleware(),
        ToolRetryMiddleware(
            max_retries=5,
            backoff_factor=2.0,
        ),
        RepeatedToolCallMiddleware(max_repeats=5, window_size=10),
        ToolSchemaCleaningMiddleware(),
    ]
