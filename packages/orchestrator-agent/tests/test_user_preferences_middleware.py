"""Tests for UserPreferencesMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import SystemMessage

from app.middleware.user_preferences_middleware import (
    UserPreferencesMiddleware,
)
from app.models.config import GraphRuntimeContext
from app.utils import LANGUAGE_NAMES, get_language_display_name


class TestGetLanguageDisplayName:
    """Tests for get_language_display_name function."""

    def test_returns_english_for_en(self):
        """Should return 'English' for 'en' code."""
        assert get_language_display_name("en") == "English"

    def test_returns_german_for_de(self):
        """Should return 'German' for 'de' code."""
        assert get_language_display_name("de") == "German"

    def test_case_insensitive(self):
        """Should handle uppercase language codes."""
        assert get_language_display_name("EN") == "English"
        assert get_language_display_name("De") == "German"
        assert get_language_display_name("FR") == "French"

    def test_returns_code_for_unknown_language(self):
        """Should return the code itself for unknown languages."""
        assert get_language_display_name("xyz") == "xyz"
        assert get_language_display_name("unknown") == "unknown"

    def test_all_known_languages_have_display_names(self):
        """All languages in LANGUAGE_NAMES should have proper display names."""
        for code, name in LANGUAGE_NAMES.items():
            assert isinstance(name, str)
            assert len(name) > 0
            assert get_language_display_name(code) == name


class TestUserPreferencesMiddleware:
    """Tests for UserPreferencesMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create a middleware instance for testing."""
        return UserPreferencesMiddleware()

    @pytest.fixture
    def user_context_en(self):
        """Create a user context with English language."""
        return GraphRuntimeContext(
            user_id="user123",
            user_sub="sub123",
            name="Test User",
            email="test@example.com",
            language="en",
        )

    @pytest.fixture
    def user_context_de(self):
        """Create a user context with German language."""
        return GraphRuntimeContext(
            user_id="user456",
            user_sub="sub456",
            name="Test User DE",
            email="test.de@example.com",
            language="de",
        )

    @pytest.fixture
    def user_context_no_language(self):
        """Create a user context with no language (empty string)."""
        return GraphRuntimeContext(
            user_id="user789",
            user_sub="sub789",
            name="Test User Empty",
            email="test.empty@example.com",
            language="",
        )

    def test_build_preferences_addendum_with_english(self, middleware, user_context_en):
        """Should build addendum with English language preference."""
        addendum = middleware._build_preferences_addendum(user_context_en)

        assert "<user_preferences>" in addendum
        assert "<language>" in addendum
        assert "English" in addendum
        assert "(en)" in addendum
        assert "Respond in English" in addendum

    def test_build_preferences_addendum_with_german(self, middleware, user_context_de):
        """Should build addendum with German language preference."""
        addendum = middleware._build_preferences_addendum(user_context_de)

        assert "<user_preferences>" in addendum
        assert "<language>" in addendum
        assert "German" in addendum
        assert "(de)" in addendum
        assert "Respond in German" in addendum

    def test_build_preferences_addendum_with_empty_language(self, middleware, user_context_no_language):
        """Should return addendum with timezone but no language when language is empty."""
        addendum = middleware._build_preferences_addendum(user_context_no_language)
        # Should still include timezone info even without language
        assert "<user_preferences>" in addendum
        assert "<timezone>" in addendum
        assert "<language>" not in addendum  # Language part should be skipped

    def test_build_preferences_addendum_with_custom_prompt(self, middleware):
        """Should include custom_prompt in addendum when set."""
        user_context = GraphRuntimeContext(
            user_id="user123",
            user_sub="sub123",
            name="Test User",
            email="test@example.com",
            language="en",
            custom_prompt="Always be concise and use bullet points.",
        )
        addendum = middleware._build_preferences_addendum(user_context)

        assert "<user_preferences>" in addendum
        assert "<custom_instructions>" in addendum
        assert "Always be concise and use bullet points." in addendum

    def test_build_preferences_addendum_without_custom_prompt(self, middleware, user_context_en):
        """Should not include custom_prompt section when not set."""
        addendum = middleware._build_preferences_addendum(user_context_en)

        assert "<user_preferences>" in addendum
        assert "<custom_instructions>" not in addendum

    def test_build_preferences_addendum_with_empty_custom_prompt(self, middleware):
        """Should not include custom_prompt section when set to empty string."""
        user_context = GraphRuntimeContext(
            user_id="user123",
            user_sub="sub123",
            name="Test User",
            email="test@example.com",
            language="en",
            custom_prompt="",
        )
        addendum = middleware._build_preferences_addendum(user_context)

        assert "<user_preferences>" in addendum
        assert "<custom_instructions>" not in addendum

    def test_wrap_model_call_appends_to_existing_prompt(self, middleware, user_context_de):
        """Should append preferences to existing system prompt."""
        original_prompt = "You are a helpful assistant."

        mock_request = MagicMock()
        mock_request.runtime.context = user_context_de
        mock_request.system_message = SystemMessage(content=original_prompt)

        mock_handler = MagicMock(return_value="response")

        middleware.wrap_model_call(mock_request, mock_handler)

        # Verify override was called with a system_message containing preferences
        mock_request.override.assert_called_once()
        new_system_message = mock_request.override.call_args.kwargs["system_message"]
        content_str = str(new_system_message.content)
        assert original_prompt in content_str
        assert "German" in content_str
        assert "<user_preferences>" in content_str

        # Verify handler was called with the modified request
        mock_handler.assert_called_once_with(mock_request.override.return_value)

    def test_wrap_model_call_creates_prompt_when_none(self, middleware, user_context_de):
        """Should create system prompt when none exists."""
        mock_request = MagicMock()
        mock_request.runtime.context = user_context_de
        mock_request.system_message = None

        mock_handler = MagicMock(return_value="response")

        middleware.wrap_model_call(mock_request, mock_handler)

        # Verify override was called with a non-None system_message containing preferences
        mock_request.override.assert_called_once()
        new_system_message = mock_request.override.call_args.kwargs["system_message"]
        assert new_system_message is not None
        content_str = str(new_system_message.content)
        assert "German" in content_str

    def test_wrap_model_call_passes_through_without_context(self, middleware):
        """Should pass through when no GraphRuntimeContext."""
        mock_request = MagicMock()
        mock_request.runtime.context = "not a GraphRuntimeContext"
        mock_request.system_prompt = "Original prompt"

        mock_handler = MagicMock(return_value="response")

        middleware.wrap_model_call(mock_request, mock_handler)

        # Verify prompt unchanged
        assert mock_request.system_prompt == "Original prompt"
        mock_handler.assert_called_once_with(mock_request)

    @pytest.mark.asyncio
    async def test_awrap_model_call_appends_to_existing_prompt(self, middleware, user_context_de):
        """Should append preferences to existing system prompt (async)."""
        original_prompt = "You are a helpful assistant."

        mock_request = MagicMock()
        mock_request.runtime.context = user_context_de
        mock_request.system_message = SystemMessage(content=original_prompt)

        mock_handler = AsyncMock(return_value="response")

        await middleware.awrap_model_call(mock_request, mock_handler)

        # Verify override was called with a system_message containing preferences
        mock_request.override.assert_called_once()
        new_system_message = mock_request.override.call_args.kwargs["system_message"]
        content_str = str(new_system_message.content)
        assert original_prompt in content_str
        assert "German" in content_str

        # Verify handler was called with the modified request
        mock_handler.assert_called_once_with(mock_request.override.return_value)

    @pytest.mark.asyncio
    async def test_awrap_model_call_passes_through_without_context(self, middleware):
        """Should pass through when no GraphRuntimeContext (async)."""
        mock_request = MagicMock()
        mock_request.runtime.context = "not a GraphRuntimeContext"
        mock_request.system_prompt = "Original prompt"

        mock_handler = AsyncMock(return_value="response")

        await middleware.awrap_model_call(mock_request, mock_handler)

        # Verify prompt unchanged
        assert mock_request.system_prompt == "Original prompt"
        mock_handler.assert_called_once_with(mock_request)

    def test_technical_terms_preserved_note(self, middleware, user_context_de):
        """Should include note about preserving technical terms."""
        addendum = middleware._build_preferences_addendum(user_context_de)

        assert "Technical terms" in addendum
        assert "code" in addendum
        assert "tool names" in addendum
        assert "original form" in addendum


class TestUserPreferencesMiddlewareIntegration:
    """Integration-style tests for UserPreferencesMiddleware."""

    def test_middleware_has_correct_schema(self):
        """Middleware should have correct state schema."""
        middleware = UserPreferencesMiddleware()
        # AgentState is the expected schema
        from langchain.agents.middleware.types import AgentState

        assert middleware.state_schema == AgentState

    def test_middleware_has_no_tools(self):
        """Middleware should not register any tools."""
        middleware = UserPreferencesMiddleware()
        assert middleware.tools == []
