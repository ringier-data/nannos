"""Shared fixtures for agent-common's tests.

model_factory's caches are module-level process state: two dicts plus the re-arm floor's
timestamp. A test that seeds any of them and doesn't put it back changes what the NEXT test
observes — and the floor in particular fails silently, by making a re-arm a test depends on
simply not happen. That only shows up when the whole suite runs in one process, which is the
worst place to discover it, so restoring is autouse here rather than per-file.
"""

import time

import pytest

from agent_common.core import model_factory as mf


@pytest.fixture(autouse=True)
def restore_model_factory_caches():
    """Snapshot and restore every piece of model_factory's global cache state."""
    gw, defaults = dict(mf._GW_CACHE), dict(mf._DEFAULTS_CACHE)
    rearm = mf._last_defaults_rearm
    yield
    mf._GW_CACHE.clear()
    mf._GW_CACHE.update(gw)
    mf._DEFAULTS_CACHE.clear()
    mf._DEFAULTS_CACHE.update(defaults)
    mf._last_defaults_rearm = rearm


@pytest.fixture
def seed_gateway():
    """Seed the gateway registry snapshot. A fresh ts means _refresh_if_stale serves it
    without firing a (network) refresh."""

    def _seed(models: dict, last_error=None):
        mf._GW_CACHE.clear()
        mf._GW_CACHE.update(
            {"ts": time.monotonic(), "models": models, "inflight": False, "last_error": last_error}
        )

    return _seed


@pytest.fixture
def seed_defaults():
    """Seed the model-defaults snapshot, and clear the re-arm floor so a test that expects an
    inline re-arm gets one regardless of what ran before it."""

    def _seed(defaults: dict, last_error=None):
        mf._DEFAULTS_CACHE.clear()
        mf._DEFAULTS_CACHE.update(
            {"ts": time.monotonic(), "defaults": defaults, "inflight": False, "last_error": last_error}
        )
        mf._last_defaults_rearm = mf._COLD

    return _seed
