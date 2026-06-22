"""Tests for sub-agent model-lifecycle resolution (model_retired / effective_model).

console-backend is the single source of truth for what a retired sub-agent model degrades
to; annotate_config_version encodes that rule (and tier resolution) for both the console UI
and the orchestrator.
"""

from datetime import datetime, timezone

from console_backend.models.sub_agent import ModelTier, SubAgentConfigVersion
from console_backend.models.user import UserSettings
from console_backend.services.model_status import annotate_config_version, resolve_alias_status


def _cv(model: str | None = None, model_tier: ModelTier | None = None) -> SubAgentConfigVersion:
    return SubAgentConfigVersion(
        version=1, description="d", model=model, model_tier=model_tier, created_at=datetime.now(timezone.utc)
    )


def test_registered_model_runs_as_is():
    cv = _cv("claude-sonnet-4.5")
    annotate_config_version(cv, {"claude-sonnet-4.5", "gpt-4o"}, {"chat": "claude-sonnet-4.5"}, {})
    assert cv.model_retired is False
    assert cv.effective_model == "claude-sonnet-4.5"


def test_retired_model_degrades_to_chat_default():
    cv = _cv("claude-sonnet-4.0")
    annotate_config_version(cv, {"claude-sonnet-4.5", "gpt-4o"}, {"chat": "claude-sonnet-4.5"}, {})
    assert cv.model_retired is True
    assert cv.effective_model == "claude-sonnet-4.5"


def test_retired_model_without_default_passes_through():
    # No chat default configured: surface the dead alias rather than masking it (gateway 400s).
    cv = _cv("claude-sonnet-4.0")
    annotate_config_version(cv, {"gpt-4o"}, {}, {})
    assert cv.model_retired is True
    assert cv.effective_model == "claude-sonnet-4.0"


def test_no_model_inherits_orchestrator():
    cv = _cv(None)
    annotate_config_version(cv, {"gpt-4o"}, {"chat": "gpt-4o"}, {})
    assert cv.model_retired is False
    assert cv.effective_model is None


def test_unknown_registry_fails_open():
    # Gateway unreadable (registered=None): never flag retired; run the configured model.
    cv = _cv("claude-sonnet-4.0")
    annotate_config_version(cv, None, {"chat": "gpt-4o"}, {})
    assert cv.model_retired is False
    assert cv.effective_model == "claude-sonnet-4.0"


def test_none_config_version_is_noop():
    annotate_config_version(None, {"gpt-4o"}, {"chat": "gpt-4o"}, {})  # must not raise


# --- Within-tier degradation: a retired CONCRETE model degrades to its tier's successor ---
def test_retired_concrete_model_degrades_within_its_tier():
    # opus was the premium model and is now retired; gpt-4o is the current premium default.
    # The sub-agent must land on gpt-4o (premium successor), NOT the standard chat default.
    cv = _cv("claude-opus-4-8")
    defaults = {"chat": "claude-sonnet-4.6", "chat:premium": "gpt-4o"}
    alias_tiers = {"claude-opus-4-8": ["chat:premium"]}
    annotate_config_version(cv, {"claude-sonnet-4.6", "gpt-4o"}, defaults, alias_tiers)
    assert cv.model_retired is True
    assert cv.effective_model == "gpt-4o"


def test_retired_concrete_model_without_tier_history_uses_standard_default():
    cv = _cv("claude-opus-4-8")
    defaults = {"chat": "claude-sonnet-4.6", "chat:premium": "gpt-4o"}
    annotate_config_version(cv, {"claude-sonnet-4.6", "gpt-4o"}, defaults, {})  # no tier memory
    assert cv.effective_model == "claude-sonnet-4.6"


def test_retired_concrete_model_tier_successor_also_dead_falls_back_to_standard():
    # opus was premium, but the recorded premium successor is itself unregistered → standard.
    cv = _cv("claude-opus-4-8")
    defaults = {"chat": "claude-sonnet-4.6", "chat:premium": "gpt-4o-also-retired"}
    alias_tiers = {"claude-opus-4-8": ["chat:premium"]}
    annotate_config_version(cv, {"claude-sonnet-4.6"}, defaults, alias_tiers)
    assert cv.effective_model == "claude-sonnet-4.6"


