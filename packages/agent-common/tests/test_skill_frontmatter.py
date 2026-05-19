"""Tests for skill_frontmatter utilities."""

from agent_common.core.skill_frontmatter import (
    build_skill_content,
    parse_skill_frontmatter,
    validate_skill_name,
)


class TestValidateSkillName:
    """Test name validation against the SKILL.md spec."""

    def test_valid_simple(self):
        assert validate_skill_name("my-skill") is None

    def test_valid_alphanumeric(self):
        assert validate_skill_name("a123") is None

    def test_valid_single_char(self):
        assert validate_skill_name("a") is None

    def test_valid_max_length(self):
        assert validate_skill_name("a" * 64) is None

    def test_empty(self):
        assert validate_skill_name("") is not None

    def test_too_long(self):
        assert validate_skill_name("a" * 65) is not None

    def test_uppercase_rejected(self):
        assert validate_skill_name("MySkill") is not None

    def test_underscores_rejected(self):
        assert validate_skill_name("my_skill") is not None

    def test_consecutive_hyphens(self):
        assert validate_skill_name("my--skill") is not None

    def test_leading_hyphen(self):
        assert validate_skill_name("-leading") is not None

    def test_trailing_hyphen(self):
        assert validate_skill_name("trailing-") is not None

    def test_spaces_rejected(self):
        assert validate_skill_name("has space") is not None


class TestParseSkillFrontmatter:
    """Test frontmatter parsing for both new and legacy formats."""

    def test_parse_frontmatter_format(self):
        content = "---\nname: my-skill\ndescription: Does something useful.\n---\n\n## Steps\n\n1. Do it"
        result = parse_skill_frontmatter(content)
        assert result is not None
        assert result.frontmatter.name == "my-skill"
        assert result.frontmatter.description == "Does something useful."
        assert "## Steps" in result.body

    def test_parse_frontmatter_with_metadata(self):
        content = "---\nname: deploy\ndescription: Deploy stuff.\nmetadata:\n  author: test-user\n  version: '1.0'\n---\n\nBody here."
        result = parse_skill_frontmatter(content)
        assert result is not None
        assert result.frontmatter.name == "deploy"
        assert result.frontmatter.metadata["author"] == "test-user"

    def test_parse_legacy_format(self):
        content = "# Incident Triage\n\nHandle production incidents step by step.\n\n## Steps\n\n1. Check alerts"
        result = parse_skill_frontmatter(content)
        assert result is not None
        assert result.frontmatter.name == "incident-triage"
        assert "Handle production incidents" in result.frontmatter.description

    def test_empty_content(self):
        assert parse_skill_frontmatter("") is None
        assert parse_skill_frontmatter("   ") is None

    def test_none_content(self):
        assert parse_skill_frontmatter(None) is None

    def test_frontmatter_missing_name(self):
        content = "---\ndescription: No name field.\n---\n\nBody"
        result = parse_skill_frontmatter(content)
        assert result is not None
        assert result.frontmatter.name == ""

    def test_frontmatter_missing_description(self):
        content = "---\nname: my-skill\n---\n\nBody"
        result = parse_skill_frontmatter(content)
        assert result is not None
        assert result.frontmatter.description == ""


class TestBuildSkillContent:
    """Test frontmatter content generation."""

    def test_basic_build(self):
        content = build_skill_content("my-skill", "A description.", "## Steps\n\n1. Do it")
        assert content.startswith("---\n")
        assert "name: my-skill" in content
        assert "description: A description." in content
        assert "## Steps" in content
        assert content.endswith("\n")

    def test_build_with_metadata(self):
        content = build_skill_content("deploy", "Deploy stuff.", "Body.", metadata={"author": "test"})
        assert "metadata:" in content
        assert 'author: "test"' in content

    def test_build_empty_body(self):
        content = build_skill_content("my-skill", "Desc.", "")
        assert "name: my-skill" in content
        assert content.endswith("\n")

    def test_roundtrip(self):
        """Build then parse should preserve data."""
        content = build_skill_content("my-skill", "A useful skill.", "Instructions here.")
        parsed = parse_skill_frontmatter(content)
        assert parsed is not None
        assert parsed.frontmatter.name == "my-skill"
        assert parsed.frontmatter.description == "A useful skill."
        assert "Instructions here." in parsed.body
