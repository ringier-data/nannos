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
            SkillFile(path="a/b/c/d.py", content="too deep")

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
