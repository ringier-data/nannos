"""Tests for sub-agent skills validation (path traversal, sandbox constraints).

Covers:
- SkillFile.path traversal rejection
- SkillDefinition.name validation
- sandbox_enabled only allowed for local agents (SubAgentCreate model validator)
- Skills persisted in config_version
"""

import pytest
from pydantic import ValidationError

from console_backend.models.sub_agent import (
    SkillDefinition,
    SkillFile,
    SubAgentCreate,
    SubAgentType,
)


class TestSkillFilePathValidation:
    """SkillFile.path must be relative, no traversal, max 3 segments."""

    def test_valid_simple_path(self):
        f = SkillFile(path="scripts/check.py", content="print('ok')")
        assert f.path == "scripts/check.py"

    def test_valid_nested_path(self):
        f = SkillFile(path="a/b/c.md", content="hello")
        assert f.path == "a/b/c.md"

    def test_valid_single_segment(self):
        f = SkillFile(path="README.md", content="# readme")
        assert f.path == "README.md"

    def test_rejects_absolute_path(self):
        with pytest.raises(ValidationError, match="must be relative"):
            SkillFile(path="/etc/passwd", content="bad")

    def test_rejects_tilde_path(self):
        with pytest.raises(ValidationError, match="must be relative"):
            SkillFile(path="~/.ssh/id_rsa", content="bad")

    def test_rejects_traversal(self):
        with pytest.raises(ValidationError, match="traversal"):
            SkillFile(path="../../../etc/passwd", content="bad")

    def test_rejects_traversal_mid_path(self):
        with pytest.raises(ValidationError, match="traversal"):
            SkillFile(path="scripts/../../../etc/passwd", content="bad")

    def test_rejects_too_deep(self):
        with pytest.raises(ValidationError, match="max depth"):
            SkillFile(path="a/b/c/d/e/f/g.py", content="too deep")

    def test_accepts_deeper_paths(self):
        """Paths up to 6 segments are valid (e.g. scripts/office/schemas/vendor/file.xsd)."""
        f = SkillFile(path="scripts/office/helpers/__init__.py", content="ok")
        assert f.path == "scripts/office/helpers/__init__.py"

    def test_rejects_empty_path(self):
        with pytest.raises(ValidationError, match="Invalid"):
            SkillFile(path="", content="empty")

    def test_rejects_trailing_slash(self):
        with pytest.raises(ValidationError, match="Invalid"):
            SkillFile(path="scripts/", content="bad")


class TestSkillDefinitionNameValidation:
    """SkillDefinition.name must be lowercase alphanumeric + hyphens."""

    def test_valid_name(self):
        s = SkillDefinition(name="incident-triage", description="Triage", body="# Steps")
        assert s.name == "incident-triage"

    def test_rejects_uppercase(self):
        with pytest.raises(ValidationError):
            SkillDefinition(name="Invalid-Name", description="x", body="x")

    def test_rejects_special_chars(self):
        with pytest.raises(ValidationError):
            SkillDefinition(name="skill_with_underscores", description="x", body="x")

    def test_rejects_empty(self):
        with pytest.raises(ValidationError):
            SkillDefinition(name="", description="x", body="x")

    def test_rejects_too_long(self):
        with pytest.raises(ValidationError):
            SkillDefinition(name="a" * 65, description="x", body="x")


class TestSandboxEnabledLocalOnly:
    """sandbox_enabled=True must be rejected for non-local agent types."""

    def test_sandbox_allowed_for_local(self):
        data = SubAgentCreate(
            name="test-agent",
            description="Test",
            type=SubAgentType.LOCAL,
            system_prompt="You are a test agent.",
            sandbox_enabled=True,
        )
        assert data.sandbox_enabled is True

    def test_sandbox_rejected_for_remote(self):
        with pytest.raises(ValidationError, match="only supported for local"):
            SubAgentCreate(
                name="test-agent",
                description="Test",
                type=SubAgentType.REMOTE,
                agent_url="https://example.com/agent",
                sandbox_enabled=True,
            )

    def test_sandbox_rejected_for_foundry(self):
        with pytest.raises(ValidationError, match="only supported for local"):
            SubAgentCreate(
                name="test-agent",
                description="Test",
                type=SubAgentType.FOUNDRY,
                sandbox_enabled=True,
            )

    def test_sandbox_false_for_remote_ok(self):
        """sandbox_enabled=False is fine for any type."""
        data = SubAgentCreate(
            name="test-agent",
            description="Test",
            type=SubAgentType.REMOTE,
            agent_url="https://example.com/agent",
            sandbox_enabled=False,
        )
        assert data.sandbox_enabled is False


