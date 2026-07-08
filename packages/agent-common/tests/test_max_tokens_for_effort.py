"""Tests for max_tokens_for_effort — the reasoning-effort → output-budget mapping.

When reasoning is on, thinking tokens share the output budget with the visible response.
Without an explicit max_tokens the gateway applies a low default (~4096), so a deep-reasoning
turn can spend the whole budget thinking and get cut off before producing an answer. The
mapping gives higher effort tiers proportionally more headroom; reasoning-off turns get
None so the gateway default is left untouched.
"""

import pytest

from agent_common.core.model_factory import (
    _DEFAULT_REASONING_MAX_TOKENS,
    _MAX_TOKENS_BY_EFFORT,
    max_tokens_for_effort,
)


def test_none_effort_leaves_max_tokens_unset():
    # Reasoning off → don't raise (or lower) a non-reasoning model's own output cap.
    assert max_tokens_for_effort(None) is None
    assert max_tokens_for_effort("") is None


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh"])
def test_every_known_effort_maps_to_its_ceiling(effort):
    assert max_tokens_for_effort(effort) == _MAX_TOKENS_BY_EFFORT[effort]


def test_ceiling_grows_with_effort():
    # Deeper reasoning needs more room above the thinking budget for the answer.
    assert max_tokens_for_effort("low") < max_tokens_for_effort("medium") < max_tokens_for_effort("high")


def test_xhigh_gets_the_most_headroom():
    # "Extra high" is the UI's deepest tier and the one that truncated in production.
    assert max_tokens_for_effort("xhigh") == max(_MAX_TOKENS_BY_EFFORT.values())


def test_unknown_effort_falls_back_to_default():
    assert max_tokens_for_effort("mystery-tier") == _DEFAULT_REASONING_MAX_TOKENS
