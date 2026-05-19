"""Playbook management API endpoints.

Provides CRUD operations for AGENTS.md playbooks and skill files.
Reads/writes directly to the LangGraph store table in the docstore database.
"""

import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from console_backend.services.skill_activation_service import SkillActivationService
    from console_backend.services.skill_registry_service import SkillRegistryService
    from console_backend.services.sub_agent_service import SubAgentService

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
from console_backend.models.skills_registry import SkillFile as RegistrySkillFile
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
    request: Request, db: AsyncSession, user: User, group_id: str | None = None
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
    if len(segments) > 6:
        raise HTTPException(status_code=400, detail="File path exceeds max depth (6 segments)")
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
# These endpoints are auto-discovered by FastApiMCP via the "MCP" tag.
# They provide a unified interface for skill/playbook management across all scopes.


def _get_sub_agent_service(request: Request) -> "SubAgentService":
    """Get the sub-agent service from app state."""
    return request.app.state.sub_agent_service


# Agents that do not support the skills feature.
_SKILLS_EXCLUDED_AGENTS = frozenset({"voice-agent"})

# Orchestrator delegates skill ownership to general-purpose.
# When the orchestrator calls skill tools, resolve to GP's sub_agent_id.
_AGENT_NAME_ALIASES: dict[str, str] = {"orchestrator": "general-purpose"}


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


def _get_skill_activation_service(request: Request) -> "SkillActivationService":
    """Get the skill activation service from app state."""
    return request.app.state.skill_activation_service


def _get_skill_registry_service(request: Request) -> "SkillRegistryService":
    """Get the skill registry service from app state."""
    return request.app.state.skill_registry_service


