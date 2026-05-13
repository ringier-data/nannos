"""Playbook management API endpoints.

Provides CRUD operations for AGENTS.md playbooks and skill files.
Reads/writes directly to the LangGraph store table in the docstore database.
"""

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from console_backend.db.session import DbSession
from console_backend.dependencies import require_auth, require_auth_or_bearer_token
from console_backend.models.playbook import (
    McpPlaybookUpdate,
    McpSkillCreate,
    McpSkillDeleteFile,
    McpSkillRemove,
    McpSkillResponse,
    McpSkillUpdate,
    McpSkillWriteFile,
    PlaybookContent,
    PlaybookListResponse,
    PlaybookUpdate,
    SkillCreate,
    SkillDetail,
    SkillFileSummary,
    SkillListResponse,
    SkillSummary,
    SkillUpdate,
)
from console_backend.models.sub_agent import SkillDefinition, SubAgentUpdate
from console_backend.models.user import User
from console_backend.services.playbook_service import PlaybookService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/playbooks", tags=["playbooks"])


def _build_skill_content(name: str, description: str, body: str) -> str:
    """Build SKILL.md content with YAML frontmatter.

    Follows the Agent Skills spec (agentskills.io/specification).
    """
    lines = ["---", f"name: {name}"]
    if description:
        lines.append(f"description: {description}")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body)
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def get_playbook_service(request: Request) -> PlaybookService:
    """Get the playbook service from app state."""
    service: PlaybookService = request.app.state.playbook_service
    if not service.is_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Playbook service is not configured (docstore connection unavailable)",
        )
    return service


async def _get_user_group_context(
    request: Request, db: Any, user: User, group_id: str | None = None
) -> list[dict[str, Any]]:
    """Get the user's group memberships.

    Returns list of dicts with 'id', 'name', 'group_role'.
    """
    user_group_service = request.app.state.user_group_service
    return await user_group_service.get_user_group_memberships(db, user.id)


def _resolve_group(memberships: list[dict[str, Any]], group_id: str | None) -> tuple[str | None, str | None]:
    """Resolve which group to use and the user's role in it.

    If group_id is provided, validates the user is a member.
    If not provided, uses the first group (primary).
    Returns (group_id, group_role).
    """
    if not memberships:
        return None, None

    if group_id:
        for m in memberships:
            if str(m["id"]) == group_id:
                return str(m["id"]), m["group_role"]
        return None, None  # User is not a member of requested group

    # Default to first group
    primary = memberships[0]
    return str(primary["id"]), primary["group_role"]


def _validate_skill_name(name: str) -> None:
    """Validate skill name per the SKILL.md spec (agentskills.io/specification).

    Rules:
    - 1-64 characters
    - Lowercase alphanumeric + hyphens only
    - Must not start or end with a hyphen
    - Must not contain consecutive hyphens (--)
    """
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill name is required",
        )
    if len(name) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill name must be at most 64 characters",
        )
    if "--" in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill name must not contain consecutive hyphens (--)",
        )
    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill name must contain only lowercase letters, numbers, and hyphens, and must not start or end with a hyphen",
        )


def _validate_file_path(file_path: str) -> None:
    """Validate a skill file path.

    Rules:
    - Must be relative (no leading / or ~)
    - No path traversal (..)
    - Max 3 segments deep
    - Cannot be SKILL.md (managed via skill create/update)
    """
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path is required")
    if file_path.startswith("/") or file_path.startswith("~"):
        raise HTTPException(status_code=400, detail="File path must be relative")
    segments = file_path.split("/")
    if ".." in segments:
        raise HTTPException(status_code=400, detail="Path traversal not allowed")
    if len(segments) > 3:
        raise HTTPException(status_code=400, detail="File path exceeds max depth (3 segments)")
    if not all(segments):
        raise HTTPException(status_code=400, detail="Invalid file path (empty segments)")
    if file_path == "SKILL.md":
        raise HTTPException(
            status_code=400,
            detail="Cannot manage SKILL.md as a file — use the skill create/update endpoints instead.",
        )


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


@router.post("/agents/{agent_name}/skills/{scope}", response_model=SkillDetail, status_code=status.HTTP_201_CREATED)
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


@router.put("/agents/{agent_name}/skills/{scope}/{skill_name}", response_model=SkillDetail)
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


@router.delete("/agents/{agent_name}/skills/{scope}/{skill_name}", status_code=status.HTTP_204_NO_CONTENT)
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
# These endpoints are auto-discovered by FastApiMCP via the "MCP" tag.
# They provide a unified interface for skill/playbook management across all scopes.


