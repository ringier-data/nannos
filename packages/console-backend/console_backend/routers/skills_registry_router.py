"""Skills Registry API endpoints.

Provides search and browse endpoints for discovering skills from external
registries (skills.sh, GitHub) before importing them.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from console_backend.dependencies import require_auth
from console_backend.models.skills_registry import (
    SkillAuditResponse,
    SkillDetailResponse,
    SkillSearchResponse,
)
from console_backend.models.user import User
from console_backend.services.skills_registry_service import skills_registry_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/skills/registry", tags=["skills-registry"])


@router.get("/search", response_model=SkillSearchResponse)
async def search_skills(
    q: str = Query(..., min_length=2, max_length=200, description="Search query"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results to return"),
    user: User = Depends(require_auth),
) -> SkillSearchResponse:
    """Search for skills on skills.sh.

    Uses semantic search for multi-word queries, fuzzy match for single-word.
    """
    results, search_type = await skills_registry_service.search_skills(query=q, limit=limit)
    return SkillSearchResponse(data=results, query=q, search_type=search_type, count=len(results))


@router.get("/browse", response_model=SkillSearchResponse)
async def browse_repo(
    repo: str = Query(..., min_length=3, max_length=200, description="GitHub repo (owner/repo)"),
    ref: str = Query(default="main", max_length=100, description="Git ref (branch/tag/SHA)"),
    user: User = Depends(require_auth),
) -> SkillSearchResponse:
    """Browse skills available in a GitHub repository.

    Scans the repo's tree for SKILL.md files and returns available skills.
    Uses authenticated GitHub API if GITHUB_TOKEN is configured.
    """
    # Basic validation: must contain exactly one slash
    parts = repo.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo must be in 'owner/repo' format",
        )

    results = await skills_registry_service.browse_repo(repo=repo, ref=ref)
    return SkillSearchResponse(data=results, query=repo, count=len(results))


@router.get("/detail/{skill_id:path}", response_model=SkillDetailResponse)
async def get_skill_detail(
    skill_id: str,
    user: User = Depends(require_auth),
) -> SkillDetailResponse:
    """Get full skill details from skills.sh (files, hash, audit info).

    skill_id is the full path identifier like 'owner/repo/skill-name'.
    """
    detail = await skills_registry_service.get_skill_detail(skill_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{skill_id}' not found on skills.sh",
        )
    return detail


@router.get("/audit/{skill_id:path}", response_model=SkillAuditResponse)
async def get_skill_audit(
    skill_id: str,
    user: User = Depends(require_auth),
) -> SkillAuditResponse:
    """Get security audit for a skill from skills.sh.

    Returns audit assessments from security partners (Gen Agent Trust Hub, Socket, Snyk, Runlayer, ZeroLeaks).
    Returns 404 if no partner has audited this skill yet.
    """
    audit = await skills_registry_service.get_skill_audit(skill_id)
    if audit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No audit found for skill '{skill_id}'",
        )
    return audit
