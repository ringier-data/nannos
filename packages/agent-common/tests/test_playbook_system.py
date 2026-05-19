"""Tests for PlaybookReaderService and playbook tools."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_common.core.playbook_reader import PlaybookReaderService


class TestPlaybookReaderService:
    """Tests for PlaybookReaderService."""

    @pytest.fixture
    def mock_store(self):
        store = AsyncMock()
        store.aget = AsyncMock(return_value=None)
        store.asearch = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def reader(self, mock_store):
        return PlaybookReaderService(mock_store)

    @pytest.mark.asyncio
    async def test_read_agents_md_returns_none_when_not_found(self, reader, mock_store):
        mock_store.aget.return_value = None
        group_content, personal_content = await reader.read_agents_md(user_id="user1", agent_name="test-agent")
        assert group_content is None
        assert personal_content is None

    @pytest.mark.asyncio
    async def test_read_agents_md_returns_personal_content(self, reader, mock_store):
        # Mock store to return content for personal namespace
        mock_item = MagicMock()
        mock_item.value = {"content": "# My Playbook\n\n## Preferences\nBe concise."}

        async def mock_aget(namespace, key):
            if namespace == ("user1", "agent-data"):
                return mock_item
            return None

        mock_store.aget = mock_aget

        group_content, personal_content = await reader.read_agents_md(user_id="user1", agent_name="test-agent")
        assert personal_content == "# My Playbook\n\n## Preferences\nBe concise."
        assert group_content is None

    @pytest.mark.asyncio
    async def test_read_agents_md_returns_both_scopes(self, reader, mock_store):
        mock_personal = MagicMock()
        mock_personal.value = {"content": "personal content"}
        mock_group = MagicMock()
        mock_group.value = {"content": "group content"}

        async def mock_aget(namespace, key):
            if namespace == ("user1", "agent-data"):
                return mock_personal
            if namespace == ("group1", "agent-data"):
                return mock_group
            return None

        mock_store.aget = mock_aget

        group_content, personal_content = await reader.read_agents_md(
            user_id="user1", agent_name="test-agent", group_ids=["group1"]
        )
        assert personal_content == "personal content"
        assert group_content == "group content"

    @pytest.mark.asyncio
    async def test_list_skills_returns_empty_when_no_skills(self, reader, mock_store):
        skills = await reader.list_skills(user_id="user1", agent_name="test-agent")
        assert skills == []

    @pytest.mark.asyncio
    async def test_list_skills_extracts_descriptions(self, reader, mock_store):
        # Mock search to return skill file paths
        mock_result = MagicMock()
        mock_result.key = "/test-agent/skills/incident_triage.md"
        mock_store.asearch.return_value = [mock_result]

        # Mock aget to return skill content
        mock_item = MagicMock()
        mock_item.value = {"content": "# Incident Triage\n\nHandle production incidents step by step.\n\n## Steps\n..."}

        async def mock_aget(namespace, key):
            if "incident_triage" in key:
                return mock_item
            return None

        mock_store.aget = mock_aget

        skills = await reader.list_skills(user_id="user1", agent_name="test-agent")
        assert len(skills) == 1
        assert skills[0].name == "incident_triage"
        assert skills[0].scope == "personal"
        assert "Handle production incidents" in skills[0].description

    @pytest.mark.asyncio
    async def test_read_skill_auto_scope_personal_first(self, reader, mock_store):
        mock_item = MagicMock()
        mock_item.value = {"content": "personal skill content"}

        async def mock_aget(namespace, key):
            if namespace == ("user1", "agent-data") and "triage" in key:
                return mock_item
            return None

        mock_store.aget = mock_aget

        content = await reader.read_skill(
            user_id="user1", agent_name="test-agent", skill_name="triage", group_ids=["group1"]
        )
        assert content == "personal skill content"

    @pytest.mark.asyncio
    async def test_read_skill_auto_scope_fallback_to_group(self, reader, mock_store):
        mock_group_item = MagicMock()
        mock_group_item.value = {"content": "group skill content"}

        async def mock_aget(namespace, key):
            if namespace == ("group1", "agent-data") and "triage" in key:
                return mock_group_item
            return None

        mock_store.aget = mock_aget

        content = await reader.read_skill(
            user_id="user1", agent_name="test-agent", skill_name="triage", group_ids=["group1"]
        )
        assert content == "group skill content"

    @pytest.mark.asyncio
    async def test_read_skill_not_found(self, reader, mock_store):
        mock_store.aget = AsyncMock(return_value=None)
        content = await reader.read_skill(user_id="user1", agent_name="test-agent", skill_name="nonexistent")
        assert content is None

    @pytest.mark.asyncio
    async def test_caching_prevents_store_re_reads(self, reader, mock_store):
        mock_item = MagicMock()
        mock_item.value = {"content": "cached content"}

        call_count = 0

        async def mock_aget(namespace, key):
            nonlocal call_count
            call_count += 1
            return mock_item

        mock_store.aget = mock_aget

        # First call
        await reader.read_agents_md(user_id="user1", agent_name="test")
        first_count = call_count

        # Second call should use cache
        await reader.read_agents_md(user_id="user1", agent_name="test")
        assert call_count == first_count  # No additional store calls