async def _resolve_sub_agent_id(
    request: Request, db: AsyncSession, agent_name: str, sub_agent_id: int | None
) -> tuple[int, str]:
    """Resolve sub_agent_id from agent_name if not explicitly provided.

    Applies agent name aliases (e.g., orchestrator → general-purpose) before lookup.
    Returns (sub_agent_id, resolved_agent_name).
    """
    resolved_name = _AGENT_NAME_ALIASES.get(agent_name, agent_name)

    if sub_agent_id:
        return sub_agent_id, resolved_name

    from sqlalchemy import text as sa_text

    result = await db.execute(
        sa_text("SELECT id FROM sub_agents WHERE name = :name AND deleted_at IS NULL"),
        {"name": resolved_name},
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Sub-agent '{resolved_name}' not found. Cannot resolve sub_agent_id.",
        )
    return row, resolved_name


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
    """Create a new skill and activate it on the calling agent.

    Creates a skill in the registry and auto-activates it on the calling agent.
    The skill is immediately usable after creation.

    Scope determines the activation target:
    - **personal**: Only your conversations with this agent see the skill.
    - **group**: All group members' conversations see it. Requires 'write' group role.

    Visibility determines who can discover and activate the skill from the registry:
    - **private**: Only you (default).
    - **group**: Members of your groups.
    - **public**: Everyone on the platform.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    if body.visibility not in ("private", "group", "public"):
        raise HTTPException(status_code=400, detail="visibility must be 'private', 'group', or 'public'")

    _validate_skill_name(body.skill_name)

    # Resolve sub_agent_id
    sub_agent_id, resolved_name = await _resolve_sub_agent_id(request, db, agent_name, body.sub_agent_id)

    # Resolve group context
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

    # Build skill files for registry
    skill_content = _build_skill_content(body.skill_name, body.description, body.body)
    registry_files = [RegistrySkillFile(path="SKILL.md", contents=skill_content)]
    if body.files:
        for f in body.files:
            registry_files.append(RegistrySkillFile(path=f.path, contents=f.content))

    # Determine group_ids for registry visibility
    group_ids = [int(resolved_group_id)] if resolved_group_id and body.visibility == "group" else None

    # Create in registry
    registry_service = _get_skill_registry_service(request)
    try:
        entry = await registry_service.create_skill(
            db=db,
            actor=user,
            name=body.skill_name.replace("-", " ").title(),
            slug=body.skill_name,
            description=body.description,
            files=registry_files,
            visibility=body.visibility,
            group_ids=group_ids,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Auto-activate on calling agent
    activation_service = _get_skill_activation_service(request)
    try:
        await activation_service.activate(
            db=db,
            registry_id=entry.id,
            sub_agent_id=sub_agent_id,
            agent_name=resolved_name,
            scope=body.scope,
            user_id=user.id,
            group_id=int(resolved_group_id) if resolved_group_id and body.scope == "group" else None,
            activated_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        registry_id=entry.id,
        message=f"Skill '{body.skill_name}' created and activated ({body.scope} scope).",
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
    """Update a skill in the registry and refresh the calling agent's activation.

    Finds the registry entry via the calling agent's activation, updates it,
    and refreshes the docstore snapshot for this agent. Other agents' activations
    remain pinned at their current content_hash until they explicitly self-update.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(body.skill_name)

    # Resolve sub_agent_id
    sub_agent_id, resolved_name = await _resolve_sub_agent_id(request, db, agent_name, body.sub_agent_id)

    # Resolve group context
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

    # Find registry entry: by registry_id or by activation on calling agent
    registry_service = _get_skill_registry_service(request)
    activation_service = _get_skill_activation_service(request)

    if body.registry_id:
        entry = await registry_service.get_by_id(db, body.registry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Registry entry '{body.registry_id}' not found")
    else:
        # Look up via activation
        activation = await activation_service.find_activation_by_skill_name(
            db,
            sub_agent_id=sub_agent_id,
            skill_name=body.skill_name,
            scope=body.scope,
            user_id=user.id if body.scope == "personal" else None,
            group_id=int(resolved_group_id) if resolved_group_id else None,
        )
        if not activation:
            raise HTTPException(
                status_code=404,
                detail=f"Skill '{body.skill_name}' not found in {body.scope} scope on this agent.",
            )
        entry = await registry_service.get_by_id(db, str(activation["registry_id"]))
        if not entry:
            raise HTTPException(status_code=404, detail="Registry entry for this activation no longer exists")

    # Build updated files
    current_files = entry.files or []
    if body.files is not None:
        # Replace all files (except SKILL.md which we handle separately)
        new_files = []
        for f in body.files:
            new_files.append(RegistrySkillFile(path=f.path, contents=f.content))
        registry_files = new_files
    else:
        # Keep existing non-SKILL.md files
        registry_files = [f for f in current_files if f.path != "SKILL.md"]

    # Build updated SKILL.md
    if body.content is not None:
        skill_content = body.content
    elif body.body is not None:
        skill_content = _build_skill_content(body.skill_name, body.description or "", body.body)
    else:
        raise HTTPException(status_code=400, detail="Either 'body' or 'content' must be provided")

    registry_files.insert(0, RegistrySkillFile(path="SKILL.md", contents=skill_content))

    # Update registry
    try:
        updated_entry = await registry_service.update_skill(
            db=db,
            actor=user,
            skill_id=entry.id,
            files=registry_files,
            description=body.description,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Self-update: refresh this agent's activation / docstore snapshot
    await activation_service.self_update(
        db=db,
        registry_id=entry.id,
        sub_agent_id=sub_agent_id,
        agent_name=resolved_name,
        actor=user,
    )

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        registry_id=entry.id,
        message=f"Skill '{body.skill_name}' updated in registry and refreshed on this agent.",
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
    """Deactivate a skill from the calling agent.

    Removes the skill's activation and cleans up the docstore snapshot.
    The registry entry is preserved so other consumers are unaffected.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(body.skill_name)

    # Resolve sub_agent_id
    sub_agent_id, resolved_name = await _resolve_sub_agent_id(request, db, agent_name, body.sub_agent_id)

    # Resolve group context
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
                detail="[ERROR_TYPE: auth] You need at least 'write' group role to deactivate group skills.",
            )

    # Find and deactivate
    activation_service = _get_skill_activation_service(request)

    activation = await activation_service.find_activation_by_skill_name(
        db,
        sub_agent_id=sub_agent_id,
        skill_name=body.skill_name,
        scope=body.scope,
        user_id=user.id if body.scope == "personal" else None,
        group_id=int(resolved_group_id) if resolved_group_id else None,
    )
    if not activation:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{body.skill_name}' not found in {body.scope} scope on this agent.",
        )

    try:
        deactivated = await activation_service.deactivate(
            db=db,
            activation_id=activation["id"],
            agent_name=resolved_name,
            user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not deactivated:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{body.skill_name}' not found in {body.scope} scope on this agent.",
        )

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        message=f"Skill '{body.skill_name}' deactivated from this agent ({body.scope} scope).",
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
    """Write a file to a skill in the registry.

    Creates or updates a single file within an existing skill. Updates the registry
    entry and refreshes the calling agent's docstore snapshot.
    Cannot be used to write SKILL.md (use console_update_skill instead).

    - **personal** scope: immediate effect, no approval needed.
    - **group** scope: requires 'write' or 'manager' group role.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(body.skill_name)
    _validate_file_path(body.file_path)

    # Resolve sub_agent_id
    sub_agent_id, resolved_name = await _resolve_sub_agent_id(request, db, agent_name, body.sub_agent_id)

    # Resolve group context
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

    # Find registry entry via activation
    activation_service = _get_skill_activation_service(request)
    registry_service = _get_skill_registry_service(request)

    activation = await activation_service.find_activation_by_skill_name(
        db,
        sub_agent_id=sub_agent_id,
        skill_name=body.skill_name,
        scope=body.scope,
        user_id=user.id if body.scope == "personal" else None,
        group_id=int(resolved_group_id) if resolved_group_id else None,
    )
    if not activation:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{body.skill_name}' not found in {body.scope} scope on this agent.",
        )

    entry = await registry_service.get_by_id(db, str(activation["registry_id"]))
    if not entry:
        raise HTTPException(status_code=404, detail="Registry entry for this activation no longer exists")

    # Update the file in the registry files list
    current_files = list(entry.files or [])
    # Replace existing file or add new one
    file_found = False
    for i, f in enumerate(current_files):
        if f.path == body.file_path:
            current_files[i] = RegistrySkillFile(path=body.file_path, contents=body.content)
            file_found = True
            break
    if not file_found:
        current_files.append(RegistrySkillFile(path=body.file_path, contents=body.content))

    # Update registry
    try:
        await registry_service.update_skill(
            db=db,
            actor=user,
            skill_id=entry.id,
            files=current_files,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Self-update: refresh this agent's activation / docstore snapshot
    await activation_service.self_update(
        db=db,
        registry_id=entry.id,
        sub_agent_id=sub_agent_id,
        agent_name=resolved_name,
        actor=user,
    )

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        registry_id=entry.id,
        message=f"File '{body.file_path}' written to skill '{body.skill_name}' in registry.",
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
    """Delete a file from a skill in the registry.

    Removes a single file from an existing skill. Updates the registry entry
    and refreshes the calling agent's docstore snapshot.
    Cannot delete SKILL.md (use console_remove_skill to deactivate the skill instead).

    - **personal** scope: immediate effect.
    - **group** scope: requires 'write' or 'manager' group role.
    """
    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'personal' or 'group'")

    _validate_skill_name(body.skill_name)
    _validate_file_path(body.file_path)

    # Resolve sub_agent_id
    sub_agent_id, resolved_name = await _resolve_sub_agent_id(request, db, agent_name, body.sub_agent_id)

    # Resolve group context
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

    # Find registry entry via activation
    activation_service = _get_skill_activation_service(request)
    registry_service = _get_skill_registry_service(request)

    activation = await activation_service.find_activation_by_skill_name(
        db,
        sub_agent_id=sub_agent_id,
        skill_name=body.skill_name,
        scope=body.scope,
        user_id=user.id if body.scope == "personal" else None,
        group_id=int(resolved_group_id) if resolved_group_id else None,
    )
    if not activation:
        raise HTTPException(
            status_code=404,
            detail=f"Skill '{body.skill_name}' not found in {body.scope} scope on this agent.",
        )

    entry = await registry_service.get_by_id(db, str(activation["registry_id"]))
    if not entry:
        raise HTTPException(status_code=404, detail="Registry entry for this activation no longer exists")

    # Remove the file from the registry files list
    current_files = list(entry.files or [])
    new_files = [f for f in current_files if f.path != body.file_path]
    if len(new_files) == len(current_files):
        raise HTTPException(
            status_code=404,
            detail=f"File '{body.file_path}' not found in skill '{body.skill_name}'",
        )

    # Update registry
    try:
        await registry_service.update_skill(
            db=db,
            actor=user,
            skill_id=entry.id,
            files=new_files,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Self-update: refresh this agent's activation / docstore snapshot
    await activation_service.self_update(
        db=db,
        registry_id=entry.id,
        sub_agent_id=sub_agent_id,
        agent_name=resolved_name,
        actor=user,
    )

    return McpSkillResponse(
        skill_name=body.skill_name,
        scope=body.scope,
        agent_name=agent_name,
        registry_id=entry.id,
        message=f"File '{body.file_path}' removed from skill '{body.skill_name}' in registry.",
    )
