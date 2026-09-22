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
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.audit import AuditAction
from console_backend.models.skills_registry import (
    RegistryVisibility,
    SkillProvenance,
    SkillFile,
    SkillRegistryEntry,
    SkillVersionDetail,
    SkillVersionSummary,
)
from console_backend.models.user import User
from console_backend.repositories.skill_registry_repository import SkillRegistryRepository
from console_backend.services.skill_sources.base import SkillSourceDetail

logger = logging.getLogger(__name__)

#: Called once per content change of an EXISTING registry row, inside the writer's
#: transaction: ``(db, actor, skill_id, previous_hash, new_hash)``. ADR-0011 hangs the
#: following-referrer bump on it.
ContentChangedHook = Callable[[AsyncSession, "User | None", str, str, str], Awaitable[None]]


class SkillReferencedError(ValueError):
    """A registry row other sub-agents reference cannot be deleted or made private (ADR-0011).

    Referrers hold no copy — only a reference by id and hash — so withdrawing the row
    would break their agents. Publishing is a commitment: the referrers must deactivate
    the skill first. ``referrers`` is ``[(sub_agent_id, name), ...]``.
    """

    def __init__(self, skill_id: str, referrers: list[tuple[int, str]]) -> None:
        self.skill_id = skill_id
        self.referrers = referrers
        names = ", ".join(f"'{name}'" for _, name in referrers)
        super().__init__(
            f"Skill is referenced by {len(referrers)} other agent(s): {names}. "
            "They must deactivate it before it can be deleted or made private."
        )


