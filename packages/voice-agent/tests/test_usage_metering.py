"""Tests for voice-agent usage metering (Gemini Live + tool risk scorer).

The voice agent bypasses the Model Gateway, so these mappings are the only thing
standing between a voice call and a $0 bill.
"""

from __future__ import annotations

from google.genai import types

from voice_agent.agent import (
    USAGE_PROVIDER,
    GeminiLiveAgent,
    fold_usage_into,
    usage_metadata_to_billing_units,
)


def _record(agent: GeminiLiveAgent, usage) -> None:
    """Feed one usage report through the same fold the receive loop uses."""
    fold_usage_into(agent.live_usage_totals, usage_metadata_to_billing_units(usage))


def _modality(name: str, count: int) -> types.ModalityTokenCount:
    return types.ModalityTokenCount(modality=name, token_count=count)


# ── usage_metadata_to_billing_units ───────────────────────────────────────────


def test_splits_audio_and_text_by_modality():
    """Audio and text are priced differently, so the split must survive."""
    um = types.UsageMetadata(
        prompt_token_count=1000,
        response_token_count=500,
        prompt_tokens_details=[_modality("AUDIO", 900), _modality("TEXT", 100)],
        response_tokens_details=[_modality("AUDIO", 480), _modality("TEXT", 20)],
    )
    assert usage_metadata_to_billing_units(um) == {
        "audio_input_tokens": 900,
        "base_input_tokens": 100,
        "audio_output_tokens": 480,
        "base_output_tokens": 20,
    }


def test_falls_back_to_flat_counts_when_details_absent():
    """Better to bill approximately as text than to lose the tokens entirely."""
    um = types.UsageMetadata(prompt_token_count=42, response_token_count=7)
    assert usage_metadata_to_billing_units(um) == {
        "base_input_tokens": 42,
        "base_output_tokens": 7,
    }


def test_flat_fallback_is_per_direction():
    """A present input detail list must not suppress the output fallback."""
    um = types.UsageMetadata(
        prompt_token_count=900,
        response_token_count=7,
        prompt_tokens_details=[_modality("AUDIO", 900)],
    )
    assert usage_metadata_to_billing_units(um) == {
        "audio_input_tokens": 900,
        "base_output_tokens": 7,
    }


def test_tool_use_and_cache_tokens_are_billed():
    um = types.UsageMetadata(
        prompt_tokens_details=[_modality("TEXT", 10)],
        tool_use_prompt_token_count=30,
        cached_content_token_count=200,
    )
    assert usage_metadata_to_billing_units(um) == {
        "base_input_tokens": 40,  # 10 text + 30 tool-use
        "cache_read_input_tokens": 200,
    }


def test_zero_and_missing_counts_are_omitted():
    """The backend rejects non-positive unit counts, so they must never be sent."""
    assert usage_metadata_to_billing_units(None) == {}
    assert usage_metadata_to_billing_units(types.UsageMetadata()) == {}
    assert usage_metadata_to_billing_units(
        types.UsageMetadata(prompt_token_count=0, response_token_count=5)
    ) == {"base_output_tokens": 5}


def test_unmapped_modality_is_not_folded_into_audio_or_text():
    """An unpriced modality must not silently inflate an audio/text bucket."""
    um = types.UsageMetadata(
        prompt_tokens_details=[_modality("AUDIO", 100), _modality("VIDEO", 999)],
    )
    assert usage_metadata_to_billing_units(um) == {"audio_input_tokens": 100}


# ── GeminiLiveAgent.build_usage_entries ───────────────────────────────────────


def test_build_usage_entries_reports_live_and_risk_scorer_separately():
    agent = GeminiLiveAgent(session_id="call-1")
    _record(agent, types.UsageMetadata(
        prompt_tokens_details=[_modality("AUDIO", 800)],
        response_tokens_details=[_modality("AUDIO", 400)],
    ))
    agent.risk_scorer_usage = {"base_input_tokens": 120, "base_output_tokens": 8}

    entries = agent.build_usage_entries()

    assert [e["model_name"] for e in entries] == [agent.model_id, "gemini-2.5-flash"]
    assert all(e["provider"] == USAGE_PROVIDER for e in entries)
    assert entries[0]["billing_unit_breakdown"] == {
        "audio_input_tokens": 800,
        "audio_output_tokens": 400,
    }
    assert entries[1]["billing_unit_breakdown"] == {
        "base_input_tokens": 120,
        "base_output_tokens": 8,
    }


def test_build_usage_entries_omits_risk_scorer_when_cache_was_warm():
    """A warm process scores no tools — an empty risk breakdown is normal."""
    agent = GeminiLiveAgent(session_id="call-2")
    _record(agent, types.UsageMetadata(prompt_token_count=10))
    agent.risk_scorer_usage = {}

    entries = agent.build_usage_entries()

    assert len(entries) == 1
    assert entries[0]["model_name"] == agent.model_id


def test_build_usage_entries_is_empty_when_nothing_was_captured():
    """No usage must yield no entries rather than a zero-count entry."""
    assert GeminiLiveAgent(session_id="call-3").build_usage_entries() == []


def test_input_side_is_a_gauge_and_output_side_accumulates():
    """Measured on a real call (2026-09-01): the two sides differ.

    `prompt_token_count` is a cumulative gauge of the context (rises monotonically, each
    rise = previous response + new audio), so it must NOT be summed. `response_token_count`
    is a per-turn delta (it decreases between turns), so it MUST be summed — snapshotting
    it dropped 71% of the output tokens, the expensive side at $12/1M.
    """
    agent = GeminiLiveAgent(session_id="call-4")
    _record(agent, types.UsageMetadata(prompt_token_count=100, response_token_count=250))
    _record(agent, types.UsageMetadata(prompt_token_count=250, response_token_count=118))

    entries = agent.build_usage_entries()

    assert entries[0]["billing_unit_breakdown"] == {
        "base_input_tokens": 250,   # gauge → max, NOT 350
        "base_output_tokens": 368,  # delta → sum
    }


def test_gauge_survives_a_context_compression_shrink():
    """Sliding-window compression can shrink the context; the peak was still processed."""
    agent = GeminiLiveAgent(session_id="call-5")
    _record(agent, types.UsageMetadata(prompt_token_count=128_000))
    _record(agent, types.UsageMetadata(prompt_token_count=32_000))

    assert agent.build_usage_entries()[0]["billing_unit_breakdown"] == {
        "base_input_tokens": 128_000
    }
