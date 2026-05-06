"""Steering Middleware for A2A Continuous Interaction Turns.

When a second user message arrives while an agent is already processing (via
the executor's active stream registry), it is placed in an asyncio.Queue.
This middleware drains that queue in the ``before_model`` hook and injects the
messages as HumanMessages so the LLM sees the user's follow-up instructions
*before* its next reasoning step.

Integration:
    ```python
    agent = create_deep_agent(
        model=model,
        tools=tools,
        middleware=[
            ...,
            SteeringMiddleware(get_pending_messages=agent.get_pending_messages),
            ...,
        ],
    )
    ```

The middleware needs a ``get_pending_messages`` callable that accepts a
``context_id: str`` and returns ``list[Message]``.  In the standard SDK
setup this is ``BaseAgent.get_pending_messages``.
"""

import inspect
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Union

from a2a.types import Message as A2AMessage
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.config import get_config, get_stream_writer
from langgraph.runtime import Runtime
from langgraph.typing import ContextT

from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content

logger = logging.getLogger(__name__)


class SteeringMiddleware(AgentMiddleware[AgentState, ContextT]):
    """Injects pending user steering messages before each LLM call.

    When the executor detects a second ``tasks/sendSubscribe`` request for an
    already-active ``context_id``, it queues the new A2A ``Message`` in the
    agent's message queue.  This middleware drains that queue and appends the
    content as ``HumanMessage`` objects to the conversation *before* the model
    is invoked, giving the LLM the user's latest instructions.

    An activity-log custom stream event is also emitted so that downstream
    consumers (e.g. the console frontend) can render a timeline entry.
    """

    def __init__(
        self,
        get_pending_messages: Callable[[str], list[A2AMessage]],
        on_messages_received: Union[
            Callable[[str, list[A2AMessage]], None],
            Callable[[str, list[A2AMessage]], Coroutine[Any, Any, None]],
            None,
        ] = None,
    ) -> None:
        """Initialise with a callable that drains the steering queue.

        Args:
            get_pending_messages: Typically ``BaseAgent.get_pending_messages``.
                Accepts a ``context_id`` string and returns a (possibly empty)
                list of A2A Messages.
            on_messages_received: Optional callback fired when steering messages
                are consumed.  May be sync or async.  The orchestrator uses
                this to forward to active sub-agents.
        """
        super().__init__()
        self._get_pending_messages = get_pending_messages
        self._on_messages_received = on_messages_received

    @property
    def name(self) -> str:
        return self.__class__.__name__

    def _resolve_context_id(self, runtime: Runtime[ContextT]) -> str | None:
        """Extract the context_id from the LangGraph runtime config.

        Tries ``metadata.conversation_id`` first (set by both the orchestrator
        and the SDK's _stream_impl), falls back to ``configurable.thread_id``.

        NOTE: ``Runtime`` (used in ``abefore_model``) does NOT carry a
        ``config`` attribute — only ``ToolRuntime`` (used in tool hooks) does.
        We therefore read the config from the LangGraph context-variable via
        ``get_config()``.
        """
        try:
            config = get_config()
        except RuntimeError:
            logger.debug("[STEERING] get_config() unavailable — not inside a LangGraph run")
            return None
        configurable = config.get("configurable", {})
        metadata = config.get("metadata", {})
        return metadata.get("conversation_id") or configurable.get("thread_id")

    async def abefore_model(
        self,
        state: AgentState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Drain pending steering messages and inject as HumanMessages.

        Returns a state update dict with the new messages appended, or None
        if no steering messages are pending.
        """
        context_id = self._resolve_context_id(runtime)
        if not context_id:
            return None

        pending = self._get_pending_messages(context_id)
        if not pending:
            return None

        # Notify callback (e.g., orchestrator forwarding to active sub-agents)
        if self._on_messages_received:
            try:
                result = self._on_messages_received(context_id, pending)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("[STEERING] on_messages_received callback failed", exc_info=True)

        injected: list[HumanMessage] = []
        for msg in pending:
            content = a2a_parts_to_content(msg.parts or [])
            if not content:
                continue
            injected.append(
                HumanMessage(
                    content_blocks=content,
                    additional_kwargs={"steering": True},
                )
            )
            logger.debug(f"[STEERING] Injecting user follow-up for context_id={context_id}: {content}")

        if not injected:
            return None

        # Emit an activity-log custom stream event so the UI can show a timeline entry.
        try:
            writer = get_stream_writer()
            writer(
                {
                    "activity_log": True,
                    "text": f"User follow-up received ({len(injected)} message(s))",
                    "source": "user",
                }
            )
        except Exception:
            # Stream writer may not be available in all execution contexts (e.g. tests).
            pass
        # will update the AgentState.messages: https://docs.langchain.com/oss/python/langgraph/graph-api#messagesstate
        return {"messages": injected}
