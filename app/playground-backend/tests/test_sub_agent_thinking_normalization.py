"""Unit tests for sub-agent thinking configuration normalization."""

from playground_backend.models.sub_agent import ThinkingLevel
from playground_backend.services.sub_agent_service import (
    MODELS_SUPPORTING_THINKING,
    _normalize_thinking_config,
)


class TestNormalizeThinkingConfig:
    """Test _normalize_thinking_config() function."""

    def test_claude_sonnet_supports_thinking(self):
        """Test that Claude Sonnet 4.5 supports thinking."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            enable_thinking=True,
            thinking_level=ThinkingLevel.MEDIUM,
        )

        assert enable is True
        assert level == "medium"

    def test_gemini_models_support_thinking(self):
        """Test that Gemini models support thinking."""
        for model in ["gemini-3-pro-preview", "gemini-3-flash-preview"]:
            enable, level = _normalize_thinking_config(
                model=model,
                enable_thinking=True,
                thinking_level=ThinkingLevel.HIGH,
            )

            assert enable is True
            assert level == "high"

    def test_unsupported_model_returns_none(self):
        """Test that unsupported models return None for thinking config."""
        for model in ["gpt4o", "gpt-4o-mini", "unsupported-model"]:
            enable, level = _normalize_thinking_config(
                model=model,
                enable_thinking=True,
                thinking_level=ThinkingLevel.LOW,
            )

            assert enable is None
            assert level is None

    def test_disabled_thinking_returns_false_and_none(self):
        """Test that disabled thinking returns False and None for level."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            enable_thinking=False,
            thinking_level=ThinkingLevel.MEDIUM,
        )

        assert enable is False
        assert level is None

    def test_none_enable_thinking_preserves_behavior(self):
        """Test that None enable_thinking is preserved for supported models."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            enable_thinking=None,
            thinking_level=None,
        )

        assert enable is None
        assert level is None

    def test_thinking_level_enum_to_string_conversion(self):
        """Test that ThinkingLevel enum is converted to string."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            enable_thinking=True,
            thinking_level=ThinkingLevel.LOW,
        )

        assert enable is True
        assert level == "low"  # String, not enum
        assert isinstance(level, str)

    def test_thinking_level_string_passthrough(self):
        """Test that string thinking level is passed through unchanged."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            enable_thinking=True,
            thinking_level="high",
        )

        assert enable is True
        assert level == "high"

    def test_none_model_with_thinking_enabled(self):
        """Test None model with thinking enabled."""
        # When model is None, we don't know if it supports thinking
        # So we should return None for both
        enable, level = _normalize_thinking_config(
            model=None,
            enable_thinking=True,
            thinking_level=ThinkingLevel.MEDIUM,
        )

        # Model doesn't support thinking (None is not in MODELS_SUPPORTING_THINKING)
        assert enable is None
        assert level is None


class TestModelsSupportingThinking:
    """Test MODELS_SUPPORTING_THINKING constant."""

    def test_supported_models_set_contains_expected_models(self):
        """Test that the supported models set contains the expected models."""
        assert "claude-sonnet-4.5" in MODELS_SUPPORTING_THINKING
        assert "claude-haiku-4-5" in MODELS_SUPPORTING_THINKING
        assert "gemini-3-pro-preview" in MODELS_SUPPORTING_THINKING
        assert "gemini-3-flash-preview" in MODELS_SUPPORTING_THINKING

    def test_unsupported_models_not_in_set(self):
        """Test that unsupported models are not in the set."""
        assert "gpt4o" not in MODELS_SUPPORTING_THINKING
        assert "gpt-4o-mini" not in MODELS_SUPPORTING_THINKING


class TestThinkingConfigScenarios:
    """Test realistic scenarios for thinking configuration."""

    def test_create_local_agent_with_thinking(self):
        """Test creating a local agent with thinking enabled."""
        # Simulate creating a local agent with Claude Sonnet and thinking
        model = "claude-sonnet-4.5"
        enable_thinking = True
        thinking_level = ThinkingLevel.LOW

        normalized_enable, normalized_level = _normalize_thinking_config(model, enable_thinking, thinking_level)

        # Should preserve thinking configuration
        assert normalized_enable is True
        assert normalized_level == "low"

    def test_create_remote_agent_ignores_thinking(self):
        """Test that remote agents don't have thinking config (no model field)."""
        # Remote agents don't have model field, so thinking config should be None
        model = None
        enable_thinking = True
        thinking_level = ThinkingLevel.MEDIUM

        normalized_enable, normalized_level = _normalize_thinking_config(model, enable_thinking, thinking_level)

        # Should return None for both (no model = no thinking support)
        assert normalized_enable is None
        assert normalized_level is None

    def test_switch_from_thinking_to_non_thinking_model(self):
        """Test switching from Claude to GPT-4o clears thinking config."""
        # Start with Claude + thinking
        model_before = "claude-sonnet-4.5"
        enable_before = True
        level_before = ThinkingLevel.MEDIUM

        # Switch to GPT-4o (doesn't support thinking)
        model_after = "gpt4o"
        enable_after = True
        level_after = ThinkingLevel.MEDIUM

        normalized_enable, normalized_level = _normalize_thinking_config(model_after, enable_after, level_after)

        # Should clear thinking config
        assert normalized_enable is None
        assert normalized_level is None

    def test_default_thinking_disabled_on_create(self):
        """Test that thinking is disabled by default when creating agents."""
        model = "claude-sonnet-4.5"
        enable_thinking = None  # Not specified
        thinking_level = None  # Not specified

        normalized_enable, normalized_level = _normalize_thinking_config(model, enable_thinking, thinking_level)

        # Should preserve None values (defaults will be applied in database)
        assert normalized_enable is None
        assert normalized_level is None
