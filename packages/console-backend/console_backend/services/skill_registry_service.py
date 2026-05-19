"""Skill Registry Service — CRUD operations over the skill_registry table.

This service owns:
- Adding skills to the registry (import from source)
- Searching the registry (full-text + visibility scoping)
- Getting/removing registry entries
- Updating visibility

It does NOT own:
- External source discovery (that's SkillSource)
- Activation to agent filesystem (that's PlaybookService / docstore)
- Security scanning (that's SkillSecurityService)
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.audit import AuditAction
from console_backend.models.skills_registry import SkillFile
from console_backend.models.user import User
from console_backend.repositories.skill_registry_repository import SkillRegistryRepository
from console_backend.services.skill_sources.base import SkillSourceDetail

logger = logging.getLogger(__name__)

# Registry scope: what the registry entry represents.
#   - standalone: agent-agnostic skill (can be activated on any agent)
#   - sub-agent: skill tied to a specific sub-agent's config
RegistryScope = Literal["standalone", "sub-agent"]


class SkillRegistryEntry:
    """A skill in the registry (read model)."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.id: str = str(row["id"])
        self.name: str = row["name"]
        self.slug: str = row["slug"]
        self.description: str | None = row.get("description")
        self.source_type: str = row["source_type"]
        self.source_repo: str | None = row.get("source_repo")
        self.source_ref: str | None = row.get("source_ref")
        self.source_path: str | None = row.get("source_path")
        self.files: list[SkillFile] = [SkillFile(**f) for f in (row.get("files") or [])]
        self.content_hash: str = row["content_hash"]
        self.metadata: dict[str, Any] = row.get("metadata") or {}
        self.security_verdict: str | None = row.get("security_verdict")
        self.visibility: str = row["visibility"]
        self.scope: RegistryScope = row.get("scope") or "standalone"
        self.sub_agent_id: int | None = row.get("sub_agent_id")
        self.sandbox_required: bool = row.get("sandbox_required", False)
        self.created_by: str = row["created_by"]
        self.author_name: str | None = row.get("author_name")
        self.created_at: datetime = row["created_at"]
        self.updated_at: datetime = row["updated_at"]