def _get_sub_agent_service(request: Request):
    """Get the sub-agent service from app state."""
    return request.app.state.sub_agent_service


# Agents that do not support the skills feature.
_SKILLS_EXCLUDED_AGENTS = frozenset({"voice-agent"})


def _require_agent_name(agent_name: str | None) -> str:
    """Validate that agent_name was provided (either by caller or auto-injected)."""
    if not agent_name:
        raise HTTPException(
            status_code=400,
            detail="agent_name is required. Sub-agents auto-inject this; if calling directly, provide it explicitly.",
        )
    if agent_name in _SKILLS_EXCLUDED_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Skills are not supported for '{agent_name}'.",
        )
    return agent_name


@router.post(
    "/mcp/skills",
    response_model=McpSkillResponse,
    tags=["MCP"],
    operation_id="console_create_skill",
    status_code=status.HTTP_201_CREATED,
)
async def mcp_create_skill(
    body: McpSkillCreate,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> McpSkillResponse:
    """Create a new skill for a sub-agent.

    Scope determines where the skill is stored:
    - **personal**: Stored in user's private docstore. Immediate effect, no approval needed.
    - **group**: Stored in group docstore. Requires 'write' or 'manager' group role.
    - **default**: Stored in sub-agent config version (DB). Creates a new version that
      may require approval. Requires 'write' or 'owner' permission on the sub-agent.

    Use 'personal' scope for quick experimentation. Upgrade to 'group' to share with
    your team, or 'default' to make it part of the agent's official configuration.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group", "default"):
        raise HTTPException(status_code=400, detail="scope must be 'personal', 'group', or 'default'")

    _validate_skill_name(body.skill_name)

    if body.scope == "default":
        if not body.sub_agent_id:
            raise HTTPException(
                status_code=400,
                detail="sub_agent_id is required for default scope",
            )
        sub_agent_service = _get_sub_agent_service(request)
        existing = await sub_agent_service.get_sub_agent_by_id(db, body.sub_agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Sub-agent not found")

        # Check write permission
        has_write = await sub_agent_service.check_user_permission(
            db, body.sub_agent_id, user.id, "write", sub_agent=existing
        )
        if not has_write:
            raise HTTPException(
                status_code=403,
                detail=(
                    "[ERROR_TYPE: auth] You don't have write access to this sub-agent. "
                    "Try scope='personal' to save this skill to your personal playbook instead, "
                    "or scope='group' to share with your group."
                ),
            )

        # Check for duplicate skill name
        current_skills = existing.config_version.skills if existing.config_version else []
        if any(s.name == body.skill_name for s in current_skills):
            raise HTTPException(
                status_code=409,
                detail=f"Skill '{body.skill_name}' already exists in default config. Use console_update_skill to modify it.",
            )

        # Add skill to the config version via update
        new_skill = SkillDefinition(name=body.skill_name, description=body.description, body=body.body)
        updated_skills = list(current_skills) + [new_skill]
        update_data = SubAgentUpdate(skills=updated_skills)
        try:
            await sub_agent_service.update_sub_agent(db, body.sub_agent_id, update_data, actor=user)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=f"[ERROR_TYPE: auth] {e}")

        return McpSkillResponse(
            skill_name=body.skill_name,
            scope="default",
            agent_name=agent_name,
            message=f"Skill '{body.skill_name}' added to default config. A new version was created (may require approval).",
        )

    # Personal/group scope: use PlaybookService
    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, user)
    resolved_group_id, group_role = _resolve_group(memberships, body.group_id)

    if body.scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400,
                detail="No group context available or not a member of the specified group",
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=403,
                detail=(
                    "[ERROR_TYPE: auth] You need at least 'write' group role to create group skills. "
                    "Try scope='personal' instead."
                ),
            )

    # Check for existing skill
    existing_skill = await service.get_skill(
        user_id=user.id,
        agent_name=agent_name,
        skill_name=body.skill_name,
        scope=body.scope,
        group_id=resolved_group_id,
    )
    if existing_skill:
        raise HTTPException(
            status_code=409,
            detail=f"Skill '{body.skill_name}' already exists in {body.scope} scope. Use console_update_skill to modify it.",
        )

    skill_content = _build_skill_content(body.skill_name, body.description, body.body)
    files_data = None
    if body.files:
        files_data = [{"path": f.path, "content": f.content} for f in body.files]

    try:
        await service.put_skill_with_files(
            user_id=user.id,
            agent_name=agent_name,
            skill_name=body.skill_name,
            scope=body.scope,
            content=skill_content,
            files=files_data,
            group_id=resolved_group_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        message=f"Skill '{body.skill_name}' created in {body.scope} scope.",
    )


@router.put(
    "/mcp/skills",
    response_model=McpSkillResponse,
    tags=["MCP"],
    operation_id="console_update_skill",
)
async def mcp_update_skill(
    body: McpSkillUpdate,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> McpSkillResponse:
    """Update an existing skill for a sub-agent.

    For personal/group scope: provide 'body' (just the instructions) or 'content'
    (full SKILL.md with frontmatter).
    For default scope: provide 'body' and optionally 'description' to update the
    skill in the config version.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group", "default"):
        raise HTTPException(status_code=400, detail="scope must be 'personal', 'group', or 'default'")

    _validate_skill_name(body.skill_name)

    if body.scope == "default":
        if not body.sub_agent_id:
            raise HTTPException(status_code=400, detail="sub_agent_id is required for default scope")

        sub_agent_service = _get_sub_agent_service(request)
        existing = await sub_agent_service.get_sub_agent_by_id(db, body.sub_agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Sub-agent not found")

        has_write = await sub_agent_service.check_user_permission(
            db, body.sub_agent_id, user.id, "write", sub_agent=existing
        )
        if not has_write:
            raise HTTPException(
                status_code=403,
                detail=(
                    "[ERROR_TYPE: auth] You don't have write access to this sub-agent. "
                    "Try scope='personal' to save changes to your personal playbook instead."
                ),
            )

        current_skills = existing.config_version.skills if existing.config_version else []
        skill_idx = next((i for i, s in enumerate(current_skills) if s.name == body.skill_name), None)
        if skill_idx is None:
            raise HTTPException(
                status_code=404,
                detail=f"Skill '{body.skill_name}' not found in default config. Use console_create_skill to add it.",
            )

        # Update the skill
        old_skill = current_skills[skill_idx]
        updated_skill = SkillDefinition(
            name=body.skill_name,
            description=body.description if body.description is not None else old_skill.description,
            body=body.body if body.body is not None else old_skill.body,
            files=old_skill.files,
        )
        updated_skills = list(current_skills)
        updated_skills[skill_idx] = updated_skill
        update_data = SubAgentUpdate(skills=updated_skills)
        try:
            await sub_agent_service.update_sub_agent(db, body.sub_agent_id, update_data, actor=user)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=f"[ERROR_TYPE: auth] {e}")

        return McpSkillResponse(
            skill_name=body.skill_name,
            scope="default",
            agent_name=agent_name,
            message=f"Skill '{body.skill_name}' updated in default config. A new version was created.",
        )

    # Personal/group scope
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
                detail="[ERROR_TYPE: auth] You need at least 'write' group role to modify group skills.",
            )

    # Verify skill exists
    existing_content = await service.get_skill(
        user_id=user.id,
        agent_name=agent_name,
        skill_name=body.skill_name,
        scope=body.scope,
        group_id=resolved_group_id,
    )
    if not existing_content:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{body.skill_name}' not found in {body.scope} scope. Use console_create_skill to create it.",
        )

    # Determine content to write
    if body.content is not None:
        new_content = body.content
    elif body.body is not None:
        new_content = _build_skill_content(body.skill_name, body.description or "", body.body)
    else:
        raise HTTPException(status_code=400, detail="Either 'body' or 'content' must be provided")

    files_data = None
    if body.files is not None:
        files_data = [{"path": f.path, "content": f.content} for f in body.files]

    try:
        await service.put_skill_with_files(
            user_id=user.id,
            agent_name=agent_name,
            skill_name=body.skill_name,
            scope=body.scope,
            content=new_content,
            files=files_data,
            group_id=resolved_group_id,
            replace_files=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        message=f"Skill '{body.skill_name}' updated in {body.scope} scope.",
    )


