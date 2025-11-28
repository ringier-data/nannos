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

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)

from ..models.config import GraphRuntimeContext

logger = logging.getLogger(__name__)

# Language code to display name mapping
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sl": "Slovenian",
    "sr": "Serbian",
    "uk": "Ukrainian",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "sw": "Swahili",
}


def get_language_display_name(language_code: str) -> str:
    """Get the display name for a language code.

    Args:
        language_code: ISO 639-1 language code (e.g., 'en', 'de', 'fr')

    Returns:
        Human-readable language name, or the code itself if not found
    """
    return LANGUAGE_NAMES.get(language_code.lower(), language_code)


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
                f"- **Response Language**: You MUST respond in {language_name} ({user_context.language}). "
                f"All your responses, explanations, and communications with the user should be in {language_name}. "
                f"However, technical terms, code, tool names, and API calls should remain in their original form."
            )

        # Message formatting preference (conversation-level)
        formatting = getattr(user_context, "message_formatting", "markdown")
        if formatting == "slack":
            preferences_parts.append(
                "- **Message Formatting**: Format your responses using Slack mrkdwn syntax. "
                "Use *bold* for emphasis, _italic_ for secondary emphasis, `code` for inline code, "
                "```code blocks``` for multi-line code, and <@U123456> format for user mentions. "
                "Avoid markdown syntax that Slack doesn't support (e.g., # headers, **bold**)."
            )
        elif formatting == "plain":
            preferences_parts.append(
                "- **Message Formatting**: Use plain text only. Do not use any formatting syntax "
                "(no markdown, no bold, no code blocks). Keep responses simple and readable as plain text."
            )
        # Default 'markdown' needs no special instruction - standard behavior

        # Multi-user conversation context
        # Check if we have a slack_user_handle, indicating multi-user Slack context
        slack_handle = getattr(user_context, "slack_user_handle", None)
        if slack_handle:
            preferences_parts.append(
                "- **Multi-User Conversation**: This is a multi-user conversation. Each user message is prefixed "
                "with the speaker's identity in the format `[Name <@SlackHandle>]: message`. You should:\n"
                "  - Track who said what and refer to users naturally (e.g., 'as Bob mentioned')\n"
                "  - When you need input from a specific user, mention them using their Slack handle from the prefix\n"
                f"  - The current speaker is: {user_context.name} {slack_handle}\n"
                "  - Address responses appropriately when multiple users are involved"
            )

        if not preferences_parts:
            return ""

        addendum = "\n\n**User Preferences:**\n" + "\n".join(preferences_parts)

        logger.debug(
            f"UserPreferencesMiddleware: Built preferences addendum for user {user_context.user_id}: "
            f"language={user_context.language}"
        )

        return addendum

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

        addendum = self._build_preferences_addendum(user_context)
        if addendum:
            request.system_prompt = request.system_prompt + addendum if request.system_prompt else addendum

        return handler(request)

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

        addendum = self._build_preferences_addendum(user_context)
        if addendum:
            request.system_prompt = request.system_prompt + addendum if request.system_prompt else addendum

        return await handler(request)
