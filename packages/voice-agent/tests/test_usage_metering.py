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


def test_tool_use_tokens_get_their_own_unit():
    """Kept separate from base_input_tokens so they stay visible in the breakdown and can
    be priced on their own line rather than merged into the context total."""
    um = types.UsageMetadata(
        prompt_tokens_details=[_modality("TEXT", 10)],
        tool_use_prompt_token_count=30,
    )
    assert usage_metadata_to_billing_units(um) == {
        "base_input_tokens": 10,
        "tool_use_input_tokens": 30,
    }


def test_cached_tokens_are_discounted_from_input_not_added_on_top():
    """promptTokenCount is cache-INCLUSIVE, so emitting a cache unit alongside the full
    prompt count billed the cached tokens twice — once at full rate, once at cache rate.

    Same defect the Model Gateway already fixed for normalized Anthropic usage; the
    convention there is that base input EXCLUDES cache.
    """
    um = types.UsageMetadata(
        prompt_token_count=1000,
        prompt_tokens_details=[_modality("TEXT", 1000)],
        cached_content_token_count=400,
        cache_tokens_details=[_modality("TEXT", 400)],
    )
    units = usage_metadata_to_billing_units(um)

    assert units == {"base_input_tokens": 600, "cache_read_input_tokens": 400}
    # The invariant that makes it not-double-counted:
    assert units["base_input_tokens"] + units["cache_read_input_tokens"] == 1000


def test_cached_audio_tokens_are_discounted_from_the_audio_unit():
    """Audio is 6x text, so discounting the wrong modality would mis-bill badly."""
    um = types.UsageMetadata(
        prompt_token_count=900,
        prompt_tokens_details=[_modality("AUDIO", 900)],
        cached_content_token_count=300,
        cache_tokens_details=[_modality("AUDIO", 300)],
    )
    assert usage_metadata_to_billing_units(um) == {
        "audio_input_tokens": 600,
        "cache_read_input_tokens": 300,
    }


def test_cached_tokens_without_modality_details_still_come_off_the_input():
    """No cache_tokens_details — the tokens must still be discounted, or they double-bill."""
    um = types.UsageMetadata(
        prompt_token_count=500,
        prompt_tokens_details=[_modality("AUDIO", 500)],
        cached_content_token_count=200,
    )
    units = usage_metadata_to_billing_units(um)

    assert units["cache_read_input_tokens"] == 200
    assert units.get("audio_input_tokens", 0) == 300
    assert sum(v for k, v in units.items() if k != "cache_read_input_tokens") == 300


def test_unexplainable_cache_count_never_under_bills():
    """If the numbers don't admit the subtraction, leave the input at full price."""
    um = types.UsageMetadata(
        prompt_token_count=100,
        prompt_tokens_details=[_modality("TEXT", 100)],
        cached_content_token_count=900,  # nonsensical: more cached than prompt
    )
    units = usage_metadata_to_billing_units(um)

    assert units["cache_read_input_tokens"] == 900
    assert units.get("base_input_tokens", 0) == 0  # discounted as far as it could go


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


def test_every_side_accumulates_because_context_is_re_billed_per_turn():
    """Both sides sum. Google documents that the Live API re-bills the whole context
    every turn ("You are charged per turn for all tokens present in the Session Context
    Window"), so each report's cumulative view of the context is a recurring charge, not
    a gauge to be read once.
    """
    agent = GeminiLiveAgent(session_id="call-4")
    _record(agent, types.UsageMetadata(prompt_token_count=100, response_token_count=250))
    _record(agent, types.UsageMetadata(prompt_token_count=250, response_token_count=118))

    entries = agent.build_usage_entries()

    assert entries[0]["billing_unit_breakdown"] == {
        "base_input_tokens": 350,   # 100 + 250 — re-billed, NOT max(100, 250)
        "base_output_tokens": 368,
    }


