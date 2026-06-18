"""Unit tests for the per-user discovery + registry caches."""

import base64
import json
import time

from app.core import discovery_cache as dc
from app.core.discovery_cache import (
    TtlTokenCache,
    cache_key,
    get_discovery_cache,
    get_user_cache,
    invalidate_all,
    invalidate_users,
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

    def test_invalidate_owner_drops_only_matching(self):
        c = TtlTokenCache(300)
        c.put("k1", "v1", None, owner="alice")
        c.put("k2", "v2", None, owner="bob")
        c.put("k3", "v3", None, owner="alice")
        removed = c.invalidate_owner("alice")
        assert removed == 2
        assert c.get("k1") is None and c.get("k3") is None
        assert c.get("k2") == "v2"

    def test_invalidate_owner_unknown_is_noop(self):
        c = TtlTokenCache(300)
        c.put("k", "v", None, owner="alice")
        assert c.invalidate_owner("nobody") == 0
        assert c.get("k") == "v"

    def test_max_entries_bounds_size(self):
        c = TtlTokenCache(ttl_seconds=300, max_entries=3)
        for i in range(10):
            c.put(f"k{i}", i, None)
        assert len(c._store) <= 3


class TestScopedInvalidation:
    def test_invalidate_users_targets_only_given_subs(self):
        get_discovery_cache(300).put("d-alice", ("t", "s"), None, owner="alice")
        get_discovery_cache().put("d-bob", ("t", "s"), None, owner="bob")
        get_user_cache(300).put("u-alice", object(), None, owner="alice")
        get_user_cache().put("u-bob", object(), None, owner="bob")

        removed = invalidate_users(["alice"])
        assert removed == 2  # one discovery + one user entry for alice
        assert get_discovery_cache().get("d-alice") is None
        assert get_user_cache().get("u-alice") is None
        assert get_discovery_cache().get("d-bob") is not None
        assert get_user_cache().get("u-bob") is not None

    def teardown_method(self):
        dc._discovery_cache = None
        dc._user_cache = None


class TestInvalidateAll:
    def test_clears_both_singletons(self):
        get_discovery_cache(300).put("d", ("t", "s"), None)
        get_user_cache(300).put("u", object(), None)
        assert get_discovery_cache().get("d") is not None
        assert get_user_cache().get("u") is not None
        invalidate_all()
        assert get_discovery_cache().get("d") is None
        assert get_user_cache().get("u") is None

    def teardown_method(self):
        # reset module singletons so tests don't bleed
        dc._discovery_cache = None
        dc._user_cache = None