@router.post(
    "/mcp/skills/remove",
    response_model=McpSkillResponse,
    tags=["MCP"],
    operation_id="console_remove_skill",
)
async def mcp_remove_skill(
    body: McpSkillRemove,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> McpSkillResponse:
    """Remove a skill from a sub-agent.

    For personal/group scope: deletes the skill file from docstore.
    For default scope: removes the skill from the config version (creates a new version).

    If you don't have write access for default scope, consider removing the skill
    from your personal scope instead (it will override/hide the default skill).
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group", "default"):
        raise HTTPException(status_code=400, detail="scope must be 'personal', 'group', or 'default'")

    _validate_skill_name(body.skill_name)

    if body.scope == "default":
        if not body.sub_agent_id:
            raise HTTPException(status_code=400, detail="sub_agent_id is required for default scope")

        sub_agent_service = _get_sub_agent_service(request)
        existing = await sub_agent_service.get_sub_agent_by_id(db, body.sub_agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Sub-agent not found")

        has_write = await sub_agent_service.check_user_permission(
            db, body.sub_agent_id, user.id, "write", sub_agent=existing
        )
        if not has_write:
            raise HTTPException(
                status_code=403,
                detail="[ERROR_TYPE: auth] You don't have write access to remove default skills.",
            )

        current_skills = existing.config_version.skills if existing.config_version else []
        new_skills = [s for s in current_skills if s.name != body.skill_name]
        if len(new_skills) == len(current_skills):
            raise HTTPException(
                status_code=404,
                detail=f"Skill '{body.skill_name}' not found in default config.",
            )

        update_data = SubAgentUpdate(skills=new_skills)
        try:
            await sub_agent_service.update_sub_agent(db, body.sub_agent_id, update_data, actor=user)
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=f"[ERROR_TYPE: auth] {e}")

        return McpSkillResponse(
            skill_name=body.skill_name,
            scope="default",
            agent_name=agent_name,
            message=f"Skill '{body.skill_name}' removed from default config.",
        )

    # Personal/group scope
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
                detail="[ERROR_TYPE: auth] You need at least 'write' group role to delete group skills.",
            )

    deleted = await service.delete_skill(
        user_id=user.id,
        agent_name=agent_name,
        skill_name=body.skill_name,
        scope=body.scope,
        group_id=resolved_group_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Skill '{body.skill_name}' not found in {body.scope} scope")

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        message=f"Skill '{body.skill_name}' removed from {body.scope} scope.",
    )


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


@router.post(
    "/mcp/skills/files",
    response_model=McpSkillResponse,
    tags=["MCP"],
    operation_id="console_write_skill_file",
)
async def mcp_write_skill_file(
    body: McpSkillWriteFile,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> McpSkillResponse:
    """Write a file to a skill folder.

    Creates or updates a single file within an existing skill.
    Cannot be used to write SKILL.md (use console_create_skill or console_update_skill instead).

    - **personal** scope: immediate effect, no approval needed.
    - **group** scope: requires 'write' or 'manager' group role.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(body.skill_name)
    _validate_file_path(body.file_path)

    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, user)
    resolved_group_id, group_role = _resolve_group(memberships, body.group_id)

    if body.scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400,
                detail="No group context available or not a member of the specified group",
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=403,
                detail="[ERROR_TYPE: auth] You need at least 'write' group role to write group skill files.",
            )

    try:
        await service.put_skill_file(
            user_id=user.id,
            agent_name=agent_name,
            skill_name=body.skill_name,
            file_path=body.file_path,
            content=body.content,
            scope=body.scope,
            group_id=resolved_group_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        message=f"File '{body.file_path}' written to skill '{body.skill_name}' in {body.scope} scope.",
    )


