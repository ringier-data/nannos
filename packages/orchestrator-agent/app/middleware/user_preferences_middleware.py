"""User preferences injection middleware for runtime system prompt customization.

This middleware injects user-specific preferences into the system prompt at runtime,
enabling personalized agent behavior without requiring separate graph instances per user.

Currently supported preferences:
- Language: User's preferred language for agent responses

Architecture:
- Reads preferences from GraphRuntimeContext at each model call
- Appends a user preferences addendum to the system prompt
- Works with all LLM providers (OpenAI, Anthropic/Bedrock, etc.)
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent_common.middleware.client_objects_middleware import render_client_objects_block
from agent_common.middleware.utils import append_to_last_human_message, append_to_system_message
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)

from ..models.config import GraphRuntimeContext
from ..utils import get_language_display_name

logger = logging.getLogger(__name__)


class UserPreferencesMiddleware(AgentMiddleware[AgentState, GraphRuntimeContext]):
    """Middleware for injecting user preferences into the system prompt.

    Reads user preferences from GraphRuntimeContext and appends them to the
    system prompt at runtime. This enables personalized agent behavior for each
    user without requiring separate graph instances.

    Currently supported preferences:
    - language: User's preferred language for responses

    Example:
        ```python
        middleware = [
            UserPreferencesMiddleware(),
            DynamicToolDispatchMiddleware(...),
            # ... other middleware
        ]

        agent = create_deep_agent(
            model=model,
            tools=[],
            middleware=middleware,
            context_schema=GraphRuntimeContext,
        )

        # User preferences are injected at runtime via context
        user_context = GraphRuntimeContext(
            user_id="user1",
            name="John Doe",
            email="john@example.com",
            language="de",  # German
        )
        agent.invoke({"messages": [...]}, context=user_context)
        ```
    """

    state_schema = AgentState
    tools: list[Any] = []  # No tools registered with this middleware

    def _build_preferences_addendum(self, user_context: GraphRuntimeContext) -> str:
        """Build the user preferences addendum for the system prompt.

        Args:
            user_context: User context containing preferences

        Returns:
            Formatted string to append to the system prompt
        """
        preferences_parts: list[str] = []

        # Language preference
        if user_context.language:
            language_name = get_language_display_name(user_context.language)
            preferences_parts.append(
                f"<language>\n"
                f"Respond in {language_name} ({user_context.language}). "
                f"All responses, explanations, and communications should be in {language_name}. "
                f"Technical terms, code, tool names, and API calls should remain in their original form.\n"
                f"</language>"
            )

        # Timezone preference
        if user_context.timezone:
            preferences_parts.append(
                f"<timezone>\n"
                f"The user's timezone is {user_context.timezone}. "
                f"When using the get_current_time tool, pass timezone='{user_context.timezone}' to get times in their local timezone.\n"
                f"</timezone>"
            )

        # Message formatting preference (conversation-level)
        formatting = getattr(user_context, "message_formatting", "markdown")
        if formatting == "slack":
            preferences_parts.append(
                '<message_formatting format="slack">\n'
                "Format responses using Slack mrkdwn syntax: *bold* for emphasis, _italic_ for secondary emphasis, "
                "`code` for inline code, ```code blocks``` for multi-line code. "
                "Avoid markdown syntax that Slack doesn't support (e.g., # headers, **bold**).\n"
                "</message_formatting>"
            )
        elif formatting == "google-chat":
            preferences_parts.append(
                '<message_formatting format="google-chat">\n'
                "Format responses using Google Chat markup syntax: *bold* for emphasis, _italic_ for secondary emphasis, "
                "~strikethrough~ for strikethrough, `code` for inline code, ```code blocks``` for multi-line code. "
                "Use plain URLs for links (they are auto-linked). "
                "Avoid markdown syntax that Google Chat doesn't support (e.g., # headers, **bold**, [links](url)).\n"
                "</message_formatting>"
            )
        elif formatting == "plain":
            preferences_parts.append(
                '<message_formatting format="plain">\n'
                "Use plain text only. Do not use any formatting syntax "
                "(no markdown, no bold, no code blocks). Keep responses simple and readable.\n"
                "</message_formatting>"
            )
        # Default 'markdown' needs no special instruction - standard behavior

        # Multi-user conversation context
        # Check if we have a client_user_handle, indicating multi-user channel context (Slack or Google Chat)
        client_handle = getattr(user_context, "client_user_handle", None)
        if client_handle:
            preferences_parts.append(
                "<multi_user_conversation>\n"
                "This is a multi-user conversation. Each user message is prefixed "
                "with the speaker's identity in the format `[Name <handle>]: message`.\n"
                "- Track who said what and refer to users naturally (e.g., 'as Bob mentioned').\n"
                "- When you need input from a specific user, mention them using their handle.\n"
                f"- The current speaker is: {user_context.name} {client_handle}\n"
                "- Address responses appropriately when multiple users are involved.\n"
                "</multi_user_conversation>"
            )

        # NOTE: the Embedded Nannos <client_objects> manifest is intentionally NOT
        # part of this system-prompt addendum. It reflects volatile on-screen state,
        # so it rides the last human message (see _apply) to keep the cached system
        # prefix byte-stable across turns.

        # Custom prompt addendum from user settings
        custom_prompt = getattr(user_context, "custom_prompt", None)
        if custom_prompt:
            preferences_parts.append(f"<custom_instructions>\n{custom_prompt}\n</custom_instructions>")

        if not preferences_parts:
            return ""

        addendum = "\n\n<user_preferences>\n" + "\n".join(preferences_parts) + "\n</user_preferences>"

        logger.debug(
            f"UserPreferencesMiddleware: Built preferences addendum for user {user_context.user_id}: "
            f"language={user_context.language}, custom_prompt={'set' if custom_prompt else 'none'}"
        )

        return addendum

    def _apply(self, request: ModelRequest, user_context: GraphRuntimeContext) -> ModelRequest:
        """Inject stable per-user prefs into the system prompt and the volatile
        Embedded Nannos ``<client_objects>`` manifest onto the last human message.

        Stable prefs (language/timezone/formatting/custom_prompt) belong in the
        cached system prefix. The manifest reflects on-screen state that changes as
        the user navigates, so it rides the human turn to avoid busting that prefix.
        """
        addendum = self._build_preferences_addendum(user_context)
        if addendum:
            request = request.override(
                system_message=append_to_system_message(request.system_message, addendum)
            )

        block = render_client_objects_block(getattr(user_context, "client_objects", None))
        if block:
            new_messages = append_to_last_human_message(request.messages, block)
            if new_messages is not None:
                request = request.override(messages=new_messages)
            else:
                # No human message to carry the manifest (e.g. an unusual resume
                # shape) — fall back to the system prompt so the agent still
                # perceives on-screen objects.
                request = request.override(
                    system_message=append_to_system_message(request.system_message, "\n\n" + block)
                )

        return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Inject user preferences into the system prompt at call time.

        Args:
            request: Model request containing the system prompt
            handler: Callback to execute the model

        Returns:
            Model response
        """
        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning("UserPreferencesMiddleware: No GraphRuntimeContext, passing through")
            return handler(request)

        return handler(self._apply(request, user_context))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Async version of wrap_model_call.

        Args:
            request: Model request containing the system prompt
            handler: Async callback to execute the model

        Returns:
            Model response
        """
        user_context = request.runtime.context
        if not isinstance(user_context, GraphRuntimeContext):
            logger.warning("UserPreferencesMiddleware: No GraphRuntimeContext, passing through")
            return await handler(request)

        return await handler(self._apply(request, user_context))
