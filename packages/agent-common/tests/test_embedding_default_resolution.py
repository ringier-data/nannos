"""Tests for embedding_default_known_absent — the transient-vs-genuine distinction that lets
the document store self-heal instead of latching off on a cold start.

A cold/failed model-defaults fetch and a genuinely-unconfigured embedding default both make
get_default_embedding_model() return None; this helper tells them apart so callers retry the
former and settle on the latter.
"""

import time

import pytest

from agent_common.core import model_factory as mf


@pytest.fixture(autouse=True)
def restore_defaults_cache():
    orig = dict(mf._DEFAULTS_CACHE)
    yield
    mf._DEFAULTS_CACHE.clear()
    mf._DEFAULTS_CACHE.update(orig)


def _seed_defaults(defaults: dict, last_error):
    # Fresh ts so _refresh_if_stale serves this snapshot without firing a (network) refresh.
    mf._DEFAULTS_CACHE.clear()
    mf._DEFAULTS_CACHE.update(
        {"ts": time.monotonic(), "defaults": defaults, "inflight": False, "last_error": last_error}
    )


def test_known_absent_when_fetch_succeeded_and_no_embedding_default():
    """Defaults fetched OK but no embedding role set → genuinely absent (settle store-less)."""
    _seed_defaults({"chat": "claude-sonnet-4.5"}, last_error=None)
    assert mf.embedding_default_known_absent() is True


def test_not_absent_when_embedding_default_present():
    _seed_defaults({"embedding": "titan-embed-v2"}, last_error=None)
    assert mf.embedding_default_known_absent() is False


def test_not_absent_when_fetch_failed():
    """A failed fetch (last_error set) is transient — must NOT be reported as known-absent,
    or a cold-start hiccup would latch the store off for the process lifetime."""
    _seed_defaults({}, last_error=RuntimeError("console-backend unreachable"))
    assert mf.embedding_default_known_absent() is False
