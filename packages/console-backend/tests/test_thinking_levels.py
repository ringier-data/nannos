"""Unit tests for thinking_levels_for() — reasoning efforts grounded in capability flags."""

from console_backend.services.model_gateway_service import thinking_levels_for


class TestThinkingLevelsFor:
    def test_non_reasoning_model_returns_empty(self):
        assert thinking_levels_for({"supports_reasoning": False}) == []
        assert thinking_levels_for({}) == []

    def test_reasoning_model_gets_the_portable_tiers(self):
        """A model that says it reasons but flags no tier gets low/medium/high."""
        assert thinking_levels_for({"supports_reasoning": True}) == ["low", "medium", "high"]

    def test_flagged_extra_tiers_are_added_to_the_portable_ones(self):
        """The map flags only the non-portable tiers; flagging one never removes low/medium/high."""
        info = {"supports_reasoning": True, "supports_xhigh_reasoning_effort": True}
        assert thinking_levels_for(info) == ["low", "medium", "high", "xhigh"]
        info = {"supports_reasoning": True, "supports_minimal_reasoning_effort": True}
        assert thinking_levels_for(info) == ["minimal", "low", "medium", "high"]

    def test_gpt_6_sol_flags_from_the_model_map(self):
        """Regression: the map's gpt-6-sol entry used to yield ["xhigh"] alone."""
        info = {
            "supports_reasoning": True,
            "supports_none_reasoning_effort": True,
            "supports_minimal_reasoning_effort": False,
            "supports_low_reasoning_effort": None,
            "supports_xhigh_reasoning_effort": True,
            "supports_max_reasoning_effort": True,
        }
        assert thinking_levels_for(info) == ["low", "medium", "high", "xhigh"]

    def test_claude_sonnet_5_flags_from_the_model_map(self):
        info = {
            "supports_reasoning": True,
            "supports_xhigh_reasoning_effort": True,
            "supports_max_reasoning_effort": True,
        }
        assert thinking_levels_for(info) == ["low", "medium", "high", "xhigh"]

    def test_explicit_false_excludes_a_portable_tier(self):
        """The map excludes a tier with False (it does so for low on a few entries)."""
        info = {"supports_reasoning": True, "supports_low_reasoning_effort": False}
        assert thinking_levels_for(info) == ["medium", "high"]

    def test_extra_tiers_need_an_explicit_true(self):
        """minimal/xhigh are never inferred: absent or False means not offered."""
        info = {
            "supports_reasoning": True,
            "supports_minimal_reasoning_effort": False,
            "supports_xhigh_reasoning_effort": None,
        }
        assert thinking_levels_for(info) == ["low", "medium", "high"]

    def test_explicit_false_overrides_declared_efforts(self):
        """An admin-stored supports_reasoning=False (console capability toggle) shadows the
        cost map entirely — thinking is off even if per-effort flags were merged in."""
        info = {
            "supports_reasoning": False,
            "supports_low_reasoning_effort": True,
            "supports_xhigh_reasoning_effort": True,
        }
        assert thinking_levels_for(info) == []

    def test_a_flagged_tier_alone_counts_as_reasoning(self):
        """none/max/xhigh aren't all user-selectable tiers, but any of them signals a reasoning
        model even without supports_reasoning."""
        assert thinking_levels_for({"supports_none_reasoning_effort": True}) == ["low", "medium", "high"]
        assert thinking_levels_for({"supports_max_reasoning_effort": True}) == ["low", "medium", "high"]
        assert thinking_levels_for({"supports_xhigh_reasoning_effort": True}) == ["low", "medium", "high", "xhigh"]

    def test_false_flags_alone_do_not_make_a_model_reason(self):
        assert thinking_levels_for({"supports_minimal_reasoning_effort": False}) == []
