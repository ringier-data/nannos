"""Unit tests for the PlaybookService."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from console_backend.services.playbook_service import PlaybookService, _to_jsonb


class TestPlaybookServicePrefixAndKeys:
    """Test prefix and key generation logic."""

    def setup_method(self):
        self.service = PlaybookService()

    def test_get_prefix_personal(self):
        prefix = self.service._get_prefix("personal", "user-123", None)
        assert prefix == "user-123.agent-data"

    def test_get_prefix_group(self):
        prefix = self.service._get_prefix("group", "user-123", "group-456")
        assert prefix == "group-456.agent-data"

    def test_get_prefix_group_requires_group_id(self):
        with pytest.raises(ValueError, match="group_id required"):
            self.service._get_prefix("group", "user-123", None)

    def test_agents_md_key_personal(self):
        key = self.service._agents_md_key("orchestrator", "personal", None)
        assert key == "/orchestrator/AGENTS.md"

    def test_agents_md_key_group(self):
        key = self.service._agents_md_key("orchestrator", "group", "group-456")
        assert key == "/orchestrator/AGENTS.md"

    def test_skill_key_personal(self):
        key = self.service._skill_key("orchestrator", "personal", None, "my_skill")
        assert key == "/orchestrator/skills/my_skill/SKILL.md"

    def test_skill_key_group(self):
        key = self.service._skill_key("orchestrator", "group", "g1", "my_skill")
        assert key == "/orchestrator/skills/my_skill/SKILL.md"

    def test_skills_prefix_key_personal(self):
        key = self.service._skills_prefix_key("orchestrator", "personal", None)
        assert key == "/orchestrator/skills/"

    def test_skills_prefix_key_group(self):
        key = self.service._skills_prefix_key("orchestrator", "group", "g1")
        assert key == "/orchestrator/skills/"


class TestPlaybookServiceContentExtraction:
    """Test content extraction from store values."""

    def setup_method(self):
        self.service = PlaybookService()

    def test_extract_content_from_dict(self):
        assert self.service._extract_content({"content": "hello"}) == "hello"

    def test_extract_content_from_empty_dict(self):
        assert self.service._extract_content({}) is None

    def test_extract_content_from_none(self):
        assert self.service._extract_content(None) is None

    def test_extract_content_from_string(self):
        assert self.service._extract_content("not a dict") is None

    def test_extract_title_and_description(self):
        content = "---\nname: My Skill\ndescription: This is a description.\n---\n\n## Steps\n\n1. Do something"
        title, description = self.service._extract_title_and_description(content)
        assert title == "My Skill"
        assert description == "This is a description."

    def test_extract_title_and_description_no_title(self):
        content = "No heading here.\nJust text."
        title, description = self.service._extract_title_and_description(content)
        assert title == ""
        assert description == ""

    def test_extract_title_and_description_empty(self):
        title, description = self.service._extract_title_and_description("")
        assert title == ""
        assert description == ""

    def test_extract_title_only(self):
        content = "---\nname: Title Only\n---"
        title, description = self.service._extract_title_and_description(content)
        assert title == "Title Only"
        assert description == ""

    def test_extract_from_yaml_frontmatter(self):
        content = "---\nname: incident-triage\ndescription: Handle production incidents step by step.\n---\n\n## Steps\n\n1. Do something"
        title, description = self.service._extract_title_and_description(content)
        assert title == "incident-triage"
        assert description == "Handle production incidents step by step."

    def test_extract_frontmatter_missing_description(self):
        content = "---\nname: deploy\n---\n\nBody content"
        title, description = self.service._extract_title_and_description(content)
        assert title == "deploy"
        assert description == ""


class TestToJsonb:
    """Test JSON serialization helper."""

    def test_to_jsonb(self):
        result = _to_jsonb("hello world")
        parsed = json.loads(result)
        assert parsed == {"content": "hello world"}

    def test_to_jsonb_with_special_chars(self):
        result = _to_jsonb('content with "quotes" and\nnewlines')
        parsed = json.loads(result)
        assert parsed["content"] == 'content with "quotes" and\nnewlines'


class TestPlaybookServiceAvailability:
    """Test service availability checks."""

    def test_not_available_without_factory(self):
        service = PlaybookService()
        assert service.is_available is False

    def test_available_with_factory(self):
        service = PlaybookService()
        service.set_db_session_factory(MagicMock())
        assert service.is_available is True


class TestPlaybookServiceGetAgentsMd:
    """Test AGENTS.md read operations."""

    @pytest.mark.asyncio
    async def test_returns_none_when_not_configured(self):
        service = PlaybookService()
        result = await service.get_agents_md("user1", "orchestrator", "personal")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_content_when_found(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.first.return_value = ({"content": "# My Playbook"},)
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        result = await service.get_agents_md("user1", "orchestrator", "personal")
        assert result == "# My Playbook"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        result = await service.get_agents_md("user1", "orchestrator", "personal")
        assert result is None


class TestPlaybookServicePutAgentsMd:
    """Test AGENTS.md write operations."""

    @pytest.mark.asyncio
    async def test_raises_when_not_configured(self):
        service = PlaybookService()
        with pytest.raises(RuntimeError, match="not configured"):
            await service.put_agents_md("user1", "orchestrator", "personal", "content")

    @pytest.mark.asyncio
    async def test_executes_upsert(self):
        service = PlaybookService()

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        await service.put_agents_md("user1", "orchestrator", "personal", "# New content")
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

        # Verify the SQL params include expected prefix and key
        call_args = mock_session.execute.call_args
        params = call_args[0][1]
        assert params["prefix"] == "user1.agent-data"
        assert params["key"] == "/orchestrator/AGENTS.md"


class TestPlaybookServiceDeleteAgentsMd:
    """Test AGENTS.md delete operations."""

    @pytest.mark.asyncio
    async def test_raises_when_not_configured(self):
        service = PlaybookService()
        with pytest.raises(RuntimeError, match="not configured"):
            await service.delete_agents_md("user1", "orchestrator", "personal")

    @pytest.mark.asyncio
    async def test_returns_true_when_deleted(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        result = await service.delete_agents_md("user1", "orchestrator", "personal")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        result = await service.delete_agents_md("user1", "orchestrator", "personal")
        assert result is False


class TestPlaybookServiceListSkills:
    """Test skill listing operations."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_not_configured(self):
        service = PlaybookService()
        result = await service.list_skills("user1", "orchestrator", "personal")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_parsed_skills(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.all.return_value = [
            (
                "/orchestrator/skills/incident_triage/SKILL.md",
                {"content": "---\nname: Incident Triage\ndescription: A workflow for handling incidents.\n---\n"},
            ),
            (
                "/orchestrator/skills/deploy/SKILL.md",
                {"content": "---\nname: Deploy Process\ndescription: How to deploy safely.\n---\n"},
            ),
        ]
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        skills = await service.list_skills("user1", "orchestrator", "personal")
        assert len(skills) == 2
        assert skills[0]["name"] == "incident_triage"
        assert skills[0]["title"] == "Incident Triage"
        assert skills[0]["description"] == "A workflow for handling incidents."
        assert skills[0]["scope"] == "personal"
        assert skills[1]["name"] == "deploy"

    @pytest.mark.asyncio
    async def test_skips_skills_with_no_content(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.all.return_value = [
            (
                "/orchestrator/skills/empty/SKILL.md",
                {"content": ""},
            ),
            (
                "/orchestrator/skills/none/SKILL.md",
                {},
            ),
        ]
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        skills = await service.list_skills("user1", "orchestrator", "personal")
        assert len(skills) == 0


class TestPlaybookServiceSkillCRUD:
    """Test skill get/put/delete operations."""

    @pytest.mark.asyncio
    async def test_get_skill_returns_content(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.first.return_value = ({"content": "# Skill Content"},)
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        result = await service.get_skill("user1", "orchestrator", "my_skill", "personal")
        assert result == "# Skill Content"

    @pytest.mark.asyncio
    async def test_put_skill_executes_upsert(self):
        service = PlaybookService()

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        await service.put_skill("user1", "orchestrator", "my_skill", "personal", "# Content")
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_skill_returns_true(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        result = await service.delete_skill("user1", "orchestrator", "my_skill", "personal")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_skill_returns_false_when_not_found(self):
        service = PlaybookService()

        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        result = await service.delete_skill("user1", "orchestrator", "my_skill", "personal")
        assert result is False

    @pytest.mark.asyncio
    async def test_put_skill_group_scope(self):
        service = PlaybookService()

        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        service.set_db_session_factory(mock_factory)

        await service.put_skill("user1", "orchestrator", "my_skill", "group", "# Content", group_id="g1")

        call_args = mock_session.execute.call_args
        params = call_args[0][1]
        assert params["prefix"] == "g1.agent-data"
        assert params["key"] == "/orchestrator/skills/my_skill/SKILL.md"