class SkillRegistryService:
    """Service for skill registry CRUD operations."""

    def __init__(self) -> None:
        self.repo = SkillRegistryRepository()
        self._content_changed_hook: ContentChangedHook | None = None

    def set_repository(self, repo: SkillRegistryRepository) -> None:
        self.repo = repo

    def set_content_changed_hook(self, hook: ContentChangedHook | None) -> None:
        """Register the one listener for "an existing row's content changed" (ADR-0011)."""
        self._content_changed_hook = hook

    async def referrers(self, db: AsyncSession, skill_id: str) -> list[tuple[int, str]]:
        """Sub-agents that REFERENCE this row without owning it (ADR-0011).

        A referrer is any live sub-agent other than the row's owner whose approved default
        version names the row, or that holds a sub-agent-scope activation on it. Both are
        checked: a reference that arrived through a plain config save has no activation
        row, and an activation whose version is still a pending draft has no default yet.
        """
        result = await db.execute(
            text(
                """
                SELECT DISTINCT sa.id, sa.name
                FROM sub_agents sa
                JOIN skill_registry sr ON sr.id = CAST(:skill_id AS uuid)
                LEFT JOIN sub_agent_config_versions cv
                       ON cv.sub_agent_id = sa.id AND cv.version = sa.default_version AND cv.deleted_at IS NULL
                LEFT JOIN skill_activations act
                       ON act.sub_agent_id = sa.id AND act.scope = 'sub-agent' AND act.registry_id = sr.id
                WHERE sa.deleted_at IS NULL
                  AND (sr.sub_agent_id IS NULL OR sr.sub_agent_id <> sa.id)
                  AND (
                        act.id IS NOT NULL
                     OR EXISTS (
                            SELECT 1 FROM jsonb_array_elements(COALESCE(cv.skills, '[]'::jsonb)) s
                            WHERE s->>'registry_id' = :skill_id_text
                        )
                  )
                ORDER BY sa.name
                """
            ),
            {"skill_id": skill_id, "skill_id_text": str(skill_id)},
        )
        return [(row["id"], row["name"]) for row in result.mappings().all()]

    async def _refuse_withdrawal_if_referenced(self, db: AsyncSession, skill_id: str) -> None:
        referrers = await self.referrers(db, skill_id)
        if referrers:
            raise SkillReferencedError(skill_id, referrers)

    async def check_write_access(
        self,
        db: AsyncSession,
        entry: "SkillRegistryEntry",
        user_id: str,
    ) -> bool:
        """Check if a user has write access to a registry entry.

        Authorization model:
        - Owner (owner_id == user_id) always has write access.
        - Sub-agent scoped entries inherit from the sub-agent's permissions:
          if the user has write access on the sub-agent, they can write its skills.
          Uses SubAgentService.check_user_permission internally.
        - Standalone/public entries not owned by the user: no write access.

        Args:
            db: Database session
            entry: The registry entry to check access for
            user_id: The user requesting access

        Returns:
            True if the user has write access to this registry entry.
        """
        # Owner always has full access
        if entry.owner_id == user_id:
            return True

        # Sub-agent scoped skills inherit permissions from the sub-agent
        if entry.scope == "sub-agent" and entry.sub_agent_id:
            # TODO: this pattern is sub-optimal but we lazy-import SubAgentService here to avoid circular dependencies
            # since SubAgentService also depends on SkillRegistryService
            from console_backend.services.sub_agent_service import SubAgentService

            sub_agent_service = SubAgentService()
            return await sub_agent_service.check_user_permission(
                db, sub_agent_id=entry.sub_agent_id, user_id=user_id, required_permission="write"
            )

        return False

    async def _save_version_snapshot(
        self,
        db: AsyncSession,
        skill_id: str,
        files_json: list[dict],
        content_hash: str,
        description: str | None,
        created_by: str,
        *,
        previous_hash: str | None = None,
        actor: User | None = None,
    ) -> None:
        """Save a version snapshot keyed by content_hash (idempotent — skips duplicates).

        This is the one point where "an existing row's content changed" is known: every
        writer that changes a row's files ends here. When ``previous_hash`` is given and
        differs from ``content_hash``, the registered content-changed hook runs in the
        same transaction so following referrers are bumped with the write (ADR-0011).
        """
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
        if self._content_changed_hook and previous_hash and previous_hash != content_hash:
            await self._content_changed_hook(db, actor, skill_id, previous_hash, content_hash)

    async def get_version_history(
        self,
        db: AsyncSession,
        skill_id: str,
    ) -> list[SkillVersionSummary]:
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
        return [SkillVersionSummary.model_validate(dict(row)) for row in result.mappings().all()]

    async def get_version(
        self,
        db: AsyncSession,
        skill_id: str,
        content_hash: str,
    ) -> SkillVersionDetail | None:
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
        return SkillVersionDetail.model_validate(dict(row)) if row else None

    async def search(
        self,
        db: AsyncSession,
        query: str,
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

        # Exclude sub-agent scoped skills — they are internal to their owning sub-agent —
        # unless the agent published them as public (e.g. a host-published SKILL.md with
        # `metadata.nannos-visibility: public`).
        conditions.append("(COALESCE(sr.scope, 'standalone') != 'sub-agent' OR sr.visibility = 'public')")

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
        return [SkillRegistryEntry.model_validate(dict(row)) for row in rows], total

    async def get_by_id(self, db: AsyncSession, skill_id: str) -> SkillRegistryEntry | None:
        """Get a single registry entry by ID."""
        result = await db.execute(
            text("""
                SELECT sr.*, CONCAT(u.first_name, ' ', u.last_name) AS author_name
                FROM skill_registry sr
                LEFT JOIN users u ON sr.owner_id = u.id
                WHERE sr.id = :id
            """),
            {"id": skill_id},
        )
        row = result.mappings().first()
        if not row:
            return None
        return SkillRegistryEntry.model_validate(dict(row))

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
            text("""
                SELECT sr.*, CONCAT(u.first_name, ' ', u.last_name) AS author_name
                FROM skill_registry sr
                LEFT JOIN users u ON sr.owner_id = u.id
                WHERE sr.slug = :slug
                ORDER BY sr.updated_at DESC LIMIT 1
            """),
            {"slug": identifier},
        )
        row = result.mappings().first()
        if not row:
            return None
        return SkillRegistryEntry.model_validate(dict(row))

    async def get_by_slug(self, db: AsyncSession, slug: str) -> SkillRegistryEntry | None:
        """Get a registry entry by slug."""
        result = await db.execute(
            text("""
                SELECT sr.*, CONCAT(u.first_name, ' ', u.last_name) AS author_name
                FROM skill_registry sr
                LEFT JOIN users u ON sr.owner_id = u.id
                WHERE sr.slug = :slug AND sr.visibility = 'public'
            """),
            {"slug": slug},
        )
        row = result.mappings().first()
        if not row:
            return None
        return SkillRegistryEntry.model_validate(dict(row))

    async def import_from_source(
        self,
        db: AsyncSession,
        actor: User,
        detail: SkillSourceDetail,
        source_type: str,
        visibility: RegistryVisibility = "public",
    ) -> SkillRegistryEntry:
        """Import a skill from a SkillSourceDetail into the registry.

        Args:
            db: Database session
            actor: The user performing the import
            detail: Resolved skill detail from a SkillSource
            source_type: 'github' or 'nannos'
            visibility: 'private' or 'public'

        Returns:
            The created SkillRegistryRow
        """
        content_hash = _compute_content_hash(detail.files)
        files_json = [{"path": f.path, "content": f.content} for f in detail.files]

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
        visibility: RegistryVisibility,
    ) -> None:
        """Change a skill's visibility. Making a referenced row private is refused (ADR-0011)."""
        if visibility == "private":
            await self._refuse_withdrawal_if_referenced(db, skill_id)
        fields: dict[str, Any] = {
            "visibility": visibility,
            "updated_at": datetime.now(timezone.utc),
        }
        await self.repo.update(db=db, actor=actor, entity_id=skill_id, fields=fields)

    async def remove(self, db: AsyncSession, actor: User, skill_id: str) -> None:
        """Remove a skill from the registry. Refused while other agents reference it (ADR-0011)."""
        # Fetch state for audit
        entry = await self.get_by_id(db, skill_id)
        if not entry:
            return

        await self._refuse_withdrawal_if_referenced(db, skill_id)
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

    async def prune_mirrored_skills(
        self,
        db: AsyncSession,
        actor: User,
        sub_agent_id: int,
        keep_ids: list[str],
    ) -> list[str]:
        """Delete the sub-agent's mirrored rows that the latest sync did not write.

        ``upsert_agent_skill`` matches a mirrored row by (sub-agent, source type, name),
        so renaming a skill upstream writes a new row and withdrawing one writes nothing
        — either way the previous row survives with no binding pointing at it. A public
        one stays world-readable forever, which is the part that matters: the host has
        stopped publishing the skill but the registry has not.

        Scoped to rows that carry provenance (``source_type <> 'nannos'``), so a skill
        somebody authored by hand on the same agent is never swept up by a sync.

        A row an activation still references is left in place. ``skill_activations``
        cascades on delete, and a sub-agent-scope activation holds only a SkillRef into
        this table rather than a copy of the content, so deleting such a row would break
        a live agent rather than merely tidy the registry. Those are logged instead:
        the host has withdrawn something another agent is still using, which is a
        decision for a human, not a side effect of a sync.

        Returns the ids removed.
        """
        result = await db.execute(
            text(
                "SELECT sr.id, COUNT(sa.id) AS refs FROM skill_registry sr "
                "LEFT JOIN skill_activations sa ON sa.registry_id = sr.id "
                "WHERE sr.sub_agent_id = :sub_agent_id AND sr.scope = 'sub-agent' "
                "AND COALESCE(sr.source_type, 'nannos') <> 'nannos' "
                "GROUP BY sr.id"
            ),
            {"sub_agent_id": sub_agent_id},
        )
        keep = {str(k) for k in keep_ids}
        stale: list[str] = []
        for row in result.mappings().all():
            skill_id = str(row["id"])
            if skill_id in keep:
                continue
            if row["refs"]:
                logger.warning(
                    "Sub-agent %s no longer mirrors registry skill %s, but %d activation(s) still "
                    "reference it — keeping the row; it needs a human decision.",
                    sub_agent_id,
                    skill_id,
                    row["refs"],
                )
                continue
            stale.append(skill_id)
        for skill_id in stale:
            await self.remove(db, actor, skill_id)
        if stale:
            logger.info("Pruned %d stale mirrored registry row(s) for sub-agent %s", len(stale), sub_agent_id)
        return stale

    async def find_by_content_hash(self, db: AsyncSession, content_hash: str) -> list[SkillRegistryEntry]:
        """Find registry entries with the same content hash (duplicate detection)."""
        result = await db.execute(
            text("""
                SELECT sr.*, CONCAT(u.first_name, ' ', u.last_name) AS author_name
                FROM skill_registry sr
                LEFT JOIN users u ON sr.owner_id = u.id
                WHERE sr.content_hash = :hash
            """),
            {"hash": content_hash},
        )
        return [SkillRegistryEntry.model_validate(dict(row)) for row in result.mappings().all()]

    async def create_skill(
        self,
        db: AsyncSession,
        actor: User,
        name: str,
        description: str,
        files: list[SkillFile],
        visibility: RegistryVisibility = "private",
        slug: str | None = None,
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

        Returns:
            The created registry entry
        """
        # If description is empty, try to extract from SKILL.md frontmatter
        if not description:
            skill_md = next((f for f in files if f.path == "SKILL.md"), None)
            if skill_md:
                description = _extract_description_from_frontmatter(skill_md.content)

        content_hash = _compute_content_hash(files)
        files_json = [{"path": f.path, "content": f.content} for f in files]

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

        skill_id = await self.repo.create(db=db, actor=actor, fields=fields, returning="id")
        # Save initial version snapshot so version history includes the first creation
        await self._save_version_snapshot(
            db=db,
            skill_id=str(skill_id),
            files_json=files_json,
            content_hash=content_hash,
            description=description,
            created_by=actor.id,
        )
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
        visibility: RegistryVisibility | None = None,
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
            if visibility == "private" and entry.visibility == "public":
                await self._refuse_withdrawal_if_referenced(db, skill_id)
            fields["visibility"] = visibility

        if files is not None:
            content_hash = _compute_content_hash(files)
            files_json = [{"path": f.path, "content": f.content} for f in files]
            fields["files"] = json.dumps(files_json)
            fields["content_hash"] = content_hash
            fields["sandbox_required"] = _detect_sandbox_required(files)

            # Sync description from SKILL.md frontmatter if not explicitly provided
            if description is None:
                skill_md = next((f for f in files if f.path == "SKILL.md"), None)
                if skill_md:
                    extracted = _extract_description_from_frontmatter(skill_md.content)
                    if extracted:
                        fields["description"] = extracted

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
                previous_hash=entry.content_hash,
                actor=actor,
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
        registry_id: str | None = None,
        visibility: RegistryVisibility | None = None,
        provenance: SkillProvenance | None = None,
    ) -> tuple[str, str]:
        """Upsert a custom skill into the registry scoped to a sub-agent.

        If registry_id is provided, updates that entry directly (idempotent).
        Without one, a skill with `provenance` updates the row this sub-agent already
        holds for the same (source_type, name) — the row a previous sync wrote. In every
        other case a new entry is created.

        `visibility` None keeps the entry's current value (private for a new entry);
        'public' makes the sub-agent skill discoverable and activatable by every user.
        `provenance` is stored in the `source_*` columns; without it the row is a
        console-authored ('nannos') skill.

        Returns:
            Tuple of (registry_id, content_hash).
        """
        content_hash = _compute_content_hash(files)
        files_json = [{"path": f.path, "content": f.content} for f in files]
        now = datetime.now(timezone.utc)

        skill_id = None
        row = None
        # Prefer direct ID lookup when available (idempotent)
        if registry_id:
            result = await db.execute(
                text(
                    "SELECT id, slug, name, content_hash, visibility FROM skill_registry WHERE id = CAST(:id AS uuid)"
                ),
                {"id": registry_id},
            )
            row = result.mappings().first()
        elif provenance is not None:
            # A mirrored skill: the sync does not know the id of the row it wrote last
            # time, but the row is the one this sub-agent holds for the same source and
            # name. Matching on the name, not the slug: the slug may carry a -2 suffix.
            result = await db.execute(
                text(
                    "SELECT id, slug, name, content_hash, visibility FROM skill_registry "
                    "WHERE sub_agent_id = :sub_agent_id AND scope = 'sub-agent' "
                    "AND source_type = :source_type AND name = :name "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {
                    "sub_agent_id": sub_agent_id,
                    "source_type": provenance.source_type,
                    "name": name,
                },
            )
            row = result.mappings().first()
        if row:
            skill_id = str(row["id"])
            slug = row["slug"]
            if row["name"] != name:
                # update the slug if the name has changed
                slug = await _ensure_unique_slug(db, _slugify(name), exclude_id=skill_id)
        else:
            slug = await _ensure_unique_slug(db, base_slug=_slugify(name))

        provenance_fields: dict[str, Any] = (
            {
                "source_type": provenance.source_type,
                "source_repo": provenance.source_repo,
                "source_ref": provenance.source_ref,
                "source_path": provenance.source_path,
            }
            if provenance is not None
            else {}
        )

        sandbox_required = _detect_sandbox_required(files)

        if skill_id:
            update_fields: dict[str, Any] = {
                "name": name,
                "slug": slug,
                "description": description,
                "files": json.dumps(files_json),
                "content_hash": content_hash,
                "sandbox_required": sandbox_required,
                "updated_at": now,
            }
            if visibility is not None:
                if visibility == "private" and row["visibility"] == "public":
                    # A host sync (or a config save) withdrawing a skill other agents
                    # reference: keep it public and say so, rather than break the
                    # referrers or fail the whole sync. Like prune_mirrored_skills, this
                    # is a decision for a human, not a side effect of a sync (ADR-0011).
                    referrers = await self.referrers(db, skill_id)
                    if referrers:
                        logger.warning(
                            "Skill %s (%s) stays public: %d other agent(s) reference it (%s)",
                            skill_id,
                            slug,
                            len(referrers),
                            ", ".join(name for _, name in referrers),
                        )
                    else:
                        update_fields["visibility"] = visibility
                else:
                    update_fields["visibility"] = visibility
            update_fields.update(provenance_fields)
            await self.repo.update(
                db=db,
                actor=actor,
                entity_id=skill_id,
                fields=update_fields,
            )
            # Save version snapshot on update; bumps following referrers if the hash moved
            await self._save_version_snapshot(
                db=db,
                skill_id=skill_id,
                files_json=files_json,
                content_hash=content_hash,
                description=description,
                created_by=actor.id,
                previous_hash=row["content_hash"],
                actor=actor,
            )
            return skill_id, content_hash

        # Create new entry
        fields = {
            "name": name,
            "slug": slug,
            "description": description,
            "source_type": "nannos",
            **provenance_fields,
            "files": json.dumps(files_json),
            "content_hash": content_hash,
            "sandbox_required": sandbox_required,
            "visibility": visibility or "private",
            "scope": "sub-agent",
            "sub_agent_id": sub_agent_id,
            "owner_id": actor.id,
            "created_by": actor.id,
        }

        skill_id = await self.repo.create(db=db, actor=actor, fields=fields, returning="id")
        # Save initial version snapshot
        await self._save_version_snapshot(
            db=db,
            skill_id=str(skill_id),
            files_json=files_json,
            content_hash=content_hash,
            description=description,
            created_by=actor.id,
        )
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


def _extract_description_from_frontmatter(content: str) -> str:
    """Extract description from SKILL.md YAML frontmatter."""
    if not content.startswith("---"):
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
        hasher.update(f.content.encode())
    return hasher.hexdigest()
