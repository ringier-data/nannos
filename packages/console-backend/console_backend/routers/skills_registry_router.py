"""Skills Registry API endpoints.

Provides endpoints for:
- Internal registry search (platform catalog)
- External source search (skills.sh, GitHub browse)
- Import from source into registry
- Activate from registry to agent filesystem
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.db.session import get_db_session
from console_backend.dependencies import require_auth, require_auth_or_bearer_token
from console_backend.models.skills_registry import (
    SkillFile,
    SkillImportRequest,
    SkillImportResponse,
    SkillSearchResponse,
    SkillSourceInfo,
)
from console_backend.models.user import User
from console_backend.services.skill_registry_service import SkillRegistryEntry, SkillRegistryService
from console_backend.services.skills_registry_service import skills_registry_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/skills/registry", tags=["skills-registry"])


def get_skill_registry_service(request: Request) -> SkillRegistryService:
    return request.app.state.skill_registry_service


async def _check_sub_agent_skill_access(
    entry: SkillRegistryEntry, user: User, db: AsyncSession, request: Request
) -> None:
    """For sub-agent-scoped skills, verify the user can access the parent agent.

    Raises 404 if the parent agent doesn't exist or the user has no access.
    Standalone skills are not checked here (they use visibility scoping).
    """
    if entry.scope != "sub-agent" or not entry.sub_agent_id:
        return

    from sqlalchemy import text as sa_text

    # Check if user is owner, has group access, or agent is public
    result = await db.execute(
        sa_text("""
            SELECT sa.id FROM sub_agents sa
            WHERE sa.id = :agent_id AND sa.deleted_at IS NULL
              AND (
                  sa.owner_user_id = :user_id
                  OR sa.is_public = TRUE
                  OR EXISTS (
                      SELECT 1 FROM sub_agent_permissions sap
                      JOIN user_group_members ugm ON sap.user_group_id = ugm.user_group_id
                      WHERE sap.sub_agent_id = sa.id AND ugm.user_id = :user_id
                  )
              )
        """),
        {"agent_id": entry.sub_agent_id, "user_id": user.id},
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{entry.id}' not found in registry",
        )


@router.get("/search", response_model=SkillSearchResponse)
async def search_skills(
    request: Request,
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    source: str = Query(default="registry", description="'registry' (internal) or 'external' (skills.sh)"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results to return"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
) -> SkillSearchResponse:
    """Search for skills.

    source='registry': Searches the internal platform catalog (default).
    source='external': Searches skills.sh for community skills.
    """
    if source == "external":
        results, search_type = await skills_registry_service.search_external(query=q, limit=limit)
        return SkillSearchResponse(
            data=results,
            query=q,
            search_type=search_type,
            count=len(results),
            total=len(results),
            offset=0,
            has_more=False,
        )

    # Internal registry search
    skill_registry_service = get_skill_registry_service(request)
    entries, total = await skill_registry_service.search(
        db=db,
        query=q,
        limit=limit,
        offset=offset,
        owner_id=user.id,
    )
    from console_backend.models.skills_registry import SkillSearchResult

    results = [
        SkillSearchResult(
            id=str(e.id),
            slug=e.slug,
            name=e.name,
            description=e.description,
            source=e.source_repo or "platform",
            installs=0,
            source_type=e.source_type,
            author=e.author_name,
            visibility=e.visibility,
            url=None,
        )
        for e in entries
    ]
    return SkillSearchResponse(
        data=results,
        query=q,
        search_type="fulltext",
        count=len(results),
        total=total,
        offset=offset,
        has_more=(offset + len(results)) < total,
    )


@router.get("/browse", response_model=SkillSearchResponse)
async def browse_repo(
    repo: str = Query(..., min_length=3, max_length=200, description="Git repo (owner/repo)"),
    ref: str = Query(default="main", max_length=100, description="Git ref (branch/tag/SHA)"),
    user: User = Depends(require_auth),
) -> SkillSearchResponse:
    """Browse skills available in a Git repository.

    Scans the repo's tree for SKILL.md files and returns available skills.
    Works with any public GitHub repo. Uses authenticated API if GITHUB_TOKEN is configured.
    """
    parts = repo.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo must be in 'owner/repo' format",
        )

    results = await skills_registry_service.browse_repo(repo=repo, ref=ref)
    return SkillSearchResponse(data=results, query=repo, count=len(results))


@router.get("/detail/{skill_id}")
async def get_skill_detail(
    request: Request,
    skill_id: str,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Get a skill's detail from the internal registry by ID or slug."""
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id_or_slug(db, skill_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{skill_id}' not found in registry",
        )
    # Sub-agent-scoped skills: verify user can access the parent agent
    await _check_sub_agent_skill_access(entry, user, db, request)

    return {
        "id": entry.id,
        "name": entry.name,
        "slug": entry.slug,
        "description": entry.description,
        "source_type": entry.source_type,
        "source_repo": entry.source_repo,
        "source_ref": entry.source_ref,
        "visibility": entry.visibility,
        "scope": entry.scope,
        "sub_agent_id": entry.sub_agent_id,
        "sandbox_required": entry.sandbox_required,
        "content_hash": entry.content_hash,
        "security_verdict": entry.security_verdict,
        "files": [{"path": f.path, "contents": f.contents} for f in entry.files],
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


@router.get("/detail/{skill_id}/versions")
async def get_skill_versions(
    request: Request,
    skill_id: str,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Get the version history for a skill."""
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _check_sub_agent_skill_access(entry, user, db, request)

    versions = await skill_registry_service.get_version_history(db, skill_id)
    return {
        "skill_id": skill_id,
        "current_hash": entry.content_hash,
        "versions": [
            {
                "content_hash": v["content_hash"],
                "description": v["description"],
                "created_by": v["created_by"],
                "created_at": v["created_at"].isoformat() if v["created_at"] else None,
            }
            for v in versions
        ],
    }


@router.get("/detail/{skill_id}/versions/{content_hash}")
async def get_skill_version_detail(
    request: Request,
    skill_id: str,
    content_hash: str,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Get a specific version's files by content_hash."""
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _check_sub_agent_skill_access(entry, user, db, request)

    version = await skill_registry_service.get_version(db, skill_id, content_hash)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")

    return {
        "skill_id": skill_id,
        "files": version["files"],
        "content_hash": version["content_hash"],
        "description": version["description"],
        "created_by": version["created_by"],
        "created_at": version["created_at"].isoformat() if version["created_at"] else None,
    }


@router.post("/import", response_model=SkillImportResponse, status_code=status.HTTP_201_CREATED)
async def import_skill(
    body: SkillImportRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
) -> SkillImportResponse:
    """Import a skill from a Git repository into the registry and activate it.

    Two-phase flow:
    1. Fetch files from source (Git or skills.sh resolution)
    2. Add to the skill registry (platform catalog)
    3. Activate to agent filesystem (docstore)

    Returns 409 if the skill already exists (unless overwrite=True).
    Returns 404 if the skill cannot be found at the source.
    Returns 403 if security assessment is 'unsafe' (unless force=True with approver role).
    """
    from console_backend.routers.playbook_router import get_playbook_service
    from console_backend.services.skill_security_service import skill_security_service

    playbook_service = get_playbook_service(request)

    # Step 1: Resolve Git coordinates
    repo: str
    skill_name: str
    ref: str = body.ref
    registry_id: str | None = None

    if body.repo:
        repo = body.repo
        skill_name = body.skill or body.repo.split("/")[-1]
    elif body.registry_id:
        try:
            repo, skill_name = skills_registry_service.resolve_registry_id(body.registry_id)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        registry_id = body.registry_id
        if body.skill:
            skill_name = body.skill
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide 'repo' (Git source) or 'registry_id' (registry resolution)",
        )

    # Step 2: Fetch files from Git
    git_detail = await skills_registry_service.fetch_skill_files_from_github(repo=repo, skill_name=skill_name, ref=ref)
    if git_detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{skill_name}' not found in repo '{repo}' (ref: {ref})",
        )

    # Extract SKILL.md and bundled files
    skill_content: str | None = None
    bundled_files: list[SkillFile] = []
    for f in git_detail.files:
        if f.path == "SKILL.md":
            skill_content = f.contents
        else:
            bundled_files.append(f)

    if not skill_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Skill '{skill_name}' in '{repo}' has no SKILL.md file",
        )

    # Step 3: Security assessment
    user_access_token = getattr(request.state, "access_token", None)
    security_verdict = await skill_security_service.assess_skill(
        files=git_detail.files,
        registry_audit=None,
        db=db,
        user_access_token=user_access_token,
    )

    if security_verdict.verdict == "unsafe" and not body.force:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": f"Skill '{skill_name}' failed security assessment.",
                "verdict": security_verdict.verdict,
                "reasoning": security_verdict.reasoning,
                "indicators": [ind.model_dump() for ind in security_verdict.indicators],
                "hint": "Set force=true with approver role to override.",
            },
        )

    # Step 4: Import into registry (catalog)
    from console_backend.services.skill_sources.base import SkillSourceDetail

    # Map scope to registry visibility
    visibility = body.scope if body.scope in ("private", "public") else "public"

    source_detail = SkillSourceDetail(
        name=skill_name,
        slug=skill_name,
        description=_extract_description(skill_content) if skill_content else "",
        files=git_detail.files,
        source_repo=repo,
        source_ref=ref,
        source_path=None,
        tree_sha=git_detail.tree_sha,
    )
    skill_registry_service = get_skill_registry_service(request)
    registry_entry = await skill_registry_service.import_from_source(
        db=db,
        actor=user,
        detail=source_detail,
        source_type="github",
        visibility=visibility,
    )

    # Steps 5-6: Activation (only if target agent is specified)
    if body.agent:
        # Step 5: Check for existing skill in filesystem (conflict detection)
        if not body.overwrite:
            existing = await playbook_service.get_skill(
                user_id=user.id,
                agent_name=body.agent,
                skill_name=skill_name,
                scope=body.scope,
                group_id=body.group_id,
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Skill '{skill_name}' already exists for agent '{body.agent}'. Set overwrite=true to replace.",
                )

        # Step 6: Activate to agent filesystem (docstore)
        files_data = (
            [{"path": f.path, "content": f.contents, "encoding": f.encoding} for f in bundled_files]
            if bundled_files
            else None
        )

        try:
            await playbook_service.put_skill_with_files(
                user_id=user.id,
                agent_name=body.agent,
                skill_name=skill_name,
                scope=body.scope,
                content=skill_content,
                files=files_data,
                group_id=body.group_id,
                replace_files=body.overwrite,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    source_info = SkillSourceInfo(
        type="registry" if registry_id else "git",
        repo=repo,
        skill=skill_name,
        ref=ref,
        hash=git_detail.tree_sha,
        registry_id=registry_id,
        imported_at=datetime.now(timezone.utc).isoformat(),
    )

    logger.info(
        "Imported skill '%s' from %s/%s (ref=%s, scope=%s, user=%s, verdict=%s)",
        skill_name,
        repo,
        skill_name,
        ref,
        body.scope,
        user.id,
        security_verdict.verdict,
    )

    return SkillImportResponse(
        skill_name=skill_name,
        agent=body.agent,
        scope=body.scope,
        source=source_info,
        files_count=1 + len(bundled_files),
        overwritten=body.overwrite,
        security=security_verdict,
    )


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_skill(
    request: Request,
    skill_id: str,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Remove a skill from the registry."""
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _check_sub_agent_skill_access(entry, user, db, request)
    await skill_registry_service.remove(db, user, skill_id)
    await db.commit()


# ─── Registry Authoring REST Endpoints ────────────────────────────────────────


class RegistryCreateRequest(BaseModel):
    """Request to create a new skill in the registry."""

    name: str = Field(description="Display name for the skill")
    slug: str | None = Field(default=None, description="URL-safe identifier. Auto-derived from name if omitted.")
    description: str = Field(default="", description="What the skill does")
    files: list[SkillFile] = Field(
        default_factory=list, description="Skill files. Defaults to a stub SKILL.md if empty."
    )
    visibility: str = Field(default="private", description="'private' or 'public'")


class RegistryUpdateRequest(BaseModel):
    """Request to update a skill in the registry."""

    name: str | None = Field(default=None, description="Updated name")
    description: str | None = Field(default=None, description="Updated description")
    files: list[SkillFile] | None = Field(default=None, description="Full replacement file list")
    sandbox_required: bool | None = Field(default=None, description="Whether skill requires sandbox execution")
    visibility: str | None = Field(default=None, description="Visibility: private or public")


class RegistryFileWriteRequest(BaseModel):
    """Request to write a single file in a registry skill."""

    content: str = Field(description="File content (text)")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_registry_skill(
    body: RegistryCreateRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Create a new skill in the registry.

    Creates a skill entry with the provided files and visibility settings.
    The skill can then be activated on agents.
    """
    if body.visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="visibility must be 'private' or 'public'")

    skill_registry_service = get_skill_registry_service(request)
    try:
        entry = await skill_registry_service.create_skill(
            db=db,
            actor=user,
            name=body.name,
            description=body.description,
            files=body.files or [SkillFile(path="SKILL.md", contents=f"# {body.name}\n\n{body.description}")],
            visibility=body.visibility,
            slug=body.slug if body.slug else None,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()

    return {
        "id": entry.id,
        "slug": entry.slug,
        "name": entry.name,
        "content_hash": entry.content_hash,
        "visibility": entry.visibility,
    }


@router.put("/{skill_id}")
async def update_registry_skill(
    skill_id: str,
    body: RegistryUpdateRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Update a skill in the registry.

    Updates description and/or file contents. If files are provided, they replace
    all existing files. Content hash is recomputed automatically.
    Only locally-authored skills (source_type='nannos') can be edited.
    """
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _check_sub_agent_skill_access(entry, user, db, request)
    if entry.source_type != "nannos":
        # Imported skills: only sandbox_required and visibility can be changed
        if body.name is not None or body.description is not None or body.files is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Imported skills are read-only. Only sandbox mode and visibility can be changed.",
            )
        if body.sandbox_required is None and body.visibility is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No updatable fields provided for imported skill.",
            )

    if body.visibility is not None and body.visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="visibility must be 'private' or 'public'")

    try:
        updated = await skill_registry_service.update_skill(
            db=db,
            actor=user,
            skill_id=skill_id,
            name=body.name,
            files=body.files,
            description=body.description,
            sandbox_required=body.sandbox_required,
            visibility=body.visibility,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()

    return {
        "id": updated.id,
        "slug": updated.slug,
        "content_hash": updated.content_hash,
    }


@router.put("/{skill_id}/files/{file_path:path}")
async def write_registry_file(
    skill_id: str,
    file_path: str,
    body: RegistryFileWriteRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Write a single file within a registry skill.

    Creates or replaces a file at the given path. Content hash is recomputed.
    Only locally-authored skills (source_type='nannos') can be edited.
    """
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _check_sub_agent_skill_access(entry, user, db, request)
    if entry.source_type != "nannos":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Imported skills are read-only. Use the copy endpoint to create an editable copy.",
        )

    # Update the file in the files list
    current_files = list(entry.files)
    file_found = False
    for i, f in enumerate(current_files):
        if f.path == file_path:
            current_files[i] = SkillFile(path=file_path, contents=body.content)
            file_found = True
            break
    if not file_found:
        current_files.append(SkillFile(path=file_path, contents=body.content))

    try:
        updated = await skill_registry_service.update_skill(
            db=db,
            actor=user,
            skill_id=skill_id,
            files=current_files,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()

    return {"id": updated.id, "file_path": file_path, "content_hash": updated.content_hash}


@router.delete("/{skill_id}/files/{file_path:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_registry_file(
    skill_id: str,
    file_path: str,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Delete a single file from a registry skill.

    Removes the file from the files list. Cannot delete SKILL.md.
    Content hash is recomputed.
    Only locally-authored skills (source_type='nannos') can be edited.
    """
    if file_path == "SKILL.md":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete SKILL.md. Delete the entire skill instead.",
        )

    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _check_sub_agent_skill_access(entry, user, db, request)
    if entry.source_type != "nannos":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Imported skills are read-only. Use the copy endpoint to create an editable copy.",
        )

    current_files = list(entry.files)
    new_files = [f for f in current_files if f.path != file_path]
    if len(new_files) == len(current_files):
        raise HTTPException(status_code=404, detail=f"File '{file_path}' not found in skill")

    try:
        await skill_registry_service.update_skill(
            db=db,
            actor=user,
            skill_id=skill_id,
            files=new_files,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()


class CopyRequest(BaseModel):
    """Request to copy a skill (creating an editable local version)."""

    name: str | None = Field(default=None, description="Name for the copy (defaults to original + ' (copy)')")
    slug: str | None = Field(default=None, description="Slug for the copy. Auto-derived from name if omitted.")


@router.post("/{skill_id}/copy", status_code=status.HTTP_201_CREATED)
async def copy_skill(
    skill_id: str,
    body: CopyRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Create an editable copy of a skill.

    Copies all files from the source skill into a new locally-authored (source_type='nannos')
    registry entry that can be freely edited.
    """
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    copy_name = body.name or f"{entry.name} (copy)"

    try:
        new_entry = await skill_registry_service.create_skill(
            db=db,
            actor=user,
            name=copy_name,
            slug=body.slug if body.slug else None,
            description=entry.description or "",
            files=list(entry.files),
            visibility="private",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()
    return {
        "id": new_entry.id,
        "name": new_entry.name,
        "slug": new_entry.slug,
        "content_hash": new_entry.content_hash,
    }


@router.post("/{skill_id}/check-update")
async def check_skill_update(
    skill_id: str,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Check if an imported skill has upstream updates.

    Fetches the latest version from the source repository and compares
    content hashes. If different, returns file-level diffs.
    """
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if entry.source_type == "nannos":
        raise HTTPException(
            status_code=400,
            detail="Only imported skills can be checked for upstream updates.",
        )
    if not entry.source_repo:
        raise HTTPException(
            status_code=400,
            detail="Skill has no source repository configured.",
        )

    # Reconstruct the source_id for GitHubSource.fetch_skill
    source_path = entry.source_path
    source_ref = entry.source_ref or "main"
    repo = entry.source_repo
    # Determine skill name from source_path or slug
    if source_path:
        # source_path is like "skills/my-skill" — extract last component
        skill_name = source_path.rstrip("/").rsplit("/", 1)[-1]
        source_id = f"{repo}/{skill_name}@{source_ref}"
    else:
        source_id = f"{repo}/{entry.slug}@{source_ref}"

    # Fast path: compare tree SHA first (avoids fetching all file contents)
    stored_tree_sha = entry.metadata.get("tree_sha") if entry.metadata else None
    if stored_tree_sha:
        try:
            latest_tree_sha = await skills_registry_service.github_source.get_tree_sha(source_id)
        except Exception as e:
            logger.warning("Failed to fetch tree SHA for skill %s: %s", skill_id, e)
            latest_tree_sha = None

        if latest_tree_sha and latest_tree_sha == stored_tree_sha:
            return {"update_available": False}

    # Tree SHA differs or unavailable — do full file fetch for detailed diff
    try:
        latest = await skills_registry_service.github_source.fetch_skill(source_id)
    except Exception as e:
        logger.warning("Failed to fetch upstream for skill %s: %s", skill_id, e)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch from source repository: {e}",
        )

    if latest is None:
        raise HTTPException(
            status_code=404,
            detail="Skill no longer found in the source repository.",
        )

    # Compute hash of latest files
    from console_backend.services.skill_registry_service import _compute_content_hash

    latest_hash = _compute_content_hash(latest.files)
    if latest_hash == entry.content_hash:
        return {"update_available": False}

    # Build file-level diff
    current_files_map = {f.path: f.contents for f in entry.files}
    latest_files_map = {f.path: f.contents for f in latest.files}
    all_paths = sorted(set(list(current_files_map.keys()) + list(latest_files_map.keys())))

    file_diffs = []
    for path in all_paths:
        current_content = current_files_map.get(path)
        latest_content = latest_files_map.get(path)
        if current_content == latest_content:
            continue
        file_diffs.append(
            {
                "path": path,
                "current": current_content,
                "latest": latest_content,
                "status": "added" if current_content is None else "removed" if latest_content is None else "modified",
            }
        )

    return {
        "update_available": True,
        "current_hash": entry.content_hash,
        "latest_hash": latest_hash,
        "latest_tree_sha": latest.tree_sha,
        "files": file_diffs,
    }


class ApplyUpdateRequest(BaseModel):
    """Request to apply an upstream update to an imported skill."""

    latest_hash: str = Field(description="Expected latest hash (from check-update response)")


@router.post("/{skill_id}/apply-update")
async def apply_skill_update(
    skill_id: str,
    body: ApplyUpdateRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Apply upstream updates to an imported skill.

    Re-fetches files from the source repository and replaces the current files.
    """
    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if entry.source_type == "nannos":
        raise HTTPException(status_code=400, detail="Only imported skills can be updated from source.")
    if not entry.source_repo:
        raise HTTPException(status_code=400, detail="Skill has no source repository configured.")

    # Re-fetch from source
    source_path = entry.source_path
    source_ref = entry.source_ref or "main"
    repo = entry.source_repo
    if source_path:
        skill_name = source_path.rstrip("/").rsplit("/", 1)[-1]
        source_id = f"{repo}/{skill_name}@{source_ref}"
    else:
        source_id = f"{repo}/{entry.slug}@{source_ref}"

    try:
        latest = await skills_registry_service.github_source.fetch_skill(source_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch from source: {e}")
    if latest is None:
        raise HTTPException(status_code=404, detail="Skill no longer found in source repository.")

    from console_backend.services.skill_registry_service import _compute_content_hash

    latest_hash = _compute_content_hash(latest.files)
    if latest_hash != body.latest_hash:
        raise HTTPException(
            status_code=409,
            detail="Source has changed since you checked. Please re-check for updates.",
        )

    # Apply the update
    try:
        updated = await skill_registry_service.update_skill(
            db=db,
            actor=user,
            skill_id=skill_id,
            files=latest.files,
            description=latest.description or entry.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Update metadata with new tree_sha
    from sqlalchemy import text as sql_text

    if latest.tree_sha:
        await db.execute(
            sql_text(
                "UPDATE skill_registry SET metadata = jsonb_set(COALESCE(metadata, '{}'), '{tree_sha}', :sha) WHERE id = :id"
            ),
            {"sha": f'"{latest.tree_sha}"', "id": skill_id},
        )

    await db.commit()

    return {
        "id": updated.id,
        "content_hash": updated.content_hash,
        "files_updated": len(latest.files),
    }


class McpActivateSkillInput(BaseModel):
    """Input for console_activate_skill MCP tool."""

    agent_name: str | None = Field(
        default=None,
        description="Name of the sub-agent. Auto-injected when called by a sub-agent — omit unless targeting a different agent.",
    )
    registry_id: str | None = Field(default=None, description="Registry entry UUID to activate")
    skill_name: str | None = Field(
        default=None, description="Skill slug to search in registry (alternative to registry_id)"
    )
    scope: str = Field(
        default="personal",
        description=(
            "Activation scope: 'personal' (only you), 'group' (shared with group members), "
            "or 'default' (baked into sub-agent config, visible to all users — requires owner/write access)"
        ),
    )
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    sub_agent_id: int | None = Field(
        default=None, description="Sub-agent ID (resolved from agent_name if not provided)"
    )


class McpActivateSkillResponse(BaseModel):
    """Response for console_activate_skill."""

    skill_name: str
    agent_name: str
    scope: str
    registry_id: str
    message: str


@router.post(
    "/mcp/activate",
    response_model=McpActivateSkillResponse,
    tags=["MCP"],
    operation_id="console_activate_skill",
    status_code=status.HTTP_201_CREATED,
)
async def mcp_activate_skill(
    body: McpActivateSkillInput,
    request: Request,
    user: User = Depends(require_auth_or_bearer_token),
    db: AsyncSession = Depends(get_db_session),
) -> McpActivateSkillResponse:
    """Activate an existing registry skill on the calling agent.

    Use this to adopt a skill created by someone else, or to re-activate
    a previously deactivated skill. The skill must exist in the registry.

    Provide either registry_id (exact) or skill_name (searches by slug).
    """
    from console_backend.routers.playbook_router import (
        _get_skill_activation_service,
        _get_sub_agent_service,
        _get_user_group_context,
        _require_agent_name,
        _resolve_group,
        _resolve_sub_agent_id,
    )

    agent_name = _require_agent_name(body.agent_name)

    if body.scope not in ("personal", "group", "default"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid scope '{body.scope}'. Must be 'personal', 'group', "
                "or 'default' (bakes skill into sub-agent config for all users)."
            ),
        )

    if not body.registry_id and not body.skill_name:
        raise HTTPException(status_code=400, detail="Either registry_id or skill_name must be provided")

    # Resolve sub_agent_id
    sub_agent_id, resolved_name = await _resolve_sub_agent_id(request, db, agent_name, body.sub_agent_id)

    # Scope-specific authorization
    if body.scope == "default":
        # Default scope bakes the skill into the sub-agent config — requires write access
        sub_agent_service = _get_sub_agent_service(request)
        has_write = await sub_agent_service.check_user_permission(
            db=db, sub_agent_id=sub_agent_id, user_id=user.id, required_permission="write"
        )
        if not has_write:
            raise HTTPException(
                status_code=403,
                detail=(
                    "[ERROR_TYPE: auth] You need owner or write access on the sub-agent to activate default-scope skills. "
                    "You can still activate the skill with 'personal' scope for your own use."
                ),
            )
        resolved_group_id = None
        group_role = None
    else:
        # Resolve group context for personal/group scopes
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
                    detail="[ERROR_TYPE: auth] You need at least 'write' group role to activate group skills.",
                )

    # Find registry entry
    skill_registry_service = get_skill_registry_service(request)
    if body.registry_id:
        entry = await skill_registry_service.get_by_id(db, body.registry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Registry entry '{body.registry_id}' not found")
    else:
        entry = await skill_registry_service.get_by_slug(db, body.skill_name)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Skill '{body.skill_name}' not found in registry")

    # Activate
    activation_service = _get_skill_activation_service(request)

    # Sub-agent-scoped skills can only be activated with 'default' scope
    # (they're embedded in a sub-agent's config and need owner access).
    if entry.scope == "sub-agent" and body.scope != "default":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Skill '{entry.slug}' is scoped to a sub-agent. Use scope='default' "
                "to activate it on your agent, or use 'console_create_skill' to create a new copy."
            ),
        )

    if entry.scope == "sub-agent" and sub_agent_id != entry.sub_agent_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Skill '{entry.slug}' is scoped to a specific sub-agent and cannot be activated on a different agent. "
                "Use 'console_create_skill' to create a new copy that can be activated on your agent, or use "
                "'console_update_skill' to change the scope to 'standalone' to make it agent-agnostic."
            ),
        )

    try:
        if body.scope == "default":
            await activation_service.activate_as_default(
                db=db,
                registry_id=entry.id,
                sub_agent_id=sub_agent_id,
                agent_name=resolved_name,
                actor=user,
            )
        else:
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

    return McpActivateSkillResponse(
        skill_name=entry.slug,
        agent_name=agent_name,
        scope=body.scope,
        registry_id=entry.id,
        message=f"Skill '{entry.slug}' activated on agent '{agent_name}' ({body.scope} scope).",
    )


class ActivateRequest(BaseModel):
    """Request to activate a registry skill for an agent."""

    agent: str = Field(description="Target sub-agent name")
    scope: str = Field(default="personal", description="'personal' or 'group'")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    overwrite: bool = Field(default=False, description="Overwrite if already activated")


@router.post("/{skill_id}/activate", status_code=status.HTTP_201_CREATED)
async def activate_skill(
    skill_id: str,
    body: ActivateRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Activate a registry skill for an agent/scope (copy to filesystem).

    The skill must exist in the registry. Files are copied to the docstore
    for the specified agent and scope.
    """
    from console_backend.routers.playbook_router import get_playbook_service

    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found in registry")

    playbook_service = get_playbook_service(request)

    # Check for existing activation
    if not body.overwrite:
        existing = await playbook_service.get_skill(
            user_id=user.id,
            agent_name=body.agent,
            skill_name=entry.slug,
            scope=body.scope,
            group_id=body.group_id,
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Skill '{entry.slug}' already activated for agent '{body.agent}'. Set overwrite=true to replace.",
            )

    # Extract SKILL.md and bundled files
    skill_content: str | None = None
    bundled_files: list[dict[str, str | None]] = []
    for f in entry.files:
        if f.path == "SKILL.md":
            skill_content = f.contents
        else:
            bundled_files.append({"path": f.path, "content": f.contents, "encoding": f.encoding})

    if not skill_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Registry skill '{entry.slug}' has no SKILL.md file",
        )

    try:
        await playbook_service.put_skill_with_files(
            user_id=user.id,
            agent_name=body.agent,
            skill_name=entry.slug,
            scope=body.scope,
            content=skill_content,
            files=bundled_files or None,
            group_id=body.group_id,
            replace_files=body.overwrite,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {"skill": entry.slug, "agent": body.agent, "scope": body.scope, "activated": True}


class VisibilityUpdate(BaseModel):
    """Request to change a skill's visibility."""

    visibility: str = Field(description="'private' or 'public'")


@router.patch("/{skill_id}/visibility")
async def update_visibility(
    request: Request,
    skill_id: str,
    body: VisibilityUpdate,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db_session),
):
    """Change a skill's visibility (promote/demote)."""
    if body.visibility not in ("private", "public"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="visibility must be 'private' or 'public'")

    skill_registry_service = get_skill_registry_service(request)
    entry = await skill_registry_service.get_by_id(db, skill_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    try:
        await skill_registry_service.update_visibility(
            db=db,
            actor=user,
            skill_id=skill_id,
            visibility=body.visibility,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await db.commit()
    return {"id": skill_id, "visibility": body.visibility}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _extract_description(content: str) -> str:
    """Extract description from SKILL.md frontmatter."""
    if not content.startswith("---"):
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:200]
        return ""
    end_idx = content.find("---", 3)
    if end_idx == -1:
        return ""
    frontmatter = content[3:end_idx]
    for line in frontmatter.split("\n"):
        line = line.strip()
        if line.startswith("description:"):
            desc = line[len("description:") :].strip()
            if (desc.startswith('"') and desc.endswith('"')) or (desc.startswith("'") and desc.endswith("'")):
                desc = desc[1:-1]
            return desc
    return ""


# ─── MCP Tool Endpoints ──────────────────────────────────────────────────────
# These endpoints are auto-discovered by FastApiMCP via the "MCP" tag.
# They provide skill search/import capabilities to orchestrator and sub-agents.


class McpSearchSkillsInput(BaseModel):
    """Input for console_search_skills MCP tool."""

    query: str = Field(description="Search query (keywords, skill name, or description fragment)")
    source: str = Field(
        default="registry",
        description=(
            "Where to search: 'registry' (platform catalog, fast), "
            "'external' (skills.sh community index), "
            "or 'repo:owner/name' (browse a specific GitHub repository for skills)"
        ),
    )
    limit: int = Field(default=10, description="Maximum number of results (1-50)", ge=1, le=50)


class McpSearchSkillResult(BaseModel):
    """A single skill search result."""

    id: str = Field(description="Skill identifier (use this for import)")
    name: str = Field(description="Human-readable skill name")
    description: str | None = Field(default=None, description="Brief description of what the skill does")
    source: str = Field(description="Source repository or provider")


class McpSearchSkillsResponse(BaseModel):
    """Response for console_search_skills."""

    results: list[McpSearchSkillResult] = Field(default_factory=list)
    count: int = Field(default=0)
    source: str = Field(description="Where results came from")


@router.post(
    "/mcp/search",
    response_model=McpSearchSkillsResponse,
    tags=["MCP"],
    operation_id="console_search_skills",
)
async def mcp_search_skills(
    body: McpSearchSkillsInput,
    request: Request,
    user: User = Depends(require_auth_or_bearer_token),
    db: AsyncSession = Depends(get_db_session),
) -> McpSearchSkillsResponse:
    """Search for skills in the platform registry, community index, or a GitHub repo.

    Use source='registry' (default) to search the platform's internal catalog.
    Use source='external' to discover community skills from skills.sh.
    Use source='repo:owner/name' to browse a specific GitHub repository for SKILL.md directories.

    Results include an 'id' field that can be passed to console_import_skill.
    """
    results: list[McpSearchSkillResult] = []

    if body.source == "registry":
        skill_registry_service = get_skill_registry_service(request)
        entries, _total = await skill_registry_service.search(db, body.query, limit=body.limit, owner_id=user.id)
        for entry in entries:
            results.append(
                McpSearchSkillResult(
                    id=entry.id,
                    name=entry.name,
                    description=entry.description,
                    source=entry.source_repo or "nannos",
                )
            )
    elif body.source == "external":
        external_results, _ = await skills_registry_service.search_external(body.query, body.limit)
        for r in external_results:
            results.append(McpSearchSkillResult(id=r.id, name=r.name, description=None, source=r.source))
    elif body.source.startswith("repo:"):
        repo = body.source[5:]
        browse_results = await skills_registry_service.browse_repo(repo, ref="main")
        for r in browse_results[: body.limit]:
            results.append(McpSearchSkillResult(id=r.id, name=r.name, description=None, source=r.source))
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source must be 'registry', 'external', or 'repo:owner/name'",
        )

    return McpSearchSkillsResponse(results=results, count=len(results), source=body.source)


class McpImportSkillInput(BaseModel):
    """Input for console_import_skill MCP tool."""

    repo: str = Field(description="Git repository in 'owner/repo' format (e.g. 'anthropics/skills')")
    skill: str = Field(
        description="Skill name/directory within the repo (e.g. 'next-js-development'). If the repo IS the skill, use the repo name."
    )
    agent_name: str = Field(description="Target sub-agent name to activate the skill for")
    scope: str = Field(default="personal", description="Scope: 'personal' (immediate, no approval) or 'group'")
    ref: str = Field(default="main", description="Git branch/tag/commit to fetch from")
    group_id: str | None = Field(default=None, description="Group ID (required when scope='group')")
    overwrite: bool = Field(default=False, description="Overwrite if skill already exists")
    force: bool = Field(default=False, description="Force import even if security assessment is 'unsafe'")


class McpImportSkillResponse(BaseModel):
    """Response for console_import_skill."""

    skill_name: str = Field(description="Imported skill name")
    agent: str = Field(description="Target sub-agent")
    scope: str = Field(description="Activation scope")
    security_verdict: str = Field(description="'safe', 'caution', or 'unsafe'")
    files_count: int = Field(description="Number of files imported")
    message: str = Field(description="Human-readable summary")


@router.post(
    "/mcp/import",
    response_model=McpImportSkillResponse,
    tags=["MCP"],
    operation_id="console_import_skill",
    status_code=status.HTTP_201_CREATED,
)
async def mcp_import_skill(
    body: McpImportSkillInput,
    request: Request,
    user: User = Depends(require_auth_or_bearer_token),
    db: AsyncSession = Depends(get_db_session),
) -> McpImportSkillResponse:
    """Import a skill from a GitHub repository and activate it for a sub-agent.

    This performs: Git fetch → security assessment → registry entry → filesystem activation.

    The skill is fetched from the specified repo+skill, assessed for security risks,
    added to the platform registry, and activated for the target agent/scope.

    Security verdicts:
    - 'safe': No issues detected, import proceeds normally
    - 'caution': Minor concerns detected, import proceeds with warnings
    - 'unsafe': High-risk patterns detected. Blocked unless force=true (requires approver role)
    """
    from console_backend.routers.playbook_router import get_playbook_service
    from console_backend.services.skill_security_service import skill_security_service
    from console_backend.services.skill_sources.base import SkillSourceDetail

    # Fetch skill files from GitHub
    source_detail = await skills_registry_service.fetch_skill_files_from_github(
        repo=body.repo, skill_name=body.skill, ref=body.ref
    )
    if source_detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{body.skill}' not found in repo '{body.repo}' (ref: {body.ref})",
        )

    files = source_detail.files

    # Security assessment
    user_access_token = getattr(request.state, "access_token", None)
    security_verdict = await skill_security_service.assess_skill(
        files,
        db=db,
        user_access_token=user_access_token,
    )

    if security_verdict.verdict == "unsafe" and not body.force:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Skill blocked: {security_verdict.reasoning}. Use force=true to override (requires approver role).",
        )

    # Find SKILL.md
    skill_md_content: str | None = None
    bundled_files: list[dict[str, str | None]] = []
    for f in files:
        if f.path == "SKILL.md":
            skill_md_content = f.contents
        else:
            bundled_files.append({"path": f.path, "content": f.contents, "encoding": f.encoding})

    if not skill_md_content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No SKILL.md found in '{body.skill}' from '{body.repo}'",
        )

    # Determine skill name from the path
    skill_name = body.skill.rstrip("/").rsplit("/", 1)[-1] if "/" in body.skill else body.skill

    # Import to registry
    detail_for_registry = SkillSourceDetail(
        name=skill_name.replace("-", " ").title(),
        slug=skill_name,
        description=_extract_description(skill_md_content),
        files=files,
        source_repo=body.repo,
        source_ref=body.ref,
        source_path=body.skill,
        tree_sha=source_detail.tree_sha,
    )
    skill_registry_service = get_skill_registry_service(request)
    await skill_registry_service.import_from_source(
        db=db,
        actor=user,
        detail=detail_for_registry,
        source_type="github",
        visibility="public",
    )
    await db.commit()

    # Activate to filesystem
    playbook_service = get_playbook_service(request)
    try:
        await playbook_service.put_skill_with_files(
            user_id=user.id,
            agent_name=body.agent_name,
            skill_name=skill_name,
            scope=body.scope,
            content=skill_md_content,
            files=bundled_files or None,
            group_id=body.group_id,
            replace_files=body.overwrite,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return McpImportSkillResponse(
        skill_name=skill_name,
        agent=body.agent_name,
        scope=body.scope,
        security_verdict=security_verdict.verdict,
        files_count=1 + len(bundled_files),
        message=f"Skill '{skill_name}' imported and activated for agent '{body.agent_name}' ({body.scope} scope). Security: {security_verdict.verdict}.",
    )