def test_compression_reduces_billed_input():
    """After a sliding-window compression the context shrinks, and the docs say the API
    "then bills subsequent turns only for the retained history plus any new tokens" —
    summing tracks that. Taking the max would have over-billed the compressed turns."""
    agent = GeminiLiveAgent(session_id="call-5")
    _record(agent, types.UsageMetadata(prompt_token_count=128_000))
    _record(agent, types.UsageMetadata(prompt_token_count=32_000))

    assert agent.build_usage_entries()[0]["billing_unit_breakdown"] == {
        "base_input_tokens": 160_000  # 128k + 32k, not max() == 128k
    }


def test_replays_the_measured_dev_call():
    """The real 10-turn call from 2026-09-01, as audio. Guards the whole fold end to end
    against the numbers we actually observed."""
    turns = [(126, 250), (509, 118), (764, 112), (968, 28), (1037, 187),
             (1321, 15), (1362, 6), (1397, 9), (1436, 0), (1490, 293)]
    agent = GeminiLiveAgent(session_id="call-real")
    for prompt, response in turns:
        _record(agent, types.UsageMetadata(
            prompt_tokens_details=[_modality("AUDIO", prompt)],
            response_tokens_details=([_modality("AUDIO", response)] if response else None),
        ))

    assert agent.build_usage_entries()[0]["billing_unit_breakdown"] == {
        "audio_input_tokens": 10_410,   # sum, not the 1_490 final context reading
        "audio_output_tokens": 1_018,
    }


def test_tool_tokens_stay_separate_from_the_context_total():
    """Tool-use tokens keep their own unit so they remain visible in the breakdown and
    separately priceable, instead of disappearing into the context count."""
    agent = GeminiLiveAgent(session_id="call-6")
    for ctx in (1000, 3000, 5000):
        _record(agent, types.UsageMetadata(
            prompt_tokens_details=[_modality("TEXT", ctx)],
            tool_use_prompt_token_count=30,
        ))

    units = agent.build_usage_entries()[0]["billing_unit_breakdown"]

    assert units["base_input_tokens"] == 9000        # 1000 + 3000 + 5000
    assert units["tool_use_input_tokens"] == 90      # 30 x 3, tracked separately


def test_fold_needs_no_per_unit_policy():
    """Every unit accumulates, including one we've never seen — so a newly-reported token
    type is billed rather than silently dropped, with no classification to maintain."""
    totals: dict[str, int] = {}
    fold_usage_into(totals, {"some_new_tokens": 5, "audio_input_tokens": 100})
    fold_usage_into(totals, {"some_new_tokens": 7, "audio_input_tokens": 250})

    assert totals == {"some_new_tokens": 12, "audio_input_tokens": 350}


def test_partial_modality_details_warn_with_the_shortfall(caplog):
    """Details that only partly explain prompt_token_count leave the rest unbilled: the
    all-or-nothing fallback check is suppressed as soon as anything was counted.

    Unreachable today (the only unmapped modalities are IMAGE/VIDEO/DOCUMENT and the Live
    session is audio-only), so this is deliberately warned rather than handled — the point
    is that the assumption fails loudly instead of quietly shrinking the bill.
    """
    um = types.UsageMetadata(
        prompt_tokens_details=[_modality("AUDIO", 300), _modality("VIDEO", 200)],
        prompt_token_count=500,
    )
    with caplog.at_level("WARNING"):
        units = usage_metadata_to_billing_units(um)

    assert units == {"audio_input_tokens": 300}
    assert "only 300 of 500" in caplog.text
    assert "200 unbilled" in caplog.text


def test_complete_details_do_not_warn(caplog):
    """The normal audio turn must stay silent — a warning that cries wolf is worthless."""
    um = types.UsageMetadata(
        prompt_tokens_details=[_modality("AUDIO", 1490)],
        response_tokens_details=[_modality("AUDIO", 293)],
        prompt_token_count=1490,
        response_token_count=293,
    )
    with caplog.at_level("WARNING"):
        usage_metadata_to_billing_units(um)

    assert caplog.text == ""


# ── generate_content shape (the tool risk scorer) ─────────────────────────────
#
# One mapper serves two models with two usage classes. Every test above builds
# types.UsageMetadata (the Live shape), which is exactly why the risk scorer's output
# tokens went unbilled unnoticed — GenerateContentResponseUsageMetadata names them
# candidates_*, not response_*.


