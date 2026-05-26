"""Playbook management API endpoints.

Provides CRUD operations for AGENTS.md playbooks and skill files.
Reads/writes directly to the LangGraph store table in the docstore database.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from console_backend.db.session import DbSession
from console_backend.dependencies import require_auth, require_auth_or_bearer_token
from console_backend.models.playbook import (
    McpPlaybookUpdate,
    PlaybookContent,
    PlaybookListResponse,
    PlaybookUpdate,
)
from console_backend.models.skills_registry import (
    McpSkillResponse,
    SkillCreate,
    SkillDetail,
    SkillFileSummary,
    SkillListResponse,
    SkillSummary,
    SkillUpdate,
)
from console_backend.models.user import User
from console_backend.routers.skills_registry_router import (
    _SKILLS_EXCLUDED_AGENTS,
    _build_skill_content,
    _get_user_group_context,
    _require_agent_name,
    _resolve_group,
    _validate_file_path,
    _validate_skill_name,
)
from console_backend.services.playbook_service import PlaybookService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/playbooks", tags=["playbooks"])


def get_playbook_service(request: Request) -> PlaybookService:
    """Get the playbook service from app state."""
    service: PlaybookService = request.app.state.playbook_service
    if not service.is_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playbook service is not configured (docstore connection unavailable)",
        )
    return service


# --- AGENTS.md Endpoints ---


@router.get("/agents/{agent_name}", response_model=PlaybookListResponse)
async def get_playbook(
    agent_name: str,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
) -> PlaybookListResponse:
    """Get AGENTS.md content for an agent (personal + all user groups)."""
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)

    personal_content = await service.get_agents_md(
        user_id=current_user.id,
        agent_name=agent_name,
        scope="personal",
    )

    group_playbooks: list[PlaybookContent] = []
    for m in memberships:
        gid = str(m["id"])
        group_content = await service.get_agents_md(
            user_id=current_user.id,
            agent_name=agent_name,
            scope="group",
            group_id=gid,
        )
        if group_content is not None:
            group_playbooks.append(
                PlaybookContent(
                    agent_name=agent_name,
                    scope="group",
                    content=group_content,
                    group_id=gid,
                    group_name=m.get("name"),
                )
            )

    return PlaybookListResponse(
        personal=PlaybookContent(agent_name=agent_name, scope="personal", content=personal_content)
        if personal_content is not None
        else None,
        groups=group_playbooks,
    )


@router.put("/agents/{agent_name}/{scope}", response_model=PlaybookContent)
async def update_playbook(
    agent_name: str,
    scope: str,
    body: PlaybookUpdate,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> PlaybookContent:
    """Update AGENTS.md for an agent in the specified scope."""
    if scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, group_role = _resolve_group(memberships, group_id)

    if scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400, detail="No group context available or not a member of the specified group"
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You need at least 'write' group role to modify group playbooks",
            )

    await service.put_agents_md(
        user_id=current_user.id,
        agent_name=agent_name,
        scope=scope,
        content=body.content,
        group_id=resolved_group_id,
    )

    return PlaybookContent(agent_name=agent_name, scope=scope, content=body.content)


@router.delete("/agents/{agent_name}/{scope}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_playbook(
    agent_name: str,
    scope: str,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> None:
    """Delete AGENTS.md for an agent in the specified scope."""
    if scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, group_role = _resolve_group(memberships, group_id)

    if scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400, detail="No group context available or not a member of the specified group"
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You need at least 'write' group role to modify group playbooks",
            )

    deleted = await service.delete_agents_md(
        user_id=current_user.id,
        agent_name=agent_name,
        scope=scope,
        group_id=resolved_group_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Playbook not found")


# --- Skills Endpoints ---


@router.get("/agents/{agent_name}/skills", response_model=SkillListResponse)
async def list_skills(
    agent_name: str,
    request: Request,
    db: DbSession,
    current_user: User = Depends(require_auth),
) -> SkillListResponse:
    """List all skill files for an agent (personal + all user groups)."""
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)

    personal_skills = await service.list_skills(
        user_id=current_user.id,
        agent_name=agent_name,
        scope="personal",
    )

    group_skills: list[dict[str, str]] = []
    for m in memberships:
        gid = str(m["id"])
        skills = await service.list_skills(
            user_id=current_user.id,
            agent_name=agent_name,
            scope="group",
            group_id=gid,
        )
        for s in skills:
            s["group_id"] = gid
            s["group_name"] = m.get("name", "")
        group_skills.extend(skills)

    items = [SkillSummary(**s) for s in personal_skills + group_skills]
    return SkillListResponse(items=items)


@router.get("/agents/{agent_name}/skills/{skill_name}", response_model=SkillDetail)
async def get_skill(
    agent_name: str,
    skill_name: str,
    request: Request,
    db: DbSession,
    scope: str = "auto",
    group_id: str | None = Query(None, description="Group ID (required when scope='group')"),
    current_user: User = Depends(require_auth),
) -> SkillDetail:
    """Get a skill's SKILL.md content and list of bundled files.

    scope: 'personal', 'group', or 'auto' (tries personal first, then all groups).
    """
    _validate_skill_name(skill_name)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)

    if scope == "auto":
        # Try personal first
        content = await service.get_skill(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope="personal",
        )
        if content:
            files = await service.list_skill_files(
                user_id=current_user.id,
                agent_name=agent_name,
                skill_name=skill_name,
                scope="personal",
            )
            return SkillDetail(
                name=skill_name,
                scope="personal",
                content=content,
                files=[SkillFileSummary(path=p) for p in files],
            )

        # Fallback: search all groups
        for m in memberships:
            gid = str(m["id"])
            content = await service.get_skill(
                user_id=current_user.id,
                agent_name=agent_name,
                skill_name=skill_name,
                scope="group",
                group_id=gid,
            )
            if content:
                files = await service.list_skill_files(
                    user_id=current_user.id,
                    agent_name=agent_name,
                    skill_name=skill_name,
                    scope="group",
                    group_id=gid,
                )
                return SkillDetail(
                    name=skill_name,
                    scope="group",
                    content=content,
                    files=[SkillFileSummary(path=p) for p in files],
                )

        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    if scope == "group":
        resolved_group_id, _ = _resolve_group(memberships, group_id)
        if not resolved_group_id:
            raise HTTPException(status_code=400, detail="group_id required for group scope")
        content = await service.get_skill(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope="group",
            group_id=resolved_group_id,
        )
        if not content:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found in group scope")
        files = await service.list_skill_files(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope="group",
            group_id=resolved_group_id,
        )
        return SkillDetail(
            name=skill_name,
            scope="group",
            content=content,
            files=[SkillFileSummary(path=p) for p in files],
        )

    if scope == "personal":
        content = await service.get_skill(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope="personal",
        )
        if not content:
            raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found in personal scope")
        files = await service.list_skill_files(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope="personal",
        )
        return SkillDetail(
            name=skill_name,
            scope="personal",
            content=content,
            files=[SkillFileSummary(path=p) for p in files],
        )

    raise HTTPException(status_code=400, detail="scope must be 'personal', 'group', or 'auto'")


@router.post(
    "/agents/{agent_name}/skills/{scope}",
    response_model=SkillDetail,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    description="Deprecated: Use POST /api/v1/skills/registry/ + POST /api/v1/skills/activations/ instead.",
)
async def create_skill(
    agent_name: str,
    scope: str,
    body: SkillCreate,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> SkillDetail:
    """Create a new skill file."""
    if agent_name in _SKILLS_EXCLUDED_AGENTS:
        raise HTTPException(status_code=400, detail=f"Skills are not supported for '{agent_name}'.")
    if scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(body.name)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, group_role = _resolve_group(memberships, group_id)

    if scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400, detail="No group context available or not a member of the specified group"
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You need at least 'write' group role to create group skills",
            )

    # Check if skill already exists
    existing = await service.get_skill(
        user_id=current_user.id,
        agent_name=agent_name,
        skill_name=body.name,
        scope=scope,
        group_id=resolved_group_id,
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Skill '{body.name}' already exists. Use PUT to update.",
        )

    # Build SKILL.md content with frontmatter
    skill_content = _build_skill_content(body.name, body.description, body.content)

    # Convert files if provided
    files_data = None
    if body.files:
        files_data = [{"path": f.path, "content": f.content} for f in body.files]

    try:
        await service.put_skill_with_files(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=body.name,
            scope=scope,
            content=skill_content,
            files=files_data,
            group_id=resolved_group_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    file_summaries = [SkillFileSummary(path=f.path) for f in body.files] if body.files else []
    return SkillDetail(name=body.name, scope=scope, content=skill_content, files=file_summaries)


@router.put(
    "/agents/{agent_name}/skills/{scope}/{skill_name}",
    response_model=SkillDetail,
    deprecated=True,
    description="Deprecated: Use PUT /api/v1/skills/registry/{id} instead.",
)
async def update_skill(
    agent_name: str,
    scope: str,
    skill_name: str,
    body: SkillUpdate,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> SkillDetail:
    """Update an existing skill file."""
    if agent_name in _SKILLS_EXCLUDED_AGENTS:
        raise HTTPException(status_code=400, detail=f"Skills are not supported for '{agent_name}'.")
    if scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(skill_name)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, group_role = _resolve_group(memberships, group_id)

    if scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400, detail="No group context available or not a member of the specified group"
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You need at least 'write' group role to modify group skills",
            )

    # Verify skill exists
    existing = await service.get_skill(
        user_id=current_user.id,
        agent_name=agent_name,
        skill_name=skill_name,
        scope=scope,
        group_id=resolved_group_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found in {scope} scope")

    # Convert files if provided
    files_data = None
    if body.files is not None:
        files_data = [{"path": f.path, "content": f.content} for f in body.files]

    try:
        await service.put_skill_with_files(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope=scope,
            content=body.content,
            files=files_data,
            group_id=resolved_group_id,
            replace_files=True,  # On update, provided files replace all existing
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    file_summaries = [SkillFileSummary(path=f.path) for f in body.files] if body.files else []
    return SkillDetail(name=skill_name, scope=scope, content=body.content, files=file_summaries)


@router.delete(
    "/agents/{agent_name}/skills/{scope}/{skill_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    deprecated=True,
    description="Deprecated: Use DELETE /api/v1/skills/activations/{id} instead.",
)
async def delete_skill(
    agent_name: str,
    scope: str,
    skill_name: str,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> None:
    """Delete a skill file."""
    if agent_name in _SKILLS_EXCLUDED_AGENTS:
        raise HTTPException(status_code=400, detail=f"Skills are not supported for '{agent_name}'.")
    if scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(skill_name)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, group_role = _resolve_group(memberships, group_id)

    if scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400, detail="No group context available or not a member of the specified group"
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You need at least 'write' group role to delete group skills",
            )

    deleted = await service.delete_skill(
        user_id=current_user.id,
        agent_name=agent_name,
        skill_name=skill_name,
        scope=scope,
        group_id=resolved_group_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")


# --- Skill File Endpoints ---


class SkillFileContent(BaseModel):
    """Response for reading a skill file."""

    path: str
    content: str


class SkillFileListResponse(BaseModel):
    """Response for listing files in a skill."""

    items: list[SkillFileSummary] = Field(default_factory=list)


class SkillFileWrite(BaseModel):
    """Request body for writing a skill file."""

    content: str = Field(description="File content (text)")


@router.get("/agents/{agent_name}/skills/{skill_name}/files", response_model=SkillFileListResponse)
async def list_skill_files(
    agent_name: str,
    skill_name: str,
    scope: str,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> SkillFileListResponse:
    """List files within a skill folder (excluding SKILL.md)."""
    _validate_skill_name(skill_name)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, _ = _resolve_group(memberships, group_id)

    if scope == "group" and not resolved_group_id:
        raise HTTPException(status_code=400, detail="group_id required for group scope")

    files = await service.list_skill_files(
        user_id=current_user.id,
        agent_name=agent_name,
        skill_name=skill_name,
        scope=scope,
        group_id=resolved_group_id,
    )
    return SkillFileListResponse(items=[SkillFileSummary(path=p) for p in files])


@router.get("/agents/{agent_name}/skills/{skill_name}/files/{file_path:path}", response_model=SkillFileContent)
async def get_skill_file(
    agent_name: str,
    skill_name: str,
    file_path: str,
    scope: str,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> SkillFileContent:
    """Read a single file from a skill folder."""
    _validate_skill_name(skill_name)
    _validate_file_path(file_path)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, _ = _resolve_group(memberships, group_id)

    if scope == "group" and not resolved_group_id:
        raise HTTPException(status_code=400, detail="group_id required for group scope")

    content = await service.get_skill_file(
        user_id=current_user.id,
        agent_name=agent_name,
        skill_name=skill_name,
        file_path=file_path,
        scope=scope,
        group_id=resolved_group_id,
    )
    if content is None:
        raise HTTPException(status_code=404, detail=f"File '{file_path}' not found in skill '{skill_name}'")
    return SkillFileContent(path=file_path, content=content)


@router.put("/agents/{agent_name}/skills/{skill_name}/files/{file_path:path}", response_model=SkillFileContent)
async def write_skill_file(
    agent_name: str,
    skill_name: str,
    file_path: str,
    body: SkillFileWrite,
    scope: str,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> SkillFileContent:
    """Write a file to a skill folder. Creates or updates the file."""
    if agent_name in _SKILLS_EXCLUDED_AGENTS:
        raise HTTPException(status_code=400, detail=f"Skills are not supported for '{agent_name}'.")
    if scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(skill_name)
    _validate_file_path(file_path)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, group_role = _resolve_group(memberships, group_id)

    if scope == "group":
        if not resolved_group_id:
            raise HTTPException(status_code=400, detail="group_id required for group scope")
        if group_role not in ("write", "manager"):
            raise HTTPException(status_code=403, detail="You need at least 'write' group role to write skill files")

    try:
        await service.put_skill_file(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            file_path=file_path,
            content=body.content,
            scope=scope,
            group_id=resolved_group_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return SkillFileContent(path=file_path, content=body.content)


@router.delete(
    "/agents/{agent_name}/skills/{skill_name}/files/{file_path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_skill_file(
    agent_name: str,
    skill_name: str,
    file_path: str,
    scope: str,
    request: Request,
    db: DbSession,
    group_id: str | None = Query(None, description="Group ID (required for group scope)"),
    current_user: User = Depends(require_auth),
) -> None:
    """Delete a file from a skill folder."""
    if scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(skill_name)
    _validate_file_path(file_path)
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, current_user)
    resolved_group_id, group_role = _resolve_group(memberships, group_id)

    if scope == "group":
        if not resolved_group_id:
            raise HTTPException(status_code=400, detail="group_id required for group scope")
        if group_role not in ("write", "manager"):
            raise HTTPException(status_code=403, detail="You need at least 'write' group role to delete skill files")

    try:
        deleted = await service.delete_skill_file(
            user_id=current_user.id,
            agent_name=agent_name,
            skill_name=skill_name,
            file_path=file_path,
            scope=scope,
            group_id=resolved_group_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not deleted:
        raise HTTPException(status_code=404, detail=f"File '{file_path}' not found in skill '{skill_name}'")


# --- MCP Tool Endpoints ---


@router.put(
    "/mcp/playbook",
    response_model=McpSkillResponse,
    tags=["MCP"],
    operation_id="console_update_playbook",
)
async def mcp_update_playbook(
    body: McpPlaybookUpdate,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> McpSkillResponse:
    """Update the AGENTS.md playbook for a sub-agent.

    The playbook provides behavioral guidance for the sub-agent. It is stored
    in the docstore (personal or group scope only — default scope uses the
    sub-agent's system prompt instead).

    Use 'personal' scope for private customizations, or 'group' to share
    behavioral guidance with your team.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(
            status_code=400,
            detail="scope must be 'personal' or 'group'. For default scope, update the sub-agent's system_prompt via console_update_sub_agent instead.",
        )

    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, user)
    resolved_group_id, group_role = _resolve_group(memberships, body.group_id)

    if body.scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400, detail="No group context available or not a member of the specified group"
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=403,
                detail="[ERROR_TYPE: auth] You need at least 'write' group role to modify group playbooks.",
            )

    await service.put_agents_md(
        user_id=user.id,
        agent_name=agent_name,
        scope=body.scope,
        content=body.content,
        group_id=resolved_group_id,
    )

    return McpSkillResponse(
        skill_name="AGENTS.md",
        scope=body.scope,
        agent_name=agent_name,
        message=f"Playbook updated for '{agent_name}' in {body.scope} scope.",
    )
