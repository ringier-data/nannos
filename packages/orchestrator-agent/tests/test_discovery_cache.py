"""Unit tests for the per-user discovery + registry caches."""

import base64
import json
import time

from app.core import discovery_cache as dc
from app.core.discovery_cache import (
    TtlTokenCache,
    cache_key,
    resolve_entitlement_version,
    token_exp,
)


def _jwt_with_exp(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


class TestCacheKey:
    def test_stable_across_group_order(self):
        assert cache_key("u", ["b", "a"], "cfg") == cache_key("u", ["a", "b"], "cfg")

    def test_changes_when_group_added(self):
        assert cache_key("u", ["a", "b"], "cfg") != cache_key("u", ["a", "b", "c"], "cfg")

    def test_changes_with_policy_version(self):
        assert cache_key("u", ["a"], "cfg", "0") != cache_key("u", ["a"], "cfg", "1")

    def test_changes_with_user(self):
        assert cache_key("u1", [], None) != cache_key("u2", [], None)

    def test_changes_with_config_hash(self):
        assert cache_key("u", ["a"], "cfg1") != cache_key("u", ["a"], "cfg2")

    def test_no_tool_names_param(self):
        # tool_names is intentionally not part of the key (discovery is unfiltered); equal
        # inputs always collide regardless of any per-user tool whitelist.
        assert cache_key("u", ["a"], "cfg", "0") == cache_key("u", ["a"], "cfg", "0")

    def test_changes_with_entitlement_version(self):
        # The load-bearing input: an activated sub-agent, whitelist, role or group-default
        # change moves the stamp and must make the previous entry unreachable.
        assert cache_key("u", ["a"], None, "0", entitlement_version="v1") != cache_key(
            "u", ["a"], None, "0", entitlement_version="v2"
        )

    def test_missing_entitlement_version_equals_empty(self):
        # None and "" are the same "unknown" stamp (pre-stamp behaviour, TTL-bounded).
        assert cache_key("u", [], None, "0", entitlement_version=None) == cache_key(
            "u", [], None, "0", entitlement_version=""
        )
        assert cache_key("u", [], None, "0") != cache_key("u", [], None, "0", entitlement_version="v1")


class TestResolveEntitlementVersion:
    def setup_method(self):
        dc._last_entitlement_version = None

    def test_fetched_wins_and_is_remembered(self):
        assert resolve_entitlement_version("alice", "v1") == "v1"
        assert dc._last_stamps().get("alice") == "v1"

    def test_fetch_failure_falls_back_to_last_known(self):
        resolve_entitlement_version("alice", "v1")
        assert resolve_entitlement_version("alice", None) == "v1"

    def test_fetch_failure_without_history_is_none(self):
        assert resolve_entitlement_version("nobody", None) is None

    def test_fallback_is_per_user(self):
        resolve_entitlement_version("alice", "v1")
        assert resolve_entitlement_version("bob", None) is None

    def test_remembered_stamps_are_bounded(self):
        # Same bounding policy as every other store in the module: size-capped, TTL-aged.
        assert dc._last_stamps()._max_entries == dc._DEFAULT_MAX_ENTRIES
        assert dc._last_stamps()._ttl == dc._LAST_STAMP_TTL_S


class TestTokenExp:
    def test_parses_exp(self):
        exp = int(time.time()) + 1234
        assert abs((token_exp(_jwt_with_exp(exp)) or 0) - exp) < 1

    def test_none_on_garbage(self):
        assert token_exp(None) is None
        assert token_exp("not-a-jwt") is None
        assert token_exp("a.b.c") is None  # b not valid base64 json


class TestTtlTokenCache:
    def test_get_put_hit(self):
        c = TtlTokenCache(ttl_seconds=300)
        c.put("k", ("tools", "subs"), _jwt_with_exp(int(time.time()) + 3600))
        assert c.get("k") == ("tools", "subs")

    def test_miss_returns_none(self):
        assert TtlTokenCache(300).get("absent") is None

    def test_ttl_expiry(self):
        c = TtlTokenCache(ttl_seconds=0.05)
        c.put("k", "v", _jwt_with_exp(int(time.time()) + 3600))
        assert c.get("k") == "v"
        time.sleep(0.1)
        assert c.get("k") is None

    def test_entry_bounded_by_token_exp(self):
        # token expires in 40s; with 30s margin the entry must live <= ~10s, not the 300s TTL
        c = TtlTokenCache(ttl_seconds=300)
        c.put("k", "v", _jwt_with_exp(int(time.time()) + 40))
        entry = c._store["k"]
        assert entry.expires_at <= time.time() + 11  # ~ (40 - 30) margin, well under 300

    def test_skips_caching_when_token_already_expiring(self):
        c = TtlTokenCache(ttl_seconds=300)
        c.put("k", "v", _jwt_with_exp(int(time.time()) + 10))  # within the 30s margin
        assert c.get("k") is None

    def test_no_token_uses_full_ttl(self):
        c = TtlTokenCache(ttl_seconds=300)
        c.put("k", "v", None)
        assert c.get("k") == "v"
        assert c._store["k"].expires_at > time.time() + 290

    def test_clear(self):
        c = TtlTokenCache(300)
        c.put("k", "v", None)
        c.clear()
        assert c.get("k") is None

    def test_max_entries_bounds_size(self):
        c = TtlTokenCache(ttl_seconds=300, max_entries=3)
        for i in range(10):
            c.put(f"k{i}", i, None)
        assert len(c._store) <= 3
