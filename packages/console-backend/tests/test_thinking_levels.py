"""Unit tests for thinking_levels_for() — reasoning efforts grounded in capability flags."""

from console_backend.services.model_gateway_service import thinking_levels_for


class TestThinkingLevelsFor:
    def test_non_reasoning_model_returns_empty(self):
        assert thinking_levels_for({"supports_reasoning": False}) == []
        assert thinking_levels_for({}) == []

    def test_declared_efforts_are_returned_in_display_order(self):
        """Explicit per-effort flags are trusted verbatim (and ordered)."""
        info = {
            "supports_reasoning": True,
            "supports_minimal_reasoning_effort": True,
            "supports_low_reasoning_effort": True,
            "supports_high_reasoning_effort": True,
        }
        assert thinking_levels_for(info) == ["minimal", "low", "high"]

    def test_low_only_model_is_not_over_reported(self):
        """The core fix: a model that only declares 'low' must NOT be offered medium/high."""
        info = {"supports_reasoning": True, "supports_low_reasoning_effort": True}
        assert thinking_levels_for(info) == ["low"]

    def test_absent_low_flag_no_longer_implies_low(self):
        """Old code treated a missing supports_low flag as supported; now only declared
        efforts count, so declaring only 'high' yields exactly ['high']."""
        info = {"supports_reasoning": True, "supports_high_reasoning_effort": True}
        assert thinking_levels_for(info) == ["high"]

    def test_xhigh_declared(self):
        info = {"supports_high_reasoning_effort": True, "supports_xhigh_reasoning_effort": True}
        assert thinking_levels_for(info) == ["high", "xhigh"]

    def test_reasoning_without_per_effort_detail_falls_back_to_baseline(self):
        """A model that says it reasons but enumerates no efforts gets the baseline tiers."""
        assert thinking_levels_for({"supports_reasoning": True}) == ["low", "medium", "high"]

    def test_bare_none_or_max_flag_still_counts_as_reasoning(self):
        """none/max aren't user-selectable tiers, but they still signal a reasoning model
        → baseline fallback rather than empty."""
        assert thinking_levels_for({"supports_none_reasoning_effort": True}) == ["low", "medium", "high"]
        assert thinking_levels_for({"supports_max_reasoning_effort": True}) == ["low", "medium", "high"]
