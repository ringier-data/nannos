"""Unit tests for sub-agent thinking configuration normalization."""

from console_backend.models.sub_agent import ThinkingLevel
from console_backend.services.sub_agent_service import _normalize_thinking_config

# A representative live capability set, as ModelGatewayService.thinking_capable_aliases()
# would return it. The set is the source of truth — not a hardcoded module constant.
SUPPORTED = {"claude-sonnet-4.5", "claude-sonnet-4.6", "claude-haiku-4-5", "gemini-3.1-pro-preview"}


class TestNormalizeThinkingConfig:
    """Test _normalize_thinking_config() function."""

    def test_claude_sonnet_supports_thinking(self):
        """A model present in the live set keeps its thinking config."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            model_tier=None,
            enable_thinking=True,
            thinking_level=ThinkingLevel.MEDIUM,
            supported_models=SUPPORTED,
        )

        assert enable is True
        assert level == "medium"

    def test_gemini_model_supports_thinking(self):
        """A Gemini model in the live set keeps its thinking config."""
        enable, level = _normalize_thinking_config(
            model="gemini-3.1-pro-preview",
            model_tier=None,
            enable_thinking=True,
            thinking_level=ThinkingLevel.HIGH,
            supported_models=SUPPORTED,
        )

        assert enable is True
        assert level == "high"

    def test_unsupported_model_returns_none(self):
        """A model absent from the live set has its thinking config cleared."""
        for model in ["gpt-4o", "gpt-4o-mini", "unsupported-model"]:
            enable, level = _normalize_thinking_config(
                model=model,
                model_tier=None,
                enable_thinking=True,
                thinking_level=ThinkingLevel.LOW,
                supported_models=SUPPORTED,
            )

            assert enable is None
            assert level is None

    def test_unknown_capability_preserves_choice(self):
        """When the live set is unavailable (None), the user's choice is preserved, not dropped.

        Regression guard: a transient gateway failure must never silently wipe a valid
        Extended Thinking config (the original bug).
        """
        enable, level = _normalize_thinking_config(
            model="some-newly-registered-alias",
            model_tier=None,
            enable_thinking=True,
            thinking_level=ThinkingLevel.HIGH,
            supported_models=None,
        )

        assert enable is True
        assert level == "high"

    def test_disabled_thinking_returns_false_and_none(self):
        """Test that disabled thinking returns False and None for level."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            model_tier=None,
            enable_thinking=False,
            thinking_level=ThinkingLevel.MEDIUM,
            supported_models=SUPPORTED,
        )

        assert enable is False
        assert level is None

    def test_none_enable_thinking_preserves_behavior(self):
        """Test that None enable_thinking is preserved for supported models."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            model_tier=None,
            enable_thinking=None,
            thinking_level=None,
            supported_models=SUPPORTED,
        )

        assert enable is None
        assert level is None

    def test_thinking_level_enum_to_string_conversion(self):
        """Test that ThinkingLevel enum is converted to string."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            model_tier=None,
            enable_thinking=True,
            thinking_level=ThinkingLevel.LOW,
            supported_models=SUPPORTED,
        )

        assert enable is True
        assert level == "low"  # String, not enum
        assert isinstance(level, str)

    def test_thinking_level_string_passthrough(self):
        """Test that string thinking level is passed through unchanged."""
        enable, level = _normalize_thinking_config(
            model="claude-sonnet-4.5",
            model_tier=None,
            enable_thinking=True,
            thinking_level="high",
            supported_models=SUPPORTED,
        )

        assert enable is True
        assert level == "high"

    def test_none_model_with_thinking_enabled(self):
        """A None model with no tier (inherit the orchestrator) clears thinking regardless."""
        enable, level = _normalize_thinking_config(
            model=None,
            model_tier=None,
            enable_thinking=True,
            thinking_level=ThinkingLevel.MEDIUM,
            supported_models=SUPPORTED,
        )

        assert enable is None
        assert level is None

    def test_tier_bound_agent_preserves_thinking(self):
        """A tier-bound agent (model=None, model_tier set) keeps its thinking config.

        Regression guard: a tier resolves to a concrete model at runtime, so there is no
        alias to check here. Previously the model-less branch wiped thinking for every
        tier-bound agent — the user's choice must be preserved instead (the original bug).
        """
        for tier in ["low", "standard", "premium"]:
            enable, level = _normalize_thinking_config(
                model=None,
                model_tier=tier,
                enable_thinking=True,
                thinking_level=ThinkingLevel.HIGH,
                supported_models=SUPPORTED,
            )

            assert enable is True, tier
            assert level == "high", tier

    def test_tier_bound_agent_with_thinking_disabled(self):
        """A tier-bound agent with thinking off normalizes level to None (but keeps False)."""
        enable, level = _normalize_thinking_config(
            model=None,
            model_tier="premium",
            enable_thinking=False,
            thinking_level=ThinkingLevel.MEDIUM,
            supported_models=SUPPORTED,
        )

        assert enable is False
        assert level is None

    def test_tier_bound_unaffected_by_capability_set(self):
        """A tier alias is never matched against supported_models (no concrete alias here)."""
        enable, level = _normalize_thinking_config(
            model=None,
            model_tier="standard",
            enable_thinking=True,
            thinking_level=ThinkingLevel.LOW,
            # Even an empty capability set must not wipe a tier-bound choice.
            supported_models=set(),
        )

        assert enable is True
        assert level == "low"


class TestThinkingConfigScenarios:
    """Test realistic scenarios for thinking configuration."""

    def test_create_local_agent_with_thinking(self):
        """Test creating a local agent with thinking enabled."""
        normalized_enable, normalized_level = _normalize_thinking_config(
            "claude-sonnet-4.5", None, True, ThinkingLevel.LOW, SUPPORTED
        )

        # Should preserve thinking configuration
        assert normalized_enable is True
        assert normalized_level == "low"

    def test_create_remote_agent_ignores_thinking(self):
        """Test that remote agents don't have thinking config (no model, no tier)."""
        # Remote agents don't have a model field, so thinking config should be None
        normalized_enable, normalized_level = _normalize_thinking_config(
            None, None, True, ThinkingLevel.MEDIUM, SUPPORTED
        )

        # Should return None for both (no model and no tier = no thinking support)
        assert normalized_enable is None
        assert normalized_level is None

    def test_switch_from_thinking_to_non_thinking_model(self):
        """Test switching from Claude to GPT-4o clears thinking config."""
        normalized_enable, normalized_level = _normalize_thinking_config(
            "gpt-4o", None, True, ThinkingLevel.MEDIUM, SUPPORTED
        )

        # Should clear thinking config
        assert normalized_enable is None
        assert normalized_level is None

    def test_default_thinking_disabled_on_create(self):
        """Test that thinking is disabled by default when creating agents."""
        normalized_enable, normalized_level = _normalize_thinking_config(
            "claude-sonnet-4.5", None, None, None, SUPPORTED
        )

        # Should preserve None values (defaults will be applied in database)
        assert normalized_enable is None
        assert normalized_level is None

