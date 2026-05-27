"""Integration tests for PUT /me/settings/tool-bypass endpoint."""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest


@pytest.mark.asyncio
class TestToolBypassEndpoint:
    """Test PUT /api/v1/auth/me/settings/tool-bypass."""

    # --- bypass_all semantics ---

    async def test_set_bypass_all_explicit(self, client_with_db):
        """Setting bypass_all=True creates a bypass_all rule."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "console_create_skill", "server_slug": "mcp-gateway", "bypass_all": True},
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        assert "console_create_skill::mcp-gateway" in rules
        assert rules["console_create_skill::mcp-gateway"] == {"bypass_all": True}

    async def test_set_bypass_all_implicit_when_no_patterns(self, client_with_db):
        """When neither bypass_all nor bypass_patterns is provided, defaults to bypass_all."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "console_update_skill"},
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        assert "console_update_skill::_self" in rules
        assert rules["console_update_skill::_self"] == {"bypass_all": True}

    async def test_default_server_slug_is_self(self, client_with_db):
        """server_slug defaults to '_self' when not specified."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "my_tool", "bypass_all": True},
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        assert "my_tool::_self" in rules

    # --- bypass_patterns semantics ---

    async def test_set_bypass_patterns(self, client_with_db):
        """Setting bypass_patterns creates a pattern-based rule."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "file_write",
                "server_slug": "sandbox",
                "bypass_patterns": {"path": ["/tmp/*", "/workspace/*"]},
            },
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        key = "file_write::sandbox"
        assert key in rules
        assert rules[key] == {"bypass_patterns": {"path": ["/tmp/*", "/workspace/*"]}}

    async def test_merge_bypass_patterns_appends(self, client_with_db):
        """Subsequent pattern calls merge into existing patterns."""
        # First: set initial patterns
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "file_write",
                "bypass_patterns": {"path": ["/tmp/*"]},
            },
        )

        # Second: merge additional patterns for same param
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "file_write",
                "bypass_patterns": {"path": ["/workspace/*"]},
            },
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        patterns = rules["file_write::_self"]["bypass_patterns"]["path"]
        assert "/tmp/*" in patterns
        assert "/workspace/*" in patterns

    async def test_merge_bypass_patterns_deduplicates(self, client_with_db):
        """Merging duplicate patterns does not create duplicates."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "file_write",
                "bypass_patterns": {"path": ["/tmp/*", "/workspace/*"]},
            },
        )

        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "file_write",
                "bypass_patterns": {"path": ["/tmp/*", "/new/*"]},
            },
        )

        assert response.status_code == 200
        patterns = response.json()["tool_bypass_rules"]["file_write::_self"]["bypass_patterns"]["path"]
        assert patterns.count("/tmp/*") == 1
        assert "/workspace/*" in patterns
        assert "/new/*" in patterns

    async def test_merge_bypass_patterns_multiple_params(self, client_with_db):
        """Patterns for different parameters are merged independently."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "api_call",
                "bypass_patterns": {"url": ["https://example.com/*"]},
            },
        )

        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "api_call",
                "bypass_patterns": {"method": ["GET", "POST"]},
            },
        )

        assert response.status_code == 200
        bp = response.json()["tool_bypass_rules"]["api_call::_self"]["bypass_patterns"]
        assert "https://example.com/*" in bp["url"]
        assert "GET" in bp["method"]
        assert "POST" in bp["method"]

    async def test_patterns_are_sorted(self, client_with_db):
        """Merged patterns are stored in sorted order."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "tool_x",
                "bypass_patterns": {"arg": ["zebra", "alpha", "middle"]},
            },
        )

        assert response.status_code == 200
        patterns = response.json()["tool_bypass_rules"]["tool_x::_self"]["bypass_patterns"]["arg"]
        assert patterns == sorted(patterns)

    # --- remove semantics ---

    async def test_remove_existing_rule(self, client_with_db):
        """remove=True deletes an existing rule."""
        # Create rule
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "to_remove", "bypass_all": True},
        )

        # Remove it
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "to_remove", "remove": True},
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        assert "to_remove::_self" not in rules

    async def test_remove_nonexistent_rule_is_noop(self, client_with_db):
        """remove=True on a non-existent rule does not error."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "never_existed", "remove": True},
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        assert "never_existed::_self" not in rules

    async def test_remove_with_custom_server_slug(self, client_with_db):
        """remove respects server_slug in the key."""
        # Create rules on two slugs
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "multi", "server_slug": "slug-a", "bypass_all": True},
        )
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "multi", "server_slug": "slug-b", "bypass_all": True},
        )

        # Remove only slug-a
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "multi", "server_slug": "slug-a", "remove": True},
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        assert "multi::slug-a" not in rules
        assert "multi::slug-b" in rules

    # --- multiple tools coexistence ---

    async def test_multiple_tools_independent(self, client_with_db):
        """Rules for different tools are independent."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "tool_a", "bypass_all": True},
        )
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "tool_b", "bypass_patterns": {"x": ["1"]}},
        )

        assert response.status_code == 200
        rules = response.json()["tool_bypass_rules"]
        assert rules["tool_a::_self"] == {"bypass_all": True}
        assert rules["tool_b::_self"] == {"bypass_patterns": {"x": ["1"]}}

    # --- persistence across requests ---

    async def test_rules_persist_across_requests(self, client_with_db):
        """Rules set via PUT are visible on subsequent GET /me/settings."""
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "persisted_tool", "bypass_all": True},
        )

        response = await client_with_db.get("/api/v1/auth/me/settings")
        assert response.status_code == 200
        rules = response.json()["data"]["tool_bypass_rules"]
        assert "persisted_tool::_self" in rules
        assert rules["persisted_tool::_self"] == {"bypass_all": True}

    # --- auth mode: require_auth_or_bearer_token ---

    async def test_endpoint_accepts_bearer_token_auth(self, client_with_db):
        """The endpoint uses require_auth_or_bearer_token, allowing bearer auth.

        Since our test fixture overrides require_auth_or_bearer_token,
        the fact that PUT succeeds confirms the dependency is wired correctly.
        """
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "bearer_test", "bypass_all": True},
        )
        assert response.status_code == 200

    # --- validation ---

    async def test_missing_tool_name_returns_422(self, client_with_db):
        """tool_name is required."""
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"bypass_all": True},
        )
        assert response.status_code == 422

    # --- bypass_all overrides existing patterns ---

    async def test_bypass_all_replaces_existing_patterns(self, client_with_db):
        """Setting bypass_all after patterns replaces the rule entirely."""
        # Start with patterns
        await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={
                "tool_name": "overwrite_test",
                "bypass_patterns": {"param": ["val"]},
            },
        )

        # Override with bypass_all
        response = await client_with_db.put(
            "/api/v1/auth/me/settings/tool-bypass",
            json={"tool_name": "overwrite_test", "bypass_all": True},
        )

        assert response.status_code == 200
        rule = response.json()["tool_bypass_rules"]["overwrite_test::_self"]
        assert rule == {"bypass_all": True}
        assert "bypass_patterns" not in rule
