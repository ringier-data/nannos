"""Unit tests for the skills registry router and service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from console_backend.models.skills_registry import (
    SkillAuditEntry,
    SkillAuditResponse,
    SkillDetailResponse,
    SkillFile,
    SkillSearchResponse,
    SkillSearchResult,
)
from console_backend.models.user import User, UserRole
from console_backend.routers.skills_registry_router import browse_repo, get_skill_audit, get_skill_detail, search_skills


def _make_user(**overrides) -> User:
    """Create a test User model."""
    defaults = {
        "id": "user-id-1",
        "sub": "user-sub-1",
        "email": "test@example.com",
        "first_name": "Test",
        "last_name": "User",
        "role": UserRole.MEMBER,
    }
    defaults.update(overrides)
    return User(**defaults)


# --- Search endpoint tests ---


class TestSearchSkills:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        mock_results = [
            SkillSearchResult(
                id="vercel-labs/agent-skills/next-js-development",
                slug="next-js-development",
                name="Next.js Development",
                source="vercel-labs/agent-skills",
                installs=1500,
                url="https://skills.sh/vercel-labs/agent-skills/next-js-development",
                source_type="github",
                install_url="https://github.com/vercel-labs/agent-skills",
            )
        ]

        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.search_skills",
            new_callable=AsyncMock,
            return_value=(mock_results, "fuzzy"),
        ) as mock_search:
            result = await search_skills(q="nextjs", limit=10, user=_make_user())

            assert isinstance(result, SkillSearchResponse)
            assert len(result.data) == 1
            assert result.data[0].name == "Next.js Development"
            assert result.query == "nextjs"
            assert result.count == 1
            assert result.search_type == "fuzzy"
            mock_search.assert_called_once_with(query="nextjs", limit=10)

    @pytest.mark.asyncio
    async def test_search_empty_results(self):
        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.search_skills",
            new_callable=AsyncMock,
            return_value=([], None),
        ):
            result = await search_skills(q="nonexistent-skill-xyz", limit=10, user=_make_user())

            assert isinstance(result, SkillSearchResponse)
            assert len(result.data) == 0
            assert result.count == 0


# --- Browse endpoint tests ---


class TestBrowseRepo:
    @pytest.mark.asyncio
    async def test_browse_valid_repo(self):
        mock_results = [
            SkillSearchResult(
                id="anthropics/skills/code-review",
                slug="code-review",
                name="code-review",
                source="anthropics/skills",
                installs=0,
                url="https://github.com/anthropics/skills/tree/main/skills/code-review",
                source_type="github",
            )
        ]

        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.browse_repo",
            new_callable=AsyncMock,
            return_value=mock_results,
        ) as mock_browse:
            result = await browse_repo(repo="anthropics/skills", ref="main", user=_make_user())

            assert isinstance(result, SkillSearchResponse)
            assert len(result.data) == 1
            assert result.data[0].slug == "code-review"
            mock_browse.assert_called_once_with(repo="anthropics/skills", ref="main")

    @pytest.mark.asyncio
    async def test_browse_invalid_repo_format(self):
        with pytest.raises(Exception) as exc_info:
            await browse_repo(repo="invalid-no-slash", ref="main", user=_make_user())

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_browse_empty_repo(self):
        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.browse_repo",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await browse_repo(repo="empty/repo", ref="main", user=_make_user())

            assert result.count == 0


# --- Detail endpoint tests ---


class TestGetSkillDetail:
    @pytest.mark.asyncio
    async def test_detail_found(self):
        mock_detail = SkillDetailResponse(
            id="vercel-labs/agent-skills/next-js-development",
            source="vercel-labs/agent-skills",
            slug="next-js-development",
            installs=24531,
            hash="a1b2c3d4e5f6",
            files=[SkillFile(path="SKILL.md", contents="# Next.js Development")],
        )

        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.get_skill_detail",
            new_callable=AsyncMock,
            return_value=mock_detail,
        ):
            result = await get_skill_detail(
                skill_id="vercel-labs/agent-skills/next-js-development",
                user=_make_user(),
            )

            assert isinstance(result, SkillDetailResponse)
            assert result.slug == "next-js-development"
            assert result.hash == "a1b2c3d4e5f6"
            assert len(result.files) == 1

    @pytest.mark.asyncio
    async def test_detail_not_found(self):
        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.get_skill_detail",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(Exception) as exc_info:
                await get_skill_detail(skill_id="nonexistent/skill", user=_make_user())

            assert exc_info.value.status_code == 404


# --- Audit endpoint tests ---


class TestGetSkillAudit:
    @pytest.mark.asyncio
    async def test_audit_found(self):
        mock_audit = SkillAuditResponse(
            id="vercel-labs/agent-skills/next-js-development",
            source="vercel-labs/agent-skills",
            slug="next-js-development",
            audits=[
                SkillAuditEntry(
                    provider="Gen Agent Trust Hub",
                    slug="agent-trust-hub",
                    status="pass",
                    summary="No risks detected",
                    audited_at="2026-04-15T12:00:00.000Z",
                    risk_level="LOW",
                )
            ],
        )

        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.get_skill_audit",
            new_callable=AsyncMock,
            return_value=mock_audit,
        ):
            result = await get_skill_audit(
                skill_id="vercel-labs/agent-skills/next-js-development",
                user=_make_user(),
            )

            assert isinstance(result, SkillAuditResponse)
            assert len(result.audits) == 1
            assert result.audits[0].status == "pass"
            assert result.audits[0].risk_level == "LOW"

    @pytest.mark.asyncio
    async def test_audit_not_found(self):
        with patch(
            "console_backend.routers.skills_registry_router.skills_registry_service.get_skill_audit",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(Exception) as exc_info:
                await get_skill_audit(skill_id="nonexistent/skill", user=_make_user())

            assert exc_info.value.status_code == 404


# --- Service unit tests ---


class TestSkillsRegistryService:
    @pytest.mark.asyncio
    async def test_search_skills_calls_skills_sh(self):
        """Verify service correctly calls skills.sh and parses response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "owner/repo/skill-name",
                    "slug": "skill-name",
                    "name": "Skill Name",
                    "source": "owner/repo",
                    "installs": 42,
                    "url": "https://skills.sh/s/skill-name",
                    "sourceType": "github",
                }
            ]
        }

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = AsyncMock(return_value=mock_response)
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            from console_backend.services.skills_registry_service import SkillsRegistryService

            service = SkillsRegistryService()
            results, search_type = await service.search_skills("test query", limit=10)

            assert len(results) == 1
            assert results[0].id == "owner/repo/skill-name"
            assert results[0].installs == 42

    @pytest.mark.asyncio
    async def test_search_skills_handles_api_error(self):
        """Verify service returns empty list on API error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = AsyncMock(return_value=mock_response)
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            from console_backend.services.skills_registry_service import SkillsRegistryService

            service = SkillsRegistryService()
            results, search_type = await service.search_skills("test", limit=10)

            assert results == []
            assert search_type is None

    @pytest.mark.asyncio
    async def test_search_skills_skips_short_queries(self):
        """Verify service rejects queries under 2 characters."""
        from console_backend.services.skills_registry_service import SkillsRegistryService

        service = SkillsRegistryService()
        results, search_type = await service.search_skills("x", limit=10)
        assert results == []
        assert search_type is None

    @pytest.mark.asyncio
    async def test_search_skills_filters_duplicates(self):
        """Verify service skips items marked as duplicates."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "owner/repo/skill-1",
                    "slug": "skill-1",
                    "name": "Skill 1",
                    "source": "owner/repo",
                    "isDuplicate": False,
                },
                {
                    "id": "fork/repo/skill-1",
                    "slug": "skill-1",
                    "name": "Skill 1 (fork)",
                    "source": "fork/repo",
                    "isDuplicate": True,
                },
            ]
        }

        with patch("httpx.AsyncClient") as MockClient:
            client_instance = AsyncMock()
            client_instance.get = AsyncMock(return_value=mock_response)
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client_instance

            from console_backend.services.skills_registry_service import SkillsRegistryService

            service = SkillsRegistryService()
            results, search_type = await service.search_skills("skill", limit=10)

            assert len(results) == 1
            assert results[0].id == "owner/repo/skill-1"

    def test_parse_repo_valid(self):
        from console_backend.services.skills_registry_service import SkillsRegistryService

        service = SkillsRegistryService()
        owner, repo = service._parse_repo("anthropics/skills")
        assert owner == "anthropics"
        assert repo == "skills"

    def test_parse_repo_invalid(self):
        from console_backend.services.skills_registry_service import SkillsRegistryService

        service = SkillsRegistryService()
        with pytest.raises(ValueError):
            service._parse_repo("no-slash-here")

    def test_extract_description_from_frontmatter(self):
        from console_backend.services.skills_registry_service import SkillsRegistryService

        service = SkillsRegistryService()

        content = '---\nname: my-skill\ndescription: "A great skill for testing"\n---\n# Content'
        assert service._extract_description_from_frontmatter(content) == "A great skill for testing"

    def test_extract_description_no_frontmatter(self):
        from console_backend.services.skills_registry_service import SkillsRegistryService

        service = SkillsRegistryService()

        content = "# Just a markdown file\nNo frontmatter here."
        assert service._extract_description_from_frontmatter(content) == ""
