"""Tests for _resolve_alias — degrading a retired model alias to the configured default.

Past the registry check the gateway has ALREADY told us the alias doesn't exist, so passing
it through is a guaranteed rejection. These tests pin the behaviour that decides whether the
call survives that: the second defaults read, which separates a defaults map that is empty
because nothing is configured from one that is empty because the first read landed badly.
"""

import time

import pytest

from agent_common.core import model_factory as mf


@pytest.fixture(autouse=True)
def restore_caches():
    gw, defaults = dict(mf._GW_CACHE), dict(mf._DEFAULTS_CACHE)
    yield
    mf._GW_CACHE.clear()
    mf._GW_CACHE.update(gw)
    mf._DEFAULTS_CACHE.clear()
    mf._DEFAULTS_CACHE.update(defaults)


def _seed_gateway(models: dict):
    # Fresh ts so _refresh_if_stale serves this snapshot without firing a (network) refresh.
    mf._GW_CACHE.clear()
    mf._GW_CACHE.update({"ts": time.monotonic(), "models": models, "inflight": False, "last_error": None})


def _seed_defaults(defaults: dict, last_error=None):
    mf._DEFAULTS_CACHE.clear()
    mf._DEFAULTS_CACHE.update(
        {"ts": time.monotonic(), "defaults": defaults, "inflight": False, "last_error": last_error}
    )


REGISTERED = {"claude-sonnet-4-6": {}, "claude-haiku-4-5": {}}


def test_registered_alias_passes_through_untouched():
    _seed_gateway(REGISTERED)
    _seed_defaults({"chat": "claude-sonnet-4-6"})
    assert mf.resolve_chat_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_retired_alias_degrades_to_chat_default():
    _seed_gateway(REGISTERED)
    _seed_defaults({"chat": "claude-sonnet-4-6"})
    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4-6"


def test_unreadable_registry_passes_through():
    """The gateway is the authority; with no registry snapshot we can't claim retirement."""
    mf._GW_CACHE.clear()
    mf._GW_CACHE.update({"ts": time.monotonic(), "models": {}, "inflight": False, "last_error": OSError("boom")})
    _seed_defaults({"chat": "claude-sonnet-4-6"})
    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4.5"


def test_empty_unerrored_defaults_are_re_read_before_failing_open(monkeypatch):
    """THE regression: a first-use defaults read that comes back empty-and-unerrored must be
    retried, not treated as 'no default configured'.

    An empty map with last_error None re-arms the cache as cold, so the second read refetches
    synchronously; giving up on the first read discards a default that is one call away.
    """
    _seed_gateway(REGISTERED)
    _seed_defaults({})  # empty, no error — the ambiguous state

    fetches: list[int] = []

    def _fetch():
        fetches.append(1)
        return {"chat": "claude-sonnet-4-6"}  # what the second read finds

    monkeypatch.setattr(mf, "_fetch_model_defaults", _fetch)

    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4-6"
    assert fetches, "the second read must actually re-fetch, not re-serve the empty snapshot"


def test_failed_defaults_fetch_does_not_retry(monkeypatch):
    """A recorded error means the endpoint was tried and failed — retrying in-line would
    hammer it during an outage. Back off (and log an ERROR) instead."""
    _seed_gateway(REGISTERED)
    _seed_defaults({}, last_error=OSError("console-backend unreachable"))

    def _fetch():
        raise AssertionError("must not refetch while backing off from a failed fetch")

    monkeypatch.setattr(mf, "_fetch_model_defaults", _fetch)

    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4.5"


def test_default_pointing_at_the_retired_alias_is_not_a_successor():
    """A default that still points at the alias being degraded away from is no way out."""
    _seed_gateway(REGISTERED)
    _seed_defaults({"chat": "claude-sonnet-4.5"})
    assert mf.resolve_chat_model("claude-sonnet-4.5") == "claude-sonnet-4.5"


def test_embedding_resolution_prefers_multimodal_role():
    _seed_gateway({"gemini-embedding-2": {}, "titan-embed-text-v2:0": {}})
    _seed_defaults({"embedding": "titan-embed-text-v2:0", "multimodal_embedding": "gemini-embedding-2"})
    assert mf.resolve_embedding_model("retired-embed", multimodal=True) == "gemini-embedding-2"
    assert mf.resolve_embedding_model("retired-embed") == "titan-embed-text-v2:0"
