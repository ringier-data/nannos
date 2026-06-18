"""Unit tests for the per-user discovery + registry caches."""

import base64
import json
import time

from app.core import discovery_cache as dc
from app.core.discovery_cache import (
    TtlTokenCache,
    entitlement_key,
    get_discovery_cache,
    get_user_cache,
    invalidate_all,
    token_exp,
    user_key,
)


def _jwt_with_exp(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


class TestEntitlementKey:
    def test_stable_across_group_and_tool_order(self):
        a = entitlement_key("u", ["b", "a"], "cfg", ["t2", "t1"])
        b = entitlement_key("u", ["a", "b"], "cfg", ["t1", "t2"])
        assert a == b

    def test_changes_when_group_added(self):
        a = entitlement_key("u", ["a", "b"], "cfg", ["t1"])
        b = entitlement_key("u", ["a", "b", "c"], "cfg", ["t1"])
        assert a != b

    def test_changes_when_tool_names_change(self):
        a = entitlement_key("u", ["a"], "cfg", ["t1"])
        b = entitlement_key("u", ["a"], "cfg", ["t1", "t2"])
        assert a != b

    def test_changes_with_policy_version(self):
        a = entitlement_key("u", ["a"], "cfg", ["t1"], policy_version="0")
        b = entitlement_key("u", ["a"], "cfg", ["t1"], policy_version="1")
        assert a != b

    def test_changes_with_user(self):
        assert entitlement_key("u1", [], None, []) != entitlement_key("u2", [], None, [])


class TestUserKey:
    def test_excludes_tool_names_but_keys_groups_and_policy(self):
        a = user_key("u", ["a"], "cfg", "0")
        b = user_key("u", ["a"], "cfg", "0")
        assert a == b
        assert user_key("u", ["a"], "cfg", "0") != user_key("u", ["a", "b"], "cfg", "0")
        assert user_key("u", ["a"], "cfg", "0") != user_key("u", ["a"], "cfg", "1")


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