@router.post(
    "/mcp/skills/files/remove",
    response_model=McpSkillResponse,
    tags=["MCP"],
    operation_id="console_delete_skill_file",
)
async def mcp_delete_skill_file(
    body: McpSkillDeleteFile,
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth_or_bearer_token),
) -> McpSkillResponse:
    """Delete a file from a skill folder.

    Removes a single file from an existing skill. Cannot delete SKILL.md
    (use console_remove_skill to delete the entire skill instead).

    - **personal** scope: immediate effect.
    - **group** scope: requires 'write' or 'manager' group role.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(body.skill_name)
    _validate_file_path(body.file_path)

    service = get_playbook_service(request)
    memberships = await _get_user_group_context(request, db, user)
    resolved_group_id, group_role = _resolve_group(memberships, body.group_id)

    if body.scope == "group":
        if not resolved_group_id:
            raise HTTPException(
                status_code=400,
                detail="No group context available or not a member of the specified group",
            )
        if group_role not in ("write", "manager"):
            raise HTTPException(
                status_code=403,
                detail="[ERROR_TYPE: auth] You need at least 'write' group role to delete group skill files.",
            )

    try:
        deleted = await service.delete_skill_file(
            user_id=user.id,
            agent_name=agent_name,
            skill_name=body.skill_name,
            file_path=body.file_path,
            scope=body.scope,
            group_id=resolved_group_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"File '{body.file_path}' not found in skill '{body.skill_name}'",
        )

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        message=f"File '{body.file_path}' removed from skill '{body.skill_name}' in {body.scope} scope.",
    )