class TestSkillsPersistence:
    """Skills are correctly serialized in SubAgentCreate."""

    def test_skills_with_files(self):
        data = SubAgentCreate(
            name="skilled-agent",
            description="An agent with skills",
            type=SubAgentType.LOCAL,
            system_prompt="You are helpful.",
            skills=[
                SkillDefinition(
                    name="data-analysis",
                    description="Analyse data from CSV files",
                    body="# Steps\n1. Read file\n2. Analyse",
                    files=[
                        SkillFile(path="scripts/parse.py", content="import csv"),
                        SkillFile(path="templates/report.md", content="# Report"),
                    ],
                ),
            ],
        )
        assert len(data.skills) == 1
        assert data.skills[0].name == "data-analysis"
        assert len(data.skills[0].files) == 2

    def test_empty_skills_default(self):
        data = SubAgentCreate(
            name="basic-agent",
            description="No skills",
            type=SubAgentType.LOCAL,
            system_prompt="Hello",
        )
        assert data.skills == []


class TestSkillDefinitionReferenceMode:
    """Imported skills stored as references (no body/files)."""

    def test_imported_skill_with_source(self):
        """Imported skill can have empty body when source is set."""
        skill = SkillDefinition(
            name="imported-skill",
            description="From registry",
            body="",
            source="vercel-labs/agent-skills/next-js-dev",
            source_hash="abc123",
        )
        assert skill.source == "vercel-labs/agent-skills/next-js-dev"
        assert skill.body == ""
        assert skill.files == []

    def test_custom_skill_no_source(self):
        """Custom skills have full body and no source."""
        skill = SkillDefinition(
            name="custom-skill",
            description="Handwritten",
            body="# Do something\nStep 1...",
        )
        assert skill.source is None
        assert skill.source_hash is None
        assert skill.body == "# Do something\nStep 1..."

    def test_body_defaults_to_empty(self):
        """Body defaults to empty string (for imported skills)."""
        skill = SkillDefinition(
            name="ref-skill",
            description="Reference only",
            source="some/source/id",
        )
        assert skill.body == ""


class TestStripImportedSkillContent:
    """_strip_imported_skill_content removes body/files from imported skills."""

    def setup_method(self):
        from console_backend.services.sub_agent_service import SubAgentService

        self.strip = SubAgentService._strip_imported_skill_content

    def test_strips_imported_skill_content(self):
        skills = [
            SkillDefinition(
                name="imported",
                description="From registry",
                body="# Full content here\nLong markdown...",
                files=[SkillFile(path="script.py", content="print('hello')")],
                source="org/repo/skill",
                source_hash="hash123",
            ),
        ]
        result = self.strip(skills)
        assert len(result) == 1
        assert result[0]["name"] == "imported"
        assert result[0]["description"] == "From registry"
        assert result[0]["source"] == "org/repo/skill"
        assert result[0]["source_hash"] == "hash123"
        assert result[0]["body"] == ""
        assert result[0]["files"] == []

    def test_preserves_custom_skill_content(self):
        skills = [
            SkillDefinition(
                name="custom",
                description="Handwritten",
                body="# Custom content",
                files=[SkillFile(path="data.json", content='{"key": "val"}')],
            ),
        ]
        result = self.strip(skills)
        assert len(result) == 1
        assert result[0]["body"] == "# Custom content"
        assert len(result[0]["files"]) == 1
        assert result[0]["files"][0]["path"] == "data.json"

    def test_mixed_skills(self):
        skills = [
            SkillDefinition(
                name="imported-one",
                description="Registry",
                body="Big content...",
                source="org/repo/one",
                source_hash="h1",
            ),
            SkillDefinition(
                name="custom-one",
                description="Custom",
                body="Custom body",
            ),
            SkillDefinition(
                name="imported-two",
                description="Another imported",
                body="More content...",
                files=[SkillFile(path="large.xsd", content="<xml>...</xml>")],
                source="org/repo/two",
                source_hash="h2",
            ),
        ]
        result = self.strip(skills)
        assert result[0]["body"] == ""
        assert result[0]["files"] == []
        assert result[1]["body"] == "Custom body"
        assert result[2]["body"] == ""
        assert result[2]["files"] == []

    def test_handles_dict_input(self):
        """Also works when skills are already dicts (from DB)."""
        skills = [
            {
                "name": "from-db",
                "description": "Imported",
                "body": "content",
                "files": [],
                "source": "x/y/z",
                "source_hash": "h",
            },
        ]
        result = self.strip(skills)
        assert result[0]["body"] == ""
        assert result[0]["source"] == "x/y/z"


