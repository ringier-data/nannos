"""Unit tests for the destructive-verb safety floor in the tool risk scorer.

Regression: `alloy-riad_delete_campaign_by_id` was LLM-scored 0.75 (< the 0.80
HITL threshold) and executed a real delete without asking. The floor guarantees a
clearly-destructive op can't drop below the gate on the strength of an LLM
estimate alone.
"""

import pytest

from agent_common.core.tool_risk_scorer import (
    _DESTRUCTIVE_FLOOR_SCORE,
    _destructive_floor,
    _deterministic_fallback,
    score_tool_risk,
)


def test_destructive_floor_flags_irreversible_verbs():
    assert _destructive_floor("alloy-riad_delete_campaign_by_id") == _DESTRUCTIVE_FLOOR_SCORE
    assert _destructive_floor("remove_user") == _DESTRUCTIVE_FLOOR_SCORE
    assert _destructive_floor("drop_table") == _DESTRUCTIVE_FLOOR_SCORE
    assert _destructive_floor("destroy_index") == _DESTRUCTIVE_FLOOR_SCORE


def test_destructive_floor_is_above_default_gate():
    # Must exceed the default HITL threshold (0.80) so it always interrupts.
    assert _DESTRUCTIVE_FLOOR_SCORE > 0.80


def test_destructive_floor_ignores_reads_and_writes():
    assert _destructive_floor("alloy-riad_get_campaign_by_id") == 0.0
    assert _destructive_floor("alloy-riad_put_campaign_by_id") == 0.0
    assert _destructive_floor("list_customers") == 0.0
    # Narrow by design: 'run'/'exec' are NOT floored (avoid over-gating reads).
    assert _destructive_floor("run_report") == 0.0


def test_deterministic_fallback_still_scores_destructive_high():
    # The fallback path (LLM unavailable) already floors these; unchanged.
    assert _deterministic_fallback("alloy-riad_delete_campaign_by_id") == 0.95
    assert _deterministic_fallback("get_campaign") == 0.3


# --- client_action deterministic gating (single HITL path for on-screen actions) ---


@pytest.mark.asyncio
async def test_client_action_apply_gates_but_benign_kinds_do_not():
    """client_action is the ONLY HITL for on-screen actions (no SDK card). It's
    scored deterministically by kind — never via LLM/cache — so `apply` always
    interrupts while `highlight`/`navigate` never do. Scored even with cache=None."""
    THRESHOLD = 0.80
    for kind in ("apply", "refresh", "invalidate"):
        score, entry = await score_tool_risk("client_action", {"kind": kind}, cache=None)
        assert score >= THRESHOLD, (kind, score)
        assert entry is not None and entry.allowed_actions == ["approve", "reject"]
    for kind in ("highlight", "navigate"):
        score, _ = await score_tool_risk("client_action", {"kind": kind}, cache=None)
        assert score < THRESHOLD, (kind, score)


@pytest.mark.asyncio
async def test_client_action_unknown_kind_fails_safe():
    # An unrecognized/new kind gates rather than slipping through.
    score, _ = await score_tool_risk("client_action", {"kind": "franticize"}, cache=None)
    assert score >= 0.80
