"""Token accounting is invisible until it is wrong, so it gets its own tests.

The integration tier reports per-test cost, and the one failure mode that matters
is a silent zero: a call whose usage nobody recorded looks exactly like a free
call. Aggregation is langchain-core's ``UsageMetadataCallbackHandler``; what is
tested here is the seam around it — the flat totals the eval report needs, and the
fallback for calls upstream declines to attribute.

No LLM, no gateway: `LLMResult` objects are built directly.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from tests.support.usage import UsageRecorder


def result(
    *,
    usage: dict | None = None,
    model_name: str | None = "claude-sonnet-4-6",
    llm_output: dict | None = None,
) -> LLMResult:
    """An `LLMResult` shaped like a chat response."""
    message = AIMessage(
        content="hi",
        usage_metadata=usage,
        response_metadata={"model_name": model_name} if model_name else {},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]], llm_output=llm_output)


def usage_metadata(input_tokens: int, output_tokens: int) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


# ---------------------------------------------------------------------------
# The normal path: upstream attributes it
# ---------------------------------------------------------------------------


def test_usage_metadata_is_attributed_upstream():
    recorder = UsageRecorder()

    recorder.on_llm_end(result(usage=usage_metadata(10, 5)))

    assert (recorder.input_tokens, recorder.output_tokens) == (10, 5)
    assert recorder.total_tokens == 15
    assert recorder.calls == 1
    assert recorder.unattributed_calls == 0


def test_repeated_calls_accumulate():
    recorder = UsageRecorder()

    recorder.on_llm_end(result(usage=usage_metadata(10, 5)))
    recorder.on_llm_end(result(usage=usage_metadata(3, 7)))

    assert (recorder.input_tokens, recorder.output_tokens) == (13, 12)
    assert recorder.unattributed_calls == 0


def test_usage_is_summed_across_models():
    """A turn can span models — the orchestrator's and an in-process sub-agent's."""
    recorder = UsageRecorder()

    recorder.on_llm_end(result(usage=usage_metadata(10, 5), model_name="claude-sonnet-4-6"))
    recorder.on_llm_end(result(usage=usage_metadata(1, 2), model_name="gemini-3-flash-preview"))

    assert (recorder.input_tokens, recorder.output_tokens) == (11, 7)
    assert set(recorder.usage_metadata) == {"claude-sonnet-4-6", "gemini-3-flash-preview"}


# ---------------------------------------------------------------------------
# The fallback: calls upstream declines
# ---------------------------------------------------------------------------


def test_usage_without_a_model_name_is_still_counted():
    """Upstream needs `usage_metadata` *and* a model_name, and records neither without.

    That would be a silent zero in the cost report — indistinguishable from a call
    that cost nothing.
    """
    recorder = UsageRecorder()

    recorder.on_llm_end(result(usage=usage_metadata(10, 5), model_name=None))

    assert (recorder.input_tokens, recorder.output_tokens) == (10, 5)
    assert recorder.unattributed_calls == 1
    assert recorder.usage_metadata == {}  # upstream really did decline it


def test_provider_level_token_usage_is_counted():
    recorder = UsageRecorder()

    recorder.on_llm_end(
        result(usage=None, llm_output={"token_usage": {"prompt_tokens": 8, "completion_tokens": 2}})
    )

    assert (recorder.input_tokens, recorder.output_tokens) == (8, 2)
    assert recorder.unattributed_calls == 1


def test_the_usage_spelling_is_also_read():
    recorder = UsageRecorder()

    recorder.on_llm_end(result(usage=None, llm_output={"usage": {"input_tokens": 4, "output_tokens": 1}}))

    assert (recorder.input_tokens, recorder.output_tokens) == (4, 1)


def test_nothing_reported_is_zero_and_flagged():
    """A zero means 'not reported', not 'free' — and the count says which."""
    recorder = UsageRecorder()

    recorder.on_llm_end(result(usage=None))

    assert recorder.total_tokens == 0
    assert recorder.unattributed_calls == 1


def test_an_attributed_call_is_never_also_counted_by_the_fallback():
    """The double-count guard. Both sources present; only one may be read."""
    recorder = UsageRecorder()

    recorder.on_llm_end(
        result(
            usage=usage_metadata(10, 5),
            llm_output={"token_usage": {"prompt_tokens": 999, "completion_tokens": 999}},
        )
    )

    assert (recorder.input_tokens, recorder.output_tokens) == (10, 5)
    assert recorder.unattributed_calls == 0


def test_a_repeat_of_the_same_model_and_usage_is_not_mistaken_for_a_decline():
    """The snapshot compares values, not just keys.

    Two identical calls to one model must both count — if the check were 'did a
    key appear', the second would look declined and be double-read.
    """
    recorder = UsageRecorder()

    recorder.on_llm_end(result(usage=usage_metadata(10, 5)))
    recorder.on_llm_end(result(usage=usage_metadata(10, 5)))

    assert (recorder.input_tokens, recorder.output_tokens) == (20, 10)
    assert recorder.unattributed_calls == 0
