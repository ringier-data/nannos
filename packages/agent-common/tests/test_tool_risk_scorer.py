"""Unit tests for the destructive-verb safety floor in the tool risk scorer.

Regression: `alloy-riad_delete_campaign_by_id` was LLM-scored 0.75 (< the 0.80
HITL threshold) and executed a real delete without asking. The floor guarantees a
clearly-destructive op can't drop below the gate on the strength of an LLM
estimate alone.
"""

import pytest

from agent_common.core.tool_risk_cache import ToolRiskEntry

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


@pytest.mark.asyncio
async def test_destructive_floor_applies_on_cache_hit():
    """A persisted under-rating (the real incident: delete_* stored at 0.75 with an
    unchanged schema hash) never reaches the LLM branch — the floor must hold on the
    cache-hit path too."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    now = datetime.now(timezone.utc)
    stale = ToolRiskEntry(
        base_score=0.75, risk_factors={}, allowed_actions=["approve", "reject"], schema_hash="",
        updated_at=now, last_accessed_at=now,
    )
    cache = MagicMock()
    cache.get.return_value = stale
    score, entry = await score_tool_risk("alloy-riad_delete_campaign_by_id", {"id": 1}, cache=cache)
    assert score == _DESTRUCTIVE_FLOOR_SCORE
    assert entry is stale
    # Non-destructive cached scores are returned as stored.
    cache.get.return_value = ToolRiskEntry(
        base_score=0.2, risk_factors={}, allowed_actions=["approve", "reject"], schema_hash="",
        updated_at=now, last_accessed_at=now,
    )
    score, _ = await score_tool_risk("alloy-riad_get_campaign_by_id", {}, cache=cache)
    assert score == 0.2


@pytest.mark.asyncio
async def test_unfetchable_tool_is_never_classified_or_persisted(monkeypatch):
    """A call whose tool cannot be fetched has no description and no schema, so the
    classification would be derived from its name alone — and used to be cached AND
    persisted with an empty schema_hash, indistinguishable from a real profile.
    """
    from unittest.mock import MagicMock

    async def fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the LLM must not be asked to classify a tool nobody could fetch")

    monkeypatch.setattr("agent_common.core.tool_risk_scorer._score_tool_via_llm", fail)

    cache = MagicMock()
    cache.get.return_value = None

    score, entry = await score_tool_risk("consoleCreateBugReport", {"description": "x"}, cache=cache)

    # Still gated — on the same name-based fallback the LLM branch uses on failure.
    assert score == _deterministic_fallback("consoleCreateBugReport")
    assert entry is None
    cache.put.assert_not_called()
    cache.persist_entry.assert_not_called()


@pytest.mark.asyncio
async def test_unfetchable_tool_still_honours_a_seeded_static_guard():
    """The seeded static guards (migration 057) carry schema_hash = '' on purpose, so
    the cache lookup must still run for a tool the caller could not fetch.
    """
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    now = datetime.now(timezone.utc)
    guard = ToolRiskEntry(
        base_score=1.0,
        risk_factors={},
        allowed_actions=["approve", "reject"],
        schema_hash="",
        updated_at=now,
        last_accessed_at=now,
    )
    cache = MagicMock()
    cache.get.return_value = guard

    score, entry = await score_tool_risk("read_personal_file", {"path": "x"}, cache=cache)

    assert score == 1.0
    assert entry is guard


@pytest.mark.asyncio
async def test_unfetchable_destructive_tool_still_clears_the_floor():
    """`_deterministic_fallback` checks its safe prefixes first, so a name like
    `read_and_remove_file` scores 0.3 on the name alone — under the 0.80 gate. The
    unfetchable path must apply the destructive floor, as the LLM and cache-hit paths do.
    """
    from unittest.mock import MagicMock

    cache = MagicMock()
    cache.get.return_value = None

    assert _deterministic_fallback("read_and_remove_file") == 0.3  # the trap
    score, entry = await score_tool_risk("read_and_remove_file", {"path": "/x"}, cache=cache)

    assert score == _DESTRUCTIVE_FLOOR_SCORE
    assert score > 0.80
    assert entry is None
    cache.persist_entry.assert_not_called()


def test_persist_entry_refuses_a_profile_with_no_schema_hash(caplog):
    """The structural half of "no row for a tool that could not be fetched".

    `score_tool_risk` returns before classifying an unfetchable tool, but that is one
    early return away from regressing. The cache refuses the write too, so a future
    caller cannot reintroduce a phantom row by skipping the check.
    """
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from agent_common.core.tool_risk_cache import ToolRiskCache

    now = datetime.now(timezone.utc)
    cache = ToolRiskCache()
    cache._api_client = MagicMock()

    no_schema = ToolRiskEntry(
        base_score=0.4, risk_factors={}, allowed_actions=["approve"], schema_hash="",
        updated_at=now, last_accessed_at=now,
    )
    cache.persist_entry("consoleCreateBugReport", "_self", no_schema)
    cache._api_client.upsert_score.assert_not_called()
