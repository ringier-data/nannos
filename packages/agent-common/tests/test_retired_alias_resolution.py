"""Tests for _resolve_alias — degrading a retired model alias to the configured default.

Past the registry check the gateway has ALREADY told us the alias doesn't exist, so passing
it through is a guaranteed rejection. These tests pin the behaviour that decides whether the
call survives that: the second defaults read, which separates a defaults map that is empty
because nothing is configured from one that is empty because the first read landed badly.
"""

import time


from agent_common.core import model_factory as mf


REGISTERED = {"claude-sonnet-4-6": {}, "claude-haiku-4-5": {}}


def test_registered_alias_passes_through_untouched(seed_gateway, seed_defaults):
    seed_gateway(REGISTERED)
    seed_defaults({"chat": "claude-sonnet-4-6"})
    assert mf.resolve_chat_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_retired_alias_degrades_to_chat_default(seed_gateway, seed_defaults):
    seed_gateway(REGISTERED)
    seed_defaults({"chat": "claude-sonnet-4-6"})
    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4-6"


def test_unreadable_registry_passes_through(seed_defaults):
    """The gateway is the authority; with no registry snapshot we can't claim retirement."""
    mf._GW_CACHE.clear()
    mf._GW_CACHE.update({"ts": time.monotonic(), "models": {}, "inflight": False, "last_error": OSError("boom")})
    seed_defaults({"chat": "claude-sonnet-4-6"})
    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4.5"


def test_empty_unerrored_defaults_are_re_read_before_failing_open(seed_gateway, seed_defaults, monkeypatch):
    """THE regression: a first-use defaults read that comes back empty-and-unerrored must be
    retried, not treated as 'no default configured'.

    An empty map with last_error None re-arms the cache as cold, so the second read refetches
    synchronously; giving up on the first read discards a default that is one call away.
    """
    seed_gateway(REGISTERED)
    seed_defaults({})  # empty, no error — the ambiguous state

    fetches: list[int] = []

    def _fetch():
        fetches.append(1)
        return {"chat": "claude-sonnet-4-6"}  # what the second read finds

    monkeypatch.setattr(mf, "_fetch_model_defaults", _fetch)

    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4-6"
    assert fetches, "the second read must actually re-fetch, not re-serve the empty snapshot"


def test_failed_defaults_fetch_does_not_retry(seed_gateway, seed_defaults, monkeypatch):
    """A recorded error means the endpoint was tried and failed — retrying in-line would
    hammer it during an outage. Back off (and log an ERROR) instead."""
    seed_gateway(REGISTERED)
    seed_defaults({}, last_error=OSError("console-backend unreachable"))

    def _fetch():
        raise AssertionError("must not refetch while backing off from a failed fetch")

    monkeypatch.setattr(mf, "_fetch_model_defaults", _fetch)

    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4.5"


def test_default_pointing_at_the_retired_alias_is_not_a_successor(seed_gateway, seed_defaults, caplog):
    """A default that still points at the alias being degraded away from is no way out — and
    says so, because the fix (repoint the default) differs from the one an unset default needs."""
    seed_gateway(REGISTERED)
    seed_defaults({"chat": "claude-sonnet-4.5"})
    with caplog.at_level("ERROR"):
        assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4.5"
    assert "still points at it" in caplog.text


def test_unregistered_default_is_not_a_successor(seed_gateway, seed_defaults, caplog):
    """Degrading onto a second dead alias just moves the rejection; the registry is in scope
    here, so a default that isn't registered must not be reported as a successful degrade."""
    seed_gateway(REGISTERED)
    seed_defaults({"chat": "also-retired"})
    with caplog.at_level("ERROR"):
        assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4.5"
    assert "falling back" not in caplog.text
    # A default IS set here — reporting it as unset would send an admin to the wrong screen.
    assert "no default is set" not in caplog.text
    assert "are not registered either" in caplog.text
    assert "also-retired" in caplog.text


def test_concurrent_refresh_landing_between_the_two_reads_is_picked_up(seed_gateway, seed_defaults, monkeypatch):
    """A refresh that lands after the first read must be used, not lost to a check-then-act on
    the first result.

    The refresh has to land BETWEEN the reads for this to mean anything — populating the map
    during the first read would let the retry be deleted with the test still green. So the
    stub answers empty once and populated thereafter, and the call count pins that both reads
    actually happened.
    """
    seed_gateway(REGISTERED)
    seed_defaults({})

    reads: list[int] = []

    def _defaults_with_a_refresh_landing_after_the_first_read():
        reads.append(1)
        return {} if len(reads) == 1 else {"chat": "claude-sonnet-4-6"}

    monkeypatch.setattr(mf, "_model_defaults", _defaults_with_a_refresh_landing_after_the_first_read)

    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4-6"
    assert len(reads) == 2, "the second read must happen — it is what sees the landed refresh"


def test_rearm_is_rate_limited(seed_gateway, seed_defaults, monkeypatch):
    """Re-population runs synchronously on the caller's thread (the event loop, in the
    orchestrator), so a persistently empty map must not put a fetch in front of every read."""
    seed_gateway(REGISTERED)
    seed_defaults({})
    monkeypatch.setattr(mf, "_last_defaults_rearm", time.monotonic())  # just re-armed

    fetches: list[int] = []
    monkeypatch.setattr(mf, "_fetch_model_defaults", lambda: (fetches.append(1), {})[1])

    mf.resolve_chat_model("claude-sonnet-4.5")
    assert not fetches, "a re-arm inside the floor window must be skipped"


def test_rearm_does_not_fire_while_backing_off_from_an_error(seed_defaults, monkeypatch):
    """A recorded error means the endpoint was tried and failed; re-arming would turn every
    read into a synchronous retry against an endpoint already known to be down."""
    seed_defaults({}, last_error=OSError("console-backend unreachable"))
    monkeypatch.setattr(mf, "_last_defaults_rearm", mf._COLD)
    assert mf._rearm_defaults_if_unconfirmed() is False
    assert mf._DEFAULTS_CACHE["ts"] != mf._COLD


def test_embedding_resolution_prefers_multimodal_role(seed_gateway, seed_defaults):
    seed_gateway({"gemini-embedding-2": {}, "titan-embed-text-v2:0": {}})
    seed_defaults({"embedding": "titan-embed-text-v2:0", "multimodal_embedding": "gemini-embedding-2"})
    assert mf.resolve_embedding_model("retired-embed", multimodal=True) == "gemini-embedding-2"
    assert mf.resolve_embedding_model("retired-embed") == "titan-embed-text-v2:0"