class TestPersistAndStripSkills:
    """_persist_and_strip_skills upserts custom skills to registry and strips all."""

    @pytest.fixture
    def mock_db(self):
        from unittest.mock import AsyncMock, MagicMock

        db = AsyncMock()
        # Mock execute for the SELECT query in upsert_agent_skill
        result_mock = MagicMock()
        result_mock.scalars.return_value.first.return_value = None  # No existing entry
        db.execute.return_value = result_mock
        return db

    @pytest.fixture
    def mock_actor(self):
        from console_backend.models.user import User

        return User(
            id="test-user-id",
            sub="test-user-id",
            email="test@example.com",
            first_name="Test",
            last_name="User",
            role="admin",
        )

    @pytest.mark.asyncio
    async def test_custom_skill_gets_registry_source(self, mock_db, mock_actor):
        """Custom skills (no source) should be upserted to registry and get a source ID."""
        from unittest.mock import AsyncMock, patch

        from console_backend.services.sub_agent_service import SubAgentService

        service = SubAgentService.__new__(SubAgentService)

        with patch(
            "console_backend.services.skill_registry_service.SkillRegistryService.upsert_agent_skill",
            new_callable=AsyncMock,
            return_value=("generated-uuid-123", "content-hash-abc"),
        ) as mock_upsert:
            skills = [
                SkillDefinition(
                    name="my-custom-skill",
                    description="A custom skill",
                    body="# Do something",
                    files=[SkillFile(path="helper.py", content="print('hi')")],
                ),
            ]

            result = await service._persist_and_strip_skills(mock_db, mock_actor, 42, skills)

            assert len(result) == 1
            assert result[0]["name"] == "my-custom-skill"
            assert result[0]["source"] == "generated-uuid-123"
            assert result[0]["source_hash"] == "content-hash-abc"
            assert result[0]["body"] == ""
            assert result[0]["files"] == []

            # Verify upsert was called with correct args
            mock_upsert.assert_called_once()
            call_kwargs = mock_upsert.call_args[1]
            assert call_kwargs["sub_agent_id"] == 42
            assert call_kwargs["name"] == "my-custom-skill"
            # Should have SKILL.md + helper.py
            assert len(call_kwargs["files"]) == 2
            file_paths = [f.path for f in call_kwargs["files"]]
            assert "SKILL.md" in file_paths
            assert "helper.py" in file_paths

    @pytest.mark.asyncio
    async def test_imported_skill_stripped_without_registry_call(self, mock_db, mock_actor):
        """Imported skills (with source) should be stripped without calling the registry."""
        from unittest.mock import AsyncMock, patch

        from console_backend.services.sub_agent_service import SubAgentService

        service = SubAgentService.__new__(SubAgentService)

        with patch(
            "console_backend.services.skill_registry_service.SkillRegistryService.upsert_agent_skill",
            new_callable=AsyncMock,
        ) as mock_upsert:
            skills = [
                SkillDefinition(
                    name="imported-skill",
                    description="From external registry",
                    body="# Full body (will be stripped)",
                    source="existing-registry-id",
                    source_hash="existing-hash",
                ),
            ]

            result = await service._persist_and_strip_skills(mock_db, mock_actor, 42, skills)

            assert len(result) == 1
            assert result[0]["source"] == "existing-registry-id"
            assert result[0]["source_hash"] == "existing-hash"
            assert result[0]["body"] == ""
            assert result[0]["files"] == []

            # Should NOT call upsert for imported skills
            mock_upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_skills_both_handled(self, mock_db, mock_actor):
        """Mix of custom and imported skills are handled correctly."""
        from unittest.mock import AsyncMock, patch

        from console_backend.services.sub_agent_service import SubAgentService

        service = SubAgentService.__new__(SubAgentService)

        with patch(
            "console_backend.services.skill_registry_service.SkillRegistryService.upsert_agent_skill",
            new_callable=AsyncMock,
            return_value=("new-id", "new-hash"),
        ) as mock_upsert:
            skills = [
                SkillDefinition(
                    name="imported",
                    description="External",
                    body="body",
                    source="ext-id",
                    source_hash="ext-hash",
                ),
                SkillDefinition(
                    name="custom",
                    description="Inline",
                    body="# Custom body",
                ),
            ]

            result = await service._persist_and_strip_skills(mock_db, mock_actor, 7, skills)

            assert len(result) == 2
            # Imported: keep original source
            assert result[0]["source"] == "ext-id"
            assert result[0]["body"] == ""
            # Custom: gets new registry source
            assert result[1]["source"] == "new-id"
            assert result[1]["source_hash"] == "new-hash"
            assert result[1]["body"] == ""

            # Only one upsert call (for the custom skill)
            mock_upsert.assert_called_once()
