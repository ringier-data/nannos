"""Unit tests for the playbook router endpoints."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from console_backend.models.playbook import PlaybookUpdate, SkillCreate, SkillUpdate
from console_backend.models.user import User, UserRole
from console_backend.routers.playbook_router import (
    _resolve_group,
    _validate_skill_name,
    create_skill,
    delete_playbook,
    delete_skill,
    get_playbook,
    get_playbook_service,
    get_skill,
    list_skills,
    update_playbook,
    update_skill,
)

# --- Fixtures ---


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


def _make_request(
    service: MagicMock | None = None,
    memberships: list | None = None,
) -> MagicMock:
    """Create a mock request with app.state populated."""
    request = MagicMock()
    if service is None:
        service = MagicMock()
        service.is_available = True
    request.app.state.playbook_service = service

    ugs = AsyncMock()
    if memberships is not None:
        ugs.get_user_group_memberships = AsyncMock(return_value=memberships)
    else:
        ugs.get_user_group_memberships = AsyncMock(return_value=[])
    request.app.state.user_group_service = ugs
    return request


DEFAULT_MEMBERSHIPS = [
    {"id": 10, "name": "Alpha Team", "group_role": "write"},
    {"id": 20, "name": "Beta Team", "group_role": "read"},
]


# --- _resolve_group tests ---


class TestResolveGroup:
    def test_no_memberships(self):
        gid, role = _resolve_group([], None)
        assert gid is None
        assert role is None

    def test_default_to_first(self):
        gid, role = _resolve_group(DEFAULT_MEMBERSHIPS, None)
        assert gid == "10"
        assert role == "write"

    def test_explicit_group_id_found(self):
        gid, role = _resolve_group(DEFAULT_MEMBERSHIPS, "20")
        assert gid == "20"
        assert role == "read"

    def test_explicit_group_id_not_found(self):
        gid, role = _resolve_group(DEFAULT_MEMBERSHIPS, "999")
        assert gid is None
        assert role is None

    def test_explicit_group_id_first(self):
        gid, role = _resolve_group(DEFAULT_MEMBERSHIPS, "10")
        assert gid == "10"
        assert role == "write"


# --- _validate_skill_name tests ---


class TestValidateSkillName:
    def test_valid_names(self):
        for name in ["my-skill", "skill-1", "a123", "incident-triage", "a"]:
            _validate_skill_name(name)  # Should not raise

    def test_empty_name(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_skill_name("")
        assert exc_info.value.status_code == 400

    def test_invalid_characters(self):
        for name in ["has space", "has/slash", "has.dot", "has@at", "../traversal", "my_skill", "CamelCase", "UPPER"]:
            with pytest.raises(HTTPException) as exc_info:
                _validate_skill_name(name)
            assert exc_info.value.status_code == 400

    def test_consecutive_hyphens_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_skill_name("my--skill")
        assert exc_info.value.status_code == 400

    def test_leading_trailing_hyphens_rejected(self):
        for name in ["-leading", "trailing-", "-both-"]:
            with pytest.raises(HTTPException) as exc_info:
                _validate_skill_name(name)
            assert exc_info.value.status_code == 400

    def test_too_long_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_skill_name("a" * 65)
        assert exc_info.value.status_code == 400


# --- get_playbook_service tests ---


class TestGetPlaybookService:
    def test_returns_service_when_available(self):
        request = _make_request()
        service = get_playbook_service(request)
        assert service is request.app.state.playbook_service

    def test_raises_503_when_unavailable(self):
        mock_service = MagicMock()
        mock_service.is_available = False
        request = _make_request(service=mock_service)
        with pytest.raises(HTTPException) as exc_info:
            get_playbook_service(request)
        assert exc_info.value.status_code == 503


# --- get_playbook endpoint tests ---


class TestGetPlaybook:
    @pytest.mark.asyncio
    async def test_personal_and_group_content(self):
        service = MagicMock()
        service.is_available = True
        service.get_agents_md = AsyncMock(side_effect=["# Personal", "# Alpha", "# Beta"])

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await get_playbook("orchestrator", request, db, current_user=user)

        assert result.personal is not None
        assert result.personal.content == "# Personal"
        assert result.personal.scope == "personal"
        assert len(result.groups) == 2
        assert result.groups[0].content == "# Alpha"
        assert result.groups[0].group_id == "10"
        assert result.groups[0].group_name == "Alpha Team"
        assert result.groups[1].content == "# Beta"
        assert result.groups[1].group_id == "20"

    @pytest.mark.asyncio
    async def test_personal_only_no_groups(self):
        service = MagicMock()
        service.is_available = True
        service.get_agents_md = AsyncMock(return_value="# Personal")

        request = _make_request(service=service, memberships=[])
        user = _make_user()
        db = AsyncMock()

        result = await get_playbook("orchestrator", request, db, current_user=user)

        assert result.personal is not None
        assert len(result.groups) == 0

    @pytest.mark.asyncio
    async def test_no_content_at_all(self):
        service = MagicMock()
        service.is_available = True
        service.get_agents_md = AsyncMock(return_value=None)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await get_playbook("orchestrator", request, db, current_user=user)

        assert result.personal is None
        assert len(result.groups) == 0

    @pytest.mark.asyncio
    async def test_only_some_groups_have_content(self):
        service = MagicMock()
        service.is_available = True
        service.get_agents_md = AsyncMock(side_effect=[None, "# Alpha", None])

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await get_playbook("orchestrator", request, db, current_user=user)

        assert result.personal is None
        assert len(result.groups) == 1
        assert result.groups[0].group_id == "10"


# --- update_playbook endpoint tests ---


class TestUpdatePlaybook:
    @pytest.mark.asyncio
    async def test_update_personal(self):
        service = MagicMock()
        service.is_available = True
        service.put_agents_md = AsyncMock()

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = PlaybookUpdate(content="# Updated personal")

        result = await update_playbook("orchestrator", "personal", body, request, db, group_id=None, current_user=user)

        assert result.scope == "personal"
        assert result.content == "# Updated personal"
        service.put_agents_md.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_group_with_write_role(self):
        service = MagicMock()
        service.is_available = True
        service.put_agents_md = AsyncMock()

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = PlaybookUpdate(content="# Updated group")

        result = await update_playbook("orchestrator", "group", body, request, db, group_id="10", current_user=user)

        assert result.scope == "group"
        assert result.content == "# Updated group"

    @pytest.mark.asyncio
    async def test_update_group_with_read_role_forbidden(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = PlaybookUpdate(content="# Should fail")

        with pytest.raises(HTTPException) as exc_info:
            await update_playbook("orchestrator", "group", body, request, db, group_id="20", current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_update_group_no_memberships(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=[])
        user = _make_user()
        db = AsyncMock()
        body = PlaybookUpdate(content="# Should fail")

        with pytest.raises(HTTPException) as exc_info:
            await update_playbook("orchestrator", "group", body, request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_invalid_scope(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service)
        user = _make_user()
        db = AsyncMock()
        body = PlaybookUpdate(content="# Nope")

        with pytest.raises(HTTPException) as exc_info:
            await update_playbook("orchestrator", "invalid", body, request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_group_with_manager_role(self):
        """Manager role should also be allowed to write."""
        memberships = [{"id": 30, "name": "Gamma", "group_role": "manager"}]
        service = MagicMock()
        service.is_available = True
        service.put_agents_md = AsyncMock()

        request = _make_request(service=service, memberships=memberships)
        user = _make_user()
        db = AsyncMock()
        body = PlaybookUpdate(content="# Manager update")

        result = await update_playbook("orchestrator", "group", body, request, db, group_id="30", current_user=user)
        assert result.scope == "group"


# --- delete_playbook endpoint tests ---


class TestDeletePlaybook:
    @pytest.mark.asyncio
    async def test_delete_personal(self):
        service = MagicMock()
        service.is_available = True
        service.delete_agents_md = AsyncMock(return_value=True)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        await delete_playbook("orchestrator", "personal", request, db, group_id=None, current_user=user)
        service.delete_agents_md.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_not_found(self):
        service = MagicMock()
        service.is_available = True
        service.delete_agents_md = AsyncMock(return_value=False)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await delete_playbook("orchestrator", "personal", request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_group_forbidden_read_role(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await delete_playbook("orchestrator", "group", request, db, group_id="20", current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_group_with_write_role(self):
        service = MagicMock()
        service.is_available = True
        service.delete_agents_md = AsyncMock(return_value=True)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        await delete_playbook("orchestrator", "group", request, db, group_id="10", current_user=user)
        service.delete_agents_md.assert_called_once()


# --- list_skills endpoint tests ---


class TestListSkills:
    @pytest.mark.asyncio
    async def test_lists_personal_and_group_skills(self):
        service = MagicMock()
        service.is_available = True
        service.list_skills = AsyncMock(
            side_effect=[
                [{"name": "skill1", "title": "Skill 1", "description": "Desc 1", "scope": "personal"}],
                [{"name": "skill2", "title": "Skill 2", "description": "Desc 2", "scope": "group"}],
                [],  # second group has no skills
            ]
        )

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await list_skills("orchestrator", request, db, current_user=user)

        assert len(result.items) == 2
        assert result.items[0].name == "skill1"
        assert result.items[0].scope == "personal"
        assert result.items[1].name == "skill2"
        assert result.items[1].scope == "group"
        assert result.items[1].group_id == "10"
        assert result.items[1].group_name == "Alpha Team"

    @pytest.mark.asyncio
    async def test_no_group_skills_when_no_memberships(self):
        service = MagicMock()
        service.is_available = True
        service.list_skills = AsyncMock(return_value=[])

        request = _make_request(service=service, memberships=[])
        user = _make_user()
        db = AsyncMock()

        result = await list_skills("orchestrator", request, db, current_user=user)

        assert len(result.items) == 0
        # Only one call for personal, no group call
        assert service.list_skills.call_count == 1

    @pytest.mark.asyncio
    async def test_lists_skills_from_multiple_groups(self):
        service = MagicMock()
        service.is_available = True
        service.list_skills = AsyncMock(
            side_effect=[
                [],  # personal
                [{"name": "alpha_skill", "title": "Alpha", "description": "", "scope": "group"}],
                [{"name": "beta_skill", "title": "Beta", "description": "", "scope": "group"}],
            ]
        )

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await list_skills("orchestrator", request, db, current_user=user)

        assert len(result.items) == 2
        assert result.items[0].group_id == "10"
        assert result.items[1].group_id == "20"


# --- get_skill endpoint tests ---


class TestGetSkill:
    @pytest.mark.asyncio
    async def test_auto_scope_finds_personal(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value="# Personal skill content")

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await get_skill(
            "orchestrator", "my-skill", request, db, scope="auto", group_id=None, current_user=user
        )
        assert result.scope == "personal"
        assert result.content == "# Personal skill content"

    @pytest.mark.asyncio
    async def test_auto_scope_falls_back_to_groups(self):
        service = MagicMock()
        service.is_available = True
        # personal returns None, first group returns None, second group returns content
        service.get_skill = AsyncMock(side_effect=[None, None, "# Beta skill content"])

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await get_skill(
            "orchestrator", "my-skill", request, db, scope="auto", group_id=None, current_user=user
        )
        assert result.scope == "group"
        assert result.content == "# Beta skill content"

    @pytest.mark.asyncio
    async def test_auto_scope_not_found(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value=None)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_skill("orchestrator", "my-skill", request, db, scope="auto", group_id=None, current_user=user)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_explicit_personal_scope(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value="# Content")

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await get_skill(
            "orchestrator", "my-skill", request, db, scope="personal", group_id=None, current_user=user
        )
        assert result.scope == "personal"

    @pytest.mark.asyncio
    async def test_explicit_scope_not_found(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value=None)

        request = _make_request(service=service, memberships=[])
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_skill("orchestrator", "my-skill", request, db, scope="personal", group_id=None, current_user=user)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_explicit_group_scope_with_group_id(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value="# Group skill")

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        result = await get_skill(
            "orchestrator", "my-skill", request, db, scope="group", group_id="10", current_user=user
        )
        assert result.scope == "group"
        assert result.content == "# Group skill"

    @pytest.mark.asyncio
    async def test_explicit_group_scope_no_group_id(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=[])
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_skill("orchestrator", "my-skill", request, db, scope="group", group_id=None, current_user=user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_skill_name(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_skill("orchestrator", "../evil", request, db, scope="personal", group_id=None, current_user=user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_scope(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock()

        request = _make_request(service=service)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_skill("orchestrator", "my-skill", request, db, scope="invalid", group_id=None, current_user=user)
        assert exc_info.value.status_code == 400


# --- create_skill endpoint tests ---


class TestCreateSkill:
    @pytest.mark.asyncio
    async def test_create_personal_skill(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value=None)
        service.put_skill = AsyncMock()

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillCreate(name="new-skill", description="A new skill", content="Do the thing.")

        result = await create_skill("orchestrator", "personal", body, request, db, group_id=None, current_user=user)

        assert result.name == "new-skill"
        assert result.scope == "personal"
        # Content now includes YAML frontmatter
        assert "---" in result.content
        assert "name: new-skill" in result.content
        assert "description: A new skill" in result.content
        assert "Do the thing." in result.content

    @pytest.mark.asyncio
    async def test_create_skill_conflict(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value="# Already exists")

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillCreate(name="existing", description="Existing skill", content="Content")

        with pytest.raises(HTTPException) as exc_info:
            await create_skill("orchestrator", "personal", body, request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_create_group_skill_with_write_role(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value=None)
        service.put_skill = AsyncMock()

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillCreate(name="team-skill", description="Team skill", content="Team instructions")

        result = await create_skill("orchestrator", "group", body, request, db, group_id="10", current_user=user)
        assert result.scope == "group"

    @pytest.mark.asyncio
    async def test_create_group_skill_read_role_forbidden(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillCreate(name="team-skill", description="Should fail", content="Body")

        with pytest.raises(HTTPException) as exc_info:
            await create_skill("orchestrator", "group", body, request, db, group_id="20", current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_create_skill_invalid_name(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillCreate(name="bad name!", description="Invalid", content="Content")

        with pytest.raises(HTTPException) as exc_info:
            await create_skill("orchestrator", "personal", body, request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_group_skill_no_group(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=[])
        user = _make_user()
        db = AsyncMock()
        body = SkillCreate(name="skill", description="A skill", content="Content")

        with pytest.raises(HTTPException) as exc_info:
            await create_skill("orchestrator", "group", body, request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 400


# --- update_skill endpoint tests ---


class TestUpdateSkill:
    @pytest.mark.asyncio
    async def test_update_personal_skill(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value="# Old content")
        service.put_skill = AsyncMock()

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillUpdate(content="# Updated content")

        result = await update_skill(
            "orchestrator", "personal", "my-skill", body, request, db, group_id=None, current_user=user
        )
        assert result.content == "# Updated content"

    @pytest.mark.asyncio
    async def test_update_skill_not_found(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value=None)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillUpdate(content="# Content")

        with pytest.raises(HTTPException) as exc_info:
            await update_skill(
                "orchestrator", "personal", "nonexistent", body, request, db, group_id=None, current_user=user
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_group_skill_forbidden(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillUpdate(content="# Content")

        with pytest.raises(HTTPException) as exc_info:
            await update_skill("orchestrator", "group", "my-skill", body, request, db, group_id="20", current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_update_group_skill_with_write_role(self):
        service = MagicMock()
        service.is_available = True
        service.get_skill = AsyncMock(return_value="# Old")
        service.put_skill = AsyncMock()

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()
        body = SkillUpdate(content="# New")

        result = await update_skill(
            "orchestrator", "group", "my-skill", body, request, db, group_id="10", current_user=user
        )
        assert result.scope == "group"


# --- delete_skill endpoint tests ---


class TestDeleteSkill:
    @pytest.mark.asyncio
    async def test_delete_personal_skill(self):
        service = MagicMock()
        service.is_available = True
        service.delete_skill = AsyncMock(return_value=True)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        await delete_skill("orchestrator", "personal", "my-skill", request, db, group_id=None, current_user=user)
        service.delete_skill.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_skill_not_found(self):
        service = MagicMock()
        service.is_available = True
        service.delete_skill = AsyncMock(return_value=False)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await delete_skill("orchestrator", "personal", "my-skill", request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_group_skill_forbidden(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await delete_skill("orchestrator", "group", "my-skill", request, db, group_id="20", current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_group_skill_with_write_role(self):
        service = MagicMock()
        service.is_available = True
        service.delete_skill = AsyncMock(return_value=True)

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        await delete_skill("orchestrator", "group", "my-skill", request, db, group_id="10", current_user=user)
        service.delete_skill.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_skill_invalid_name(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await delete_skill("orchestrator", "personal", "bad/name", request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_skill_invalid_scope(self):
        service = MagicMock()
        service.is_available = True

        request = _make_request(service=service, memberships=DEFAULT_MEMBERSHIPS)
        user = _make_user()
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await delete_skill("orchestrator", "invalid", "my-skill", request, db, group_id=None, current_user=user)
        assert exc_info.value.status_code == 400