class SkillRegistryService:
    """Service for skill registry CRUD operations."""

    def __init__(self) -> None:
        self.repo = SkillRegistryRepository()

    def set_repository(self, repo: SkillRegistryRepository) -> None:
        self.repo = repo

    async def _save_version_snapshot(
        self,
        db: AsyncSession,
        skill_id: str,
        files_json: list[dict],
        content_hash: str,
        description: str | None,
        created_by: str,
    ) -> None:
        """Save a version snapshot keyed by content_hash (idempotent — skips duplicates)."""
        await db.execute(
            text("""
                INSERT INTO skill_registry_versions (skill_id, content_hash, files, description, created_by)
                VALUES (:skill_id, :content_hash, :files, :description, :created_by)
                ON CONFLICT (skill_id, content_hash) DO NOTHING
            """),
            {
                "skill_id": skill_id,
                "content_hash": content_hash,
                "files": json.dumps(files_json),
                "description": description,
                "created_by": created_by,
            },
        )

    async def get_version_history(
        self,
        db: AsyncSession,
        skill_id: str,
    ) -> list[dict[str, Any]]:
        """Get all version snapshots for a skill, newest first."""
        result = await db.execute(
            text("""
                SELECT content_hash, description, created_by, created_at
                FROM skill_registry_versions
                WHERE skill_id = :skill_id
                ORDER BY created_at DESC
            """),
            {"skill_id": skill_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def get_version(
        self,
        db: AsyncSession,
        skill_id: str,
        content_hash: str,
    ) -> dict[str, Any] | None:
        """Get a specific version snapshot by content_hash."""
        result = await db.execute(
            text("""
                SELECT files, content_hash, description, created_by, created_at
                FROM skill_registry_versions
                WHERE skill_id = :skill_id AND content_hash = :content_hash
            """),
            {"skill_id": skill_id, "content_hash": content_hash},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def search(
        self,
        db: AsyncSession,
        query: str,
        visibility: str | None = None,
        limit: int = 50,
        offset: int = 0,
        owner_id: str | None = None,
    ) -> tuple[list[SkillRegistryEntry], int]:
        """Search the registry with full-text search and visibility scoping.

        Users see:
        - Public skills (visibility='public')
        - Their own private skills (filtered by owner_id)

        Returns (entries, total_count) for pagination.
        """
        conditions = []
        params: dict[str, Any] = {"limit": min(limit, 200), "offset": offset}

        if query and query != "*":
            conditions.append(
                "to_tsvector('english', sr.name || ' ' || COALESCE(sr.description, '')) @@ plainto_tsquery('english', :query)"
            )
            params["query"] = query

        # Visibility scoping
        visibility_clauses = []
        visibility_clauses.append("sr.visibility = 'public'")

        # Always include the user's own private skills
        if owner_id:
            visibility_clauses.append("(sr.visibility = 'private' AND sr.owner_id = :owner_id)")
            params["owner_id"] = owner_id

        if visibility_clauses:
            conditions.append(f"({' OR '.join(visibility_clauses)})")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Get total count
        count_sql = text(f"SELECT COUNT(*) FROM skill_registry sr {where}")
        count_result = await db.execute(count_sql, params)
        total = count_result.scalar() or 0

        sql = text(f"""
            SELECT sr.*,
                   CONCAT(u.first_name, ' ', u.last_name) AS author_name
            FROM skill_registry sr
            LEFT JOIN users u ON sr.owner_id = u.id
            {where}
            ORDER BY sr.updated_at DESC
            LIMIT :limit OFFSET :offset
        """)

        result = await db.execute(sql, params)
        rows = result.mappings().all()
        return [SkillRegistryEntry(dict(row)) for row in rows], total

    async def get_by_id(self, db: AsyncSession, skill_id: str) -> SkillRegistryEntry | None:
        """Get a single registry entry by ID."""
        result = await db.execute(
            text("SELECT * FROM skill_registry WHERE id = :id"),
            {"id": skill_id},
        )
        row = result.mappings().first()
        if not row:
            return None
        return SkillRegistryEntry(dict(row))

    async def get_by_id_or_slug(self, db: AsyncSession, identifier: str) -> SkillRegistryEntry | None:
        """Get a registry entry by ID (UUID) or slug.

        Tries UUID lookup first; falls back to slug if the identifier
        doesn't look like a UUID or the UUID lookup returns nothing.
        """
        import uuid as _uuid

        # Try as UUID first
        try:
            _uuid.UUID(identifier)
            entry = await self.get_by_id(db, identifier)
            if entry:
                return entry
        except ValueError:
            pass

        # Fallback to slug lookup (any visibility)
        result = await db.execute(
            text("SELECT * FROM skill_registry WHERE slug = :slug ORDER BY updated_at DESC LIMIT 1"),
            {"slug": identifier},
        )
        row = result.mappings().first()
        if not row:
            return None
        return SkillRegistryEntry(dict(row))

    async def get_by_slug(self, db: AsyncSession, slug: str) -> SkillRegistryEntry | None:
        """Get a registry entry by slug."""
        result = await db.execute(
            text("SELECT * FROM skill_registry WHERE slug = :slug AND visibility = 'public'"),
            {"slug": slug},
        )
        row = result.mappings().first()
        if not row:
            return None
        return SkillRegistryEntry(dict(row))

    async def import_from_source(
        self,
        db: AsyncSession,
        actor: User,
        detail: SkillSourceDetail,
        source_type: str,
        visibility: str = "public",
    ) -> SkillRegistryEntry:
        """Import a skill from a SkillSourceDetail into the registry.

        Args:
            db: Database session
            actor: The user performing the import
            detail: Resolved skill detail from a SkillSource
            source_type: 'github' or 'nannos'
            visibility: 'private' or 'public'

        Returns:
            The created SkillRegistryEntry
        """
        content_hash = _compute_content_hash(detail.files)
        files_json = [{"path": f.path, "contents": f.contents} for f in detail.files]

        resolved_slug = await _ensure_unique_slug(
            db,
            detail.slug or _slugify(detail.name),
        )

        fields = {
            "name": detail.name,
            "slug": resolved_slug,
            "description": detail.description,
            "source_type": source_type,
            "source_repo": detail.source_repo,
            "source_ref": detail.source_ref,
            "source_path": detail.source_path,
            "files": json.dumps(files_json),
            "content_hash": content_hash,
            "metadata": json.dumps({"tree_sha": detail.tree_sha} if detail.tree_sha else {}),
            "visibility": visibility,
            "owner_id": actor.id,
            "created_by": actor.id,
        }

        skill_id = await self.repo.create(db=db, actor=actor, fields=fields, returning="id")
        entry = await self.get_by_id(db, str(skill_id))
        if not entry:
            raise RuntimeError("Failed to read back created registry entry")
        return entry

    async def update_visibility(
        self,
        db: AsyncSession,
        actor: User,
        skill_id: str,
        visibility: str,
    ) -> None:
        """Change a skill's visibility."""
        fields: dict[str, Any] = {
            "visibility": visibility,
            "updated_at": datetime.now(timezone.utc),
        }
        await self.repo.update(db=db, actor=actor, entity_id=skill_id, fields=fields)

    async def remove(self, db: AsyncSession, actor: User, skill_id: str) -> None:
        """Remove a skill from the registry."""
        # Fetch state for audit
        entry = await self.get_by_id(db, skill_id)
        if not entry:
            return

        await db.execute(text("DELETE FROM skill_registry WHERE id = :id"), {"id": skill_id})

        # Log deletion audit manually since we're not using repo.delete()
        await self.repo.audit_service.log_action(
            db=db,
            actor=actor,
            entity_type=self.repo.entity_type,
            entity_id=skill_id,
            action=AuditAction.DELETE,
            changes={"before": {"name": entry.name, "slug": entry.slug, "visibility": entry.visibility}},
        )

    async def find_by_content_hash(self, db: AsyncSession, content_hash: str) -> list[SkillRegistryEntry]:
        """Find registry entries with the same content hash (duplicate detection)."""
        result = await db.execute(
            text("SELECT * FROM skill_registry WHERE content_hash = :hash"),
            {"hash": content_hash},
        )
        return [SkillRegistryEntry(dict(row)) for row in result.mappings().all()]

    async def create_skill(
        self,
        db: AsyncSession,
        actor: User,
        name: str,
        description: str,
        files: list[SkillFile],
        visibility: str = "private",
        slug: str | None = None,
        group_ids: list[int] | None = None,
    ) -> SkillRegistryEntry:
        """Create a new skill in the registry (authoring flow).

        Args:
            db: Database session
            actor: The user creating the skill
            name: Skill display name
            description: What the skill does
            files: Skill files (must include SKILL.md)
            visibility: 'private' or 'public'
            slug: Optional explicit slug override. Auto-derived from name if omitted.
            group_ids: Group IDs for group-visible skills.

        Returns:
            The created registry entry
        """
        content_hash = _compute_content_hash(files)
        files_json = [{"path": f.path, "contents": f.contents} for f in files]

        resolved_slug = await _ensure_unique_slug(
            db,
            slug or _slugify(name),
        )

        fields = {
            "name": name,
            "slug": resolved_slug,
            "description": description,
            "source_type": "nannos",
            "files": json.dumps(files_json),
            "content_hash": content_hash,
            "sandbox_required": _detect_sandbox_required(files),
            "visibility": visibility,
            "owner_id": actor.id,
            "created_by": actor.id,
        }
        if group_ids is not None:
            fields["group_ids"] = group_ids

        skill_id = await self.repo.create(db=db, actor=actor, fields=fields, returning="id")
        entry = await self.get_by_id(db, str(skill_id))
        if not entry:
            raise RuntimeError("Failed to read back created registry entry")
        return entry

    async def update_skill(
        self,
        db: AsyncSession,
        actor: User,
        skill_id: str,
        files: list[SkillFile] | None = None,
        description: str | None = None,
        name: str | None = None,
        sandbox_required: bool | None = None,
        visibility: str | None = None,
    ) -> SkillRegistryEntry:
        """Update a skill in the registry. Returns entry with new content_hash.

        Only updates fields that are provided (non-None).
        Recomputes content_hash if files change.
        """
        entry = await self.get_by_id(db, skill_id)
        if not entry:
            raise ValueError(f"Registry entry not found: {skill_id}")

        fields: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

        if name is not None:
            fields["name"] = name
            fields["slug"] = await _ensure_unique_slug(
                db,
                _slugify(name),
                exclude_id=skill_id,
            )

        if description is not None:
            fields["description"] = description

        if sandbox_required is not None:
            fields["sandbox_required"] = sandbox_required

        if visibility is not None:
            fields["visibility"] = visibility

        if files is not None:
            content_hash = _compute_content_hash(files)
            files_json = [{"path": f.path, "contents": f.contents} for f in files]
            fields["files"] = json.dumps(files_json)
            fields["content_hash"] = content_hash
            fields["sandbox_required"] = _detect_sandbox_required(files)

        await self.repo.update(db=db, actor=actor, entity_id=skill_id, fields=fields)

        # Save version snapshot if files changed
        if files is not None:
            await self._save_version_snapshot(
                db=db,
                skill_id=skill_id,
                files_json=files_json,
                content_hash=content_hash,  # type: ignore[possibly-unbound]
                description=description if description is not None else entry.description,
                created_by=actor.id,
            )

        updated = await self.get_by_id(db, skill_id)
        if not updated:
            raise RuntimeError("Failed to read back updated registry entry")
        return updated

    async def upsert_agent_skill(
        self,
        db: AsyncSession,
        actor: User,
        sub_agent_id: int,
        name: str,
        description: str,
        files: list[SkillFile],
    ) -> tuple[str, str]:
        """Upsert a custom skill into the registry scoped to a sub-agent.

        If a matching skill (same sub_agent_id + slug) already exists, it is
        updated in-place. Otherwise a new entry is created.

        Returns:
            Tuple of (registry_id, content_hash).
        """
        slug = _slugify(name)
        content_hash = _compute_content_hash(files)
        files_json = [{"path": f.path, "contents": f.contents} for f in files]
        now = datetime.now(timezone.utc)

        # Check for existing entry (same agent + slug)
        result = await db.execute(
            text(
                "SELECT id FROM skill_registry "
                "WHERE scope = 'sub-agent' AND sub_agent_id = :sub_agent_id AND slug = :slug"
            ),
            {"sub_agent_id": sub_agent_id, "slug": slug},
        )
        existing = result.scalars().first()

        sandbox_required = _detect_sandbox_required(files)

        if existing:
            skill_id = str(existing)
            await self.repo.update(
                db=db,
                actor=actor,
                entity_id=skill_id,
                fields={
                    "description": description,
                    "files": json.dumps(files_json),
                    "content_hash": content_hash,
                    "sandbox_required": sandbox_required,
                    "updated_at": now,
                },
            )
            # Save version snapshot on update
            await self._save_version_snapshot(
                db=db,
                skill_id=skill_id,
                files_json=files_json,
                content_hash=content_hash,
                description=description,
                created_by=actor.id,
            )
            return skill_id, content_hash

        # Create new entry
        fields = {
            "name": name,
            "slug": slug,
            "description": description,
            "source_type": "nannos",
            "files": json.dumps(files_json),
            "content_hash": content_hash,
            "sandbox_required": sandbox_required,
            "visibility": "private",
            "scope": "sub-agent",
            "sub_agent_id": sub_agent_id,
            "owner_id": actor.id,
            "created_by": actor.id,
        }

        skill_id = await self.repo.create(db=db, actor=actor, fields=fields, returning="id")
        return str(skill_id), content_hash


# File extensions that indicate executable content requiring a sandbox.
_SANDBOX_EXTENSIONS = frozenset(
    {
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".js",
        ".ts",
        ".rb",
        ".pl",
        ".ps1",
        ".bat",
        ".cmd",
        ".mjs",
        ".cjs",
    }
)


def _slugify(name: str) -> str:
    """Derive a URL-safe slug from a display name.

    Rules match _validate_skill_name: 1-64 chars, lowercase alphanumeric + hyphens,
    no leading/trailing/consecutive hyphens.
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)  # collapse consecutive hyphens
    return s[:64]


async def _ensure_unique_slug(
    db: AsyncSession,
    base_slug: str,
    exclude_id: str | None = None,
) -> str:
    """Return a globally unique slug, appending -2, -3, … on collision.

    Slugs must be unique across all visibilities and owners because they
    are used for URL navigation and MCP tool identification.
    """
    candidate = base_slug
    suffix = 2
    while True:
        q = text(
            "SELECT 1 FROM skill_registry WHERE slug = :slug"
            + (" AND id != CAST(:exclude AS uuid)" if exclude_id else "")
        )
        params: dict[str, Any] = {"slug": candidate}
        if exclude_id:
            params["exclude"] = exclude_id
        result = await db.execute(q, params)
        if result.first() is None:
            return candidate
        candidate = f"{base_slug[:60]}-{suffix}"
        suffix += 1
        if suffix > 100:
            raise ValueError(f"Cannot find unique slug for '{base_slug}'")


def _detect_sandbox_required(files: list[SkillFile]) -> bool:
    """Return True if any file has an executable extension."""
    for f in files:
        ext = "." + f.path.rsplit(".", 1)[-1] if "." in f.path else ""
        if ext.lower() in _SANDBOX_EXTENSIONS:
            return True
    return False


def _compute_content_hash(files: list[SkillFile]) -> str:
    """Compute a stable SHA-256 hash of skill file contents.

    Sorted by path to ensure deterministic hashing regardless of file order.
    """
    hasher = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.path):
        hasher.update(f.path.encode())
        hasher.update(f.contents.encode())
    return hasher.hexdigest()