def test_retired_standard_tier_model_uses_standard_default():
    # A model whose only tier was 'chat' (standard) degrades to the standard default (no change).
    cv = _cv("claude-sonnet-4.5")
    defaults = {"chat": "claude-sonnet-4.6", "chat:premium": "gpt-4o"}
    alias_tiers = {"claude-sonnet-4.5": ["chat"]}
    annotate_config_version(cv, {"claude-sonnet-4.6", "gpt-4o"}, defaults, alias_tiers)
    assert cv.effective_model == "claude-sonnet-4.6"


def test_retired_multi_tier_model_degrades_to_highest_tier_served():
    # A model that was BOTH low and premium degrades toward premium (highest), not low.
    cv = _cv("do-it-all")
    defaults = {"chat": "sonnet", "chat:low": "haiku", "chat:premium": "gpt-4o"}
    alias_tiers = {"do-it-all": ["chat:low", "chat:premium"]}
    annotate_config_version(cv, {"sonnet", "haiku", "gpt-4o"}, defaults, alias_tiers)
    assert cv.effective_model == "gpt-4o"


def test_retired_multi_tier_model_falls_through_to_next_live_tier():
    # Served low + premium; the premium successor is dead → fall through to the low successor.
    cv = _cv("do-it-all")
    defaults = {"chat": "sonnet", "chat:low": "haiku", "chat:premium": "gpt-4o-gone"}
    alias_tiers = {"do-it-all": ["chat:low", "chat:premium"]}
    annotate_config_version(cv, {"sonnet", "haiku"}, defaults, alias_tiers)
    assert cv.effective_model == "haiku"


# --- Tier resolution: a tier-bound version resolves to the tier's current default alias ---
def test_tier_resolves_to_its_slot():
    cv = _cv(model_tier=ModelTier.LOW)
    defaults = {"chat": "claude-sonnet-4.5", "chat:low": "claude-haiku-4-5"}
    annotate_config_version(cv, {"claude-sonnet-4.5", "claude-haiku-4-5"}, defaults, {})
    assert cv.model_retired is False
    assert cv.effective_model == "claude-haiku-4-5"


def test_standard_tier_uses_plain_chat_default():
    cv = _cv(model_tier=ModelTier.STANDARD)
    annotate_config_version(cv, {"claude-sonnet-4.5"}, {"chat": "claude-sonnet-4.5"}, {})
    assert cv.effective_model == "claude-sonnet-4.5"


def test_unset_tier_slot_falls_back_to_chat_default():
    # chat:premium not configured yet → degrade to the standard chat default rather than fail.
    cv = _cv(model_tier=ModelTier.PREMIUM)
    annotate_config_version(cv, {"claude-sonnet-4.5"}, {"chat": "claude-sonnet-4.5"}, {})
    assert cv.effective_model == "claude-sonnet-4.5"


# resolve_alias_status — the shared primitive used for both sub-agent and preferred models.
def test_resolve_alias_status_matrix():
    assert resolve_alias_status(None, {"gpt-4o"}, "gpt-4o") == (False, None)
    assert resolve_alias_status("gpt-4o", {"gpt-4o"}, "claude") == (False, "gpt-4o")
    assert resolve_alias_status("old", {"gpt-4o"}, "gpt-4o") == (True, "gpt-4o")
    assert resolve_alias_status("old", {"gpt-4o"}, None) == (True, "old")
    assert resolve_alias_status("old", None, "gpt-4o") == (False, "old")  # registry unknown → fail open


def test_user_settings_fields_default_safely():
    # A retired preferred_model resolves like any other alias; default fields stay safe.
    s = UserSettings(user_id="u", preferred_model="old")
    assert s.preferred_model_retired is False
    s.preferred_model_retired, s.effective_preferred_model = resolve_alias_status("old", {"gpt-4o"}, "gpt-4o")
    assert (s.preferred_model_retired, s.effective_preferred_model) == (True, "gpt-4o")