def _gc_usage(**kwargs) -> types.GenerateContentResponseUsageMetadata:
    """The shape `client.aio.models.generate_content` actually returns."""
    return types.GenerateContentResponseUsageMetadata(**kwargs)


def test_risk_scorer_output_tokens_are_billed_from_candidates_fields():
    """generate_content reports output as candidates_*, not response_*. Reading only the
    Live names billed every risk-scorer call with input and ZERO output."""
    um = _gc_usage(
        prompt_token_count=900,
        candidates_token_count=40,
        candidates_tokens_details=[_modality("TEXT", 40)],
    )
    assert usage_metadata_to_billing_units(um) == {
        "base_input_tokens": 900,
        "base_output_tokens": 40,
    }


def test_risk_scorer_output_falls_back_to_flat_candidates_count():
    """Same fix on the no-details path."""
    um = _gc_usage(prompt_token_count=900, candidates_token_count=40)
    assert usage_metadata_to_billing_units(um) == {
        "base_input_tokens": 900,
        "base_output_tokens": 40,
    }


def test_thinking_tokens_use_the_platform_reasoning_unit():
    """Thoughts belong in `reasoning_output_tokens` — the platform's existing unit, priced
    on the gemini-3.x cards and labelled "Reasoning" in the console — not folded into base
    output. Thinking is on by default on both models, so leaving this unread under-billed
    the risk scorer on every call.
    """
    um = _gc_usage(
        prompt_token_count=900, candidates_token_count=40, thoughts_token_count=250,
        total_token_count=1190,  # 900 + 40 + 250 -> exclusive, the documented identity
    )
    assert usage_metadata_to_billing_units(um) == {
        "base_input_tokens": 900,
        "base_output_tokens": 40,
        "reasoning_output_tokens": 250,
    }


def test_inclusive_response_count_does_not_double_bill_thoughts():
    """Google documents thoughts as a separate addend, but LiteLLM checks the arithmetic
    rather than trusting it, having hit endpoints that disagree. So do we: when
    prompt + candidates + tool_use == total, thoughts are already inside the candidates
    count and must come OUT of base output.

    The invariant that matters: billed output is the same under both provider behaviours.
    """
    inclusive = _gc_usage(
        prompt_token_count=900, candidates_token_count=290, thoughts_token_count=250,
        total_token_count=1190,  # 900 + 290 == 1190 -> thoughts already inside
    )
    exclusive = _gc_usage(
        prompt_token_count=900, candidates_token_count=40, thoughts_token_count=250,
        total_token_count=1190,  # 900 + 40 + 250 -> thoughts additive
    )
    assert usage_metadata_to_billing_units(inclusive) == usage_metadata_to_billing_units(
        exclusive
    ) == {
        "base_input_tokens": 900,
        "base_output_tokens": 40,
        "reasoning_output_tokens": 250,
    }


def test_missing_total_falls_back_to_the_documented_exclusive_reading():
    """No total_token_count means the arithmetic check can't run; Google documents the
    exclusive identity, so thoughts are additive."""
    um = _gc_usage(prompt_token_count=900, candidates_token_count=40, thoughts_token_count=250)
    assert usage_metadata_to_billing_units(um) == {
        "base_input_tokens": 900,
        "base_output_tokens": 40,
        "reasoning_output_tokens": 250,
    }


def test_thoughts_do_not_trigger_a_false_shortfall_warning(caplog):
    """The candidates count excludes thoughts, so the shortfall check must count them as
    accounted-for or it cries wolf on every thinking call — which is every risk-scorer
    call. Uses the generate_content shape because that is the only path that thinks.
    """
    um = _gc_usage(
        prompt_tokens_details=[_modality("TEXT", 272)],
        candidates_tokens_details=[_modality("TEXT", 44)],
        prompt_token_count=272,
        candidates_token_count=520,   # a count that exceeds the details by exactly thoughts
        thoughts_token_count=476,
    )
    with caplog.at_level("WARNING"):
        usage_metadata_to_billing_units(um)

    assert "unbilled" not in caplog.text
