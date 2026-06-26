"""Tests for the gateway/defaults cache refresh, focused on the cold-start race.

Regression guard: on a cold cache, a concurrent caller that arrives while the first
population is in flight must BLOCK until it lands, not return an empty snapshot. Warm
refreshes must never block (serve stale, refresh in the background).
"""

import threading
import time

from agent_common.core import model_factory as mf


def _cold_cache() -> dict:
    return {"ts": mf._COLD, "models": {}, "inflight": False, "last_error": None}


def test_cold_concurrent_callers_all_see_populated_cache():
    """The owner blocks populating; a second cold caller waits and sees the result, not {}."""
    cache = _cold_cache()
    cond = threading.Condition()
    in_fetch = threading.Event()
    release = threading.Event()
    fetch_calls = {"n": 0}

    def fetch():
        fetch_calls["n"] += 1
        in_fetch.set()  # owner is now inside the (blocking) cold population
        release.wait(2.0)
        return {"m": {"cap": True}}

    results: dict[str, dict] = {}

    def call(name: str):
        mf._refresh_if_stale(cache, "models", 60.0, cond, fetch)
        results[name] = dict(cache["models"])

    owner = threading.Thread(target=call, args=("A",))
    owner.start()
    assert in_fetch.wait(2.0)  # A owns the refresh and is blocked in fetch (inflight=True)

    # B arrives mid-cold-population. It must park on the condition, not return empty.
    second = threading.Thread(target=call, args=("B",))
    second.start()
    time.sleep(0.05)  # let B reach the wait
    assert second.is_alive()  # blocked — the pre-fix bug returned here with {}

    release.set()  # let the owner's fetch finish
    owner.join(2.0)
    second.join(2.0)

    assert fetch_calls["n"] == 1  # only the owner fetched; B waited on it
    assert results["A"] == {"m": {"cap": True}}
    assert results["B"] == {"m": {"cap": True}}  # the fix: B sees the populated registry


def test_cold_fetch_failure_releases_waiters():
    """A failed cold population still wakes cold waiters and records the error (fail-open)."""
    cache = _cold_cache()
    cond = threading.Condition()
    in_fetch = threading.Event()
    release = threading.Event()

    def fetch():
        in_fetch.set()
        release.wait(2.0)
        raise RuntimeError("gateway down")

    b_done = threading.Event()

    owner = threading.Thread(target=lambda: mf._refresh_if_stale(cache, "models", 60.0, cond, fetch))
    owner.start()
    assert in_fetch.wait(2.0)

    def call_b():
        mf._refresh_if_stale(cache, "models", 60.0, cond, fetch)
        b_done.set()

    second = threading.Thread(target=call_b)
    second.start()
    time.sleep(0.05)
    assert not b_done.is_set()  # B is blocked waiting on the cold population

    release.set()
    owner.join(2.0)
    second.join(2.0)

    assert b_done.is_set()  # released despite the failure (notify runs in finally)
    assert cache["last_error"] is not None
    assert cache["ts"] != mf._COLD  # ts advanced → back off a full TTL before retrying


def test_empty_registry_refetches_on_next_call(monkeypatch):
    """Regression: a freshly-registered model is visible on the very next request.

    A successfully-empty registry is the fresh-deploy bootstrap state. It must NOT latch
    behind _GW_TTL — otherwise the orchestrator keeps telling the user "no model
    configured" for up to a minute after the admin registers the first model.
    """
    monkeypatch.setattr(mf, "_GW_CACHE", _cold_cache())
    fetched = {"models": {}}  # empty until "registration" flips it
    monkeypatch.setattr(mf, "_fetch_gateway_models", lambda: dict(fetched["models"]))

    # First request on a fresh deploy: gateway reached, registry genuinely empty.
    assert mf._gateway_models() == {}
    assert mf.models_known_empty() is True
    # The empty (but successful) fetch is kept cold so it doesn't stick for a full TTL.
    assert mf._GW_CACHE["ts"] == mf._COLD

    # Admin registers a model.
    fetched["models"] = {"claude-sonnet-4-6": {"cap": True}}

    # Next request must re-fetch and see it immediately — no TTL wait.
    assert mf._gateway_models() == {"claude-sonnet-4-6": {"cap": True}}
    assert mf.models_known_empty() is False
    assert mf._GW_CACHE["ts"] != mf._COLD  # now a steady state on the normal TTL


def test_failed_fetch_stays_on_normal_ttl(monkeypatch):
    """A gateway outage (last_error set) backs off a full TTL instead of being kept cold."""
    monkeypatch.setattr(mf, "_GW_CACHE", _cold_cache())

    def boom():
        raise RuntimeError("gateway down")

    monkeypatch.setattr(mf, "_fetch_gateway_models", boom)

    assert mf._gateway_models() == {}
    assert mf._GW_CACHE["last_error"] is not None
    assert mf._GW_CACHE["ts"] != mf._COLD  # not kept cold → won't hammer the gateway
    assert mf.models_known_empty() is False  # fail open, don't claim "no model configured"


def test_empty_defaults_refetches_on_next_call(monkeypatch):
    """Regression: the auto-set 'chat' default is visible on the next request after register.

    Sibling to the gateway-models latch one layer down: with no defaults at all,
    require_default_model() must not keep raising NoDefaultModelError for a full _DEFAULTS_TTL
    after the admin registers the first model (which auto-becomes the chat default).
    """
    monkeypatch.setattr(mf, "_DEFAULTS_CACHE", _cold_cache() | {"defaults": {}})
    fetched = {"defaults": {}}
    monkeypatch.setattr(mf, "_fetch_model_defaults", lambda: dict(fetched["defaults"]))

    # Fresh deploy: no defaults configured yet.
    assert mf.get_default_model() is None
    assert mf._DEFAULTS_CACHE["ts"] == mf._COLD  # kept cold, won't latch for a full TTL

    # Registering the first model sets it as the chat default.
    fetched["defaults"] = {"chat": "claude-sonnet-4-6"}

    # Next request re-fetches and resolves — no TTL wait, no NoDefaultModelError.
    assert mf.require_default_model() == "claude-sonnet-4-6"
    assert mf._DEFAULTS_CACHE["ts"] != mf._COLD


def test_warm_refresh_does_not_block_caller():
    """A stale (non-cold) cache refreshes in the background; the caller returns immediately."""
    cache = {"ts": time.monotonic() - 1000.0, "models": {"old": {}}, "inflight": False, "last_error": None}
    cond = threading.Condition()
    release = threading.Event()

    def fetch():
        release.wait(2.0)  # would block a caller that waited on it
        return {"new": {}}

    start = time.monotonic()
    mf._refresh_if_stale(cache, "models", 60.0, cond, fetch)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5  # returned without waiting for the background fetch
    assert cache["models"] == {"old": {}}  # still serving the stale snapshot
    release.set()  # let the background thread finish so it doesn't leak
