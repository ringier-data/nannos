"""Skill Activation Service — manages skill activations on agents.

Responsibilities:
- Activate a registry skill on an agent (write activation record + docstore snapshot)
- Deactivate (remove activation + docstore snapshot)
- Self-update: refresh own activation after registry edit (author's fast path)
- List activations for an agent with update-available detection
- Upsert locked activations during config set-default

Does NOT own:
- Registry CRUD (that's SkillRegistryService)
- External source discovery (that's SkillsRegistryService)
- Security scanning (that's SkillSecurityService)
"""

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from console_backend.models.skills_registry import (
    SkillActivationWithStatus,
)
from console_backend.services.playbook_service import PlaybookService

if TYPE_CHECKING:
    from console_backend.models.user import User
    from console_backend.services.sub_agent_service import SubAgentService

logger = logging.getLogger(__name__)


class SkillActivationService:
    """Service for managing skill activations on agents."""

    def __init__(self) -> None:
        self._playbook_service: PlaybookService | None = None
        self._sub_agent_service: "SubAgentService | None" = None

    def set_playbook_service(self, service: PlaybookService) -> None:
        self._playbook_service = service

    def set_sub_agent_service(self, service: "SubAgentService") -> None:
        from console_backend.services.sub_agent_service import SubAgentService

        assert isinstance(service, SubAgentService)
        self._sub_agent_service = service

    @property
    def playbook_service(self) -> PlaybookService:
        if not self._playbook_service:
            raise RuntimeError("PlaybookService not configured on SkillActivationService")
        return self._playbook_service

    @property
    def sub_agent_service(self) -> "SubAgentService":
        if not self._sub_agent_service:
            raise RuntimeError("SubAgentService not configured on SkillActivationService")
        return self._sub_agent_service

    async def activate(
        self,
        db: AsyncSession,
        registry_id: str,
        sub_agent_id: int,
        agent_name: str,
        scope: str,
        user_id: str,
        group_id: int | None = None,
        activated_by: str | None = None,
    ) -> int:
        """Activate a registry skill on an agent.

        Creates an activation record and writes the skill snapshot to docstore.

        Args:
            db: Database session (console DB)
            registry_id: UUID of the skill in the registry
            sub_agent_id: Target sub-agent ID
            agent_name: Sub-agent name (for docstore key)
            scope: 'personal' or 'group'
            user_id: User ID (used as docstore prefix for personal scope)
            group_id: Group ID (required for group scope)
            activated_by: User sub who triggered the activation (defaults to user_id)

        Returns:
            The activation record ID

        Raises:
            ValueError: If the skill is already activated in this scope
        """
        if scope == "group" and not group_id:
            raise ValueError("group_id is required for group scope")

        activated_by = activated_by or user_id

        # Fetch the registry entry
        registry = await self._get_registry_entry(db, registry_id)
        if not registry:
            raise ValueError(f"Registry entry not found: {registry_id}")

        # Check for existing activation — idempotent: return existing ID
        existing = await self._find_activation(db, sub_agent_id, registry_id, scope, user_id, group_id)
        if existing:
            return existing["id"]

        # Insert activation record
        result = await db.execute(
            text("""
                INSERT INTO skill_activations
                    (sub_agent_id, registry_id, scope, user_id, group_id, content_hash, locked, activated_by)
                VALUES
                    (:sub_agent_id, :registry_id, :scope, :user_id, :group_id, :content_hash, FALSE, :activated_by)
                RETURNING id
            """),
            {
                "sub_agent_id": sub_agent_id,
                "registry_id": registry_id,
                "scope": scope,
                "user_id": user_id if scope == "personal" else None,
                "group_id": group_id if scope == "group" else None,
                "content_hash": registry["content_hash"],
                "activated_by": activated_by,
            },
        )
        activation_id = result.scalar_one()

        # Write snapshot to docstore
        await self._write_snapshot_to_docstore(
            agent_name=agent_name,
            skill_name=registry["slug"],
            scope=scope,
            user_id=user_id,
            group_id=str(group_id) if group_id else None,
            files=registry["files"],
        )

        logger.info(
            "Activated skill %s on agent %s (scope=%s, hash=%s)",
            registry["name"],
            agent_name,
            scope,
            registry["content_hash"][:12],
        )
        return activation_id

    async def activate_as_default(
        self,
        db: AsyncSession,
        registry_id: str,
        sub_agent_id: int,
        agent_name: str,
        actor: "User",
    ) -> int:
        """Activate a registry skill as a default (locked) skill on a sub-agent.

        Creates a new config version with the skill appended to the skills list
        (versions are immutable) and creates a locked activation record for tracking.

        Requires the caller to have owner/write access (enforced by the router).

        Args:
            db: Database session
            registry_id: UUID of the skill in the registry
            sub_agent_id: Target sub-agent ID
            agent_name: Sub-agent name (unused, kept for API compat)
            actor: User performing the activation

        Returns:
            The activation record ID

        Raises:
            ValueError: If the registry entry is not found or agent has no default version
        """
        registry = await self._get_registry_entry(db, registry_id)
        if not registry:
            raise ValueError(f"Registry entry not found: {registry_id}")

        # Check for existing locked activation of this skill on this agent
        result = await db.execute(
            text("""
                SELECT id FROM skill_activations
                WHERE sub_agent_id = :sub_agent_id
                  AND registry_id = :registry_id
                  AND locked = TRUE
            """),
            {"sub_agent_id": sub_agent_id, "registry_id": registry_id},
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

        # Create a new config version with this skill appended (immutable versions).
        # add_skill_to_config is idempotent — skips if already present by source ref.
        await self.sub_agent_service.add_skill_to_config(
            db=db,
            sub_agent_id=sub_agent_id,
            registry_id=registry_id,
            skill_name=registry["name"],
            skill_description=registry.get("description") or "",
            content_hash=registry["content_hash"],
            actor=actor,
        )

        # Create locked activation record (for tracking/listing/update-detection)
        result = await db.execute(
            text("""
                INSERT INTO skill_activations
                    (sub_agent_id, registry_id, scope, user_id, group_id, content_hash,
                     locked, activated_by)
                VALUES
                    (:sub_agent_id, :registry_id, 'group', NULL, NULL, :content_hash,
                     TRUE, :activated_by)
                RETURNING id
            """),
            {
                "sub_agent_id": sub_agent_id,
                "registry_id": registry_id,
                "content_hash": registry["content_hash"],
                "activated_by": actor.id,
            },
        )
        activation_id = result.scalar_one()

        logger.info(
            "Activated skill %s as default on agent %s (locked, hash=%s)",
            registry["name"],
            agent_name,
            registry["content_hash"][:12],
        )
        return activation_id

    async def deactivate(
        self,
        db: AsyncSession,
        activation_id: int,
        agent_name: str,
        user_id: str,
    ) -> bool:
        """Deactivate a skill (remove activation record + docstore snapshot).

        Args:
            activation_id: ID of the activation record
            agent_name: Sub-agent name (for docstore cleanup)
            user_id: User requesting deactivation

        Returns:
            True if deactivated, False if not found or locked
        """
        # Fetch the activation
        activation = await self._get_activation(db, activation_id)
        if not activation:
            return False

        if activation["locked"]:
            raise ValueError("Cannot deactivate a locked activation (managed by config version)")

        # Remove docstore snapshot
        skill_name = await self._get_skill_name_for_activation(db, activation)
        if skill_name:
            await self._remove_snapshot_from_docstore(
                agent_name=agent_name,
                skill_name=skill_name,
                scope=activation["scope"],
                user_id=user_id,
                group_id=str(activation["group_id"]) if activation["group_id"] else None,
            )

        # Delete activation record
        await db.execute(
            text("DELETE FROM skill_activations WHERE id = :id"),
            {"id": activation_id},
        )

        logger.info("Deactivated skill activation %d from agent %s", activation_id, agent_name)
        return True

    async def update_activation(
        self,
        db: AsyncSession,
        activation_id: int,
        agent_name: str,
        user_id: str,
    ) -> str | None:
        """Pull latest from registry — update activation hash + docstore snapshot.

        Returns:
            New content hash, or None if activation not found
        """
        activation = await self._get_activation(db, activation_id)
        if not activation:
            return None

        if activation["locked"]:
            raise ValueError("Cannot update a locked activation (managed by config version)")

        # Get latest from registry
        registry = await self._get_registry_entry(db, str(activation["registry_id"]))
        if not registry:
            raise ValueError("Registry entry no longer exists")

        new_hash = registry["content_hash"]

        # Update activation hash
        await db.execute(
            text("UPDATE skill_activations SET content_hash = :hash WHERE id = :id"),
            {"hash": new_hash, "id": activation_id},
        )

        # Refresh docstore snapshot
        await self._write_snapshot_to_docstore(
            agent_name=agent_name,
            skill_name=registry["slug"],
            scope=activation["scope"],
            user_id=user_id,
            group_id=str(activation["group_id"]) if activation["group_id"] else None,
            files=registry["files"],
        )

        logger.info(
            "Updated activation %d to hash %s",
            activation_id,
            new_hash[:12],
        )
        return new_hash

    async def self_update(
        self,
        db: AsyncSession,
        registry_id: str,
        sub_agent_id: int,
        agent_name: str,
        actor: "User | None" = None,
        user_id: str | None = None,
    ) -> bool:
        """Auto-update the calling agent's own activation after a registry edit.

        Called by MCP tools after editing a registry entry. Only updates activations
        belonging to the specified sub_agent_id — other consumers stay pinned.

        For locked activations, creates a new config version with the updated hash
        (versions are immutable). For personal/group activations, refreshes docstore.

        Args:
            db: Database session
            registry_id: UUID of the registry skill
            sub_agent_id: Target sub-agent ID
            agent_name: Sub-agent name (for docstore keys)
            actor: User performing the update (required for locked activations)
            user_id: Deprecated — use actor instead. Falls back for docstore writes.

        Returns:
            True if an activation was updated, False if no activation exists
        """
        # Find all activations of this registry entry on this agent
        result = await db.execute(
            text("""
                SELECT id, scope, group_id, locked FROM skill_activations
                WHERE registry_id = :registry_id AND sub_agent_id = :sub_agent_id
            """),
            {"registry_id": registry_id, "sub_agent_id": sub_agent_id},
        )
        activations = result.mappings().all()
        if not activations:
            return False

        # Get current registry hash
        registry = await self._get_registry_entry(db, registry_id)
        if not registry:
            return False

        new_hash = registry["content_hash"]
        effective_user_id = actor.id if actor else user_id

        for act in activations:
            # Update activation hash
            await db.execute(
                text("UPDATE skill_activations SET content_hash = :hash WHERE id = :id"),
                {"hash": new_hash, "id": act["id"]},
            )

            if act["locked"] or (act["scope"] == "group" and not act["group_id"]):
                # Locked/default activation — create a new config version with updated hash
                if actor:
                    await self.sub_agent_service.update_skill_hash_in_config(
                        db=db,
                        sub_agent_id=sub_agent_id,
                        registry_id=registry_id,
                        new_hash=new_hash,
                        actor=actor,
                    )
                else:
                    logger.warning(
                        "Cannot create new config version for locked activation %d — no actor provided",
                        act["id"],
                    )
            else:
                # Personal/group activation — refresh docstore snapshot
                if effective_user_id:
                    await self._write_snapshot_to_docstore(
                        agent_name=agent_name,
                        skill_name=registry["slug"],
                        scope=act["scope"],
                        user_id=effective_user_id,
                        group_id=str(act["group_id"]) if act["group_id"] else None,
                        files=registry["files"],
                    )

        logger.info(
            "Self-updated %d activation(s) for registry %s on agent %s",
            len(activations),
            registry_id,
            agent_name,
        )
        return True

    async def list_for_agent(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        user_id: str,
        group_ids: list[int] | None = None,
    ) -> list[SkillActivationWithStatus]:
        """List all activations for an agent, enriched with update-available status.

        Shows:
        - Personal activations for this user
        - Group activations for user's groups
        - Locked activations (visible to all)
        """
        params: dict[str, Any] = {"sub_agent_id": sub_agent_id, "user_id": user_id}

        # Build scope filter
        scope_conditions = ["(sa.scope = 'personal' AND sa.user_id = :user_id)"]
        if group_ids:
            scope_conditions.append("(sa.scope = 'group' AND sa.group_id = ANY(:group_ids))")
            params["group_ids"] = group_ids
        scope_conditions.append("sa.locked = TRUE")  # locked always visible

        scope_filter = " OR ".join(scope_conditions)

        result = await db.execute(
            text(f"""
                SELECT
                    sa.id,
                    sa.sub_agent_id,
                    sa.registry_id::text as registry_id,
                    sa.scope,
                    sa.user_id,
                    sa.group_id,
                    ug.name as group_name,
                    sa.content_hash,
                    sa.locked,
                    sa.activated_at,
                    sa.activated_by,
                    sr.slug as skill_slug,
                    sr.name as skill_name,
                    sr.description as skill_description,
                    sr.content_hash as latest_hash
                FROM skill_activations sa
                JOIN skill_registry sr ON sr.id = sa.registry_id
                LEFT JOIN user_groups ug ON ug.id = sa.group_id
                WHERE sa.sub_agent_id = :sub_agent_id
                  AND ({scope_filter})
                ORDER BY sa.activated_at DESC
            """),
            params,
        )
        rows = result.mappings().all()

        return [
            SkillActivationWithStatus(
                id=row["id"],
                sub_agent_id=row["sub_agent_id"],
                registry_id=row["registry_id"],
                scope=row["scope"],
                user_id=row["user_id"],
                group_id=row["group_id"],
                group_name=row["group_name"],
                content_hash=row["content_hash"],
                locked=row["locked"],
                activated_at=row["activated_at"],
                activated_by=row["activated_by"],
                skill_slug=row["skill_slug"],
                skill_name=row["skill_name"],
                skill_description=row["skill_description"],
                update_available=row["content_hash"] != row["latest_hash"],
                latest_hash=row["latest_hash"] if row["content_hash"] != row["latest_hash"] else None,
            )
            for row in rows
        ]

    async def upsert_locked(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        agent_name: str,
        registry_refs: list[dict[str, Any]],
        config_version_id: int,
        activated_by: str,
    ) -> None:
        """Create/update locked activations during config set-default.

        Args:
            sub_agent_id: Target agent
            agent_name: Agent name for docstore keys
            registry_refs: List of {"registry_id": str, "name": str} from config version
            config_version_id: The config version creating these locks
            activated_by: User who approved the config version
        """
        # Remove existing locked activations for this config version's agent
        await db.execute(
            text("DELETE FROM skill_activations WHERE sub_agent_id = :sub_agent_id AND locked = TRUE"),
            {"sub_agent_id": sub_agent_id},
        )

        for ref in registry_refs:
            registry_id = ref["registry_id"]
            registry = await self._get_registry_entry(db, registry_id)
            if not registry:
                logger.warning("Registry entry %s not found during locked activation upsert", registry_id)
                continue

            # Create locked activation (group scope with no specific group = system-level)
            await db.execute(
                text("""
                    INSERT INTO skill_activations
                        (sub_agent_id, registry_id, scope, user_id, group_id, content_hash,
                         locked, config_version_id, activated_by)
                    VALUES
                        (:sub_agent_id, :registry_id, 'group', NULL, NULL, :content_hash,
                         TRUE, :config_version_id, :activated_by)
                """),
                {
                    "sub_agent_id": sub_agent_id,
                    "registry_id": registry_id,
                    "content_hash": registry["content_hash"],
                    "config_version_id": config_version_id,
                    "activated_by": activated_by,
                },
            )

        logger.info(
            "Upserted %d locked activations for agent %s (config_version=%d)",
            len(registry_refs),
            agent_name,
            config_version_id,
        )

    # --- Internal helpers ---

    async def _get_registry_entry(self, db: AsyncSession, registry_id: str) -> dict[str, Any] | None:
        """Fetch a registry entry as a dict."""
        result = await db.execute(
            text("SELECT * FROM skill_registry WHERE id = :id"),
            {"id": registry_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _get_activation(self, db: AsyncSession, activation_id: int) -> dict[str, Any] | None:
        """Fetch an activation record."""
        result = await db.execute(
            text("SELECT * FROM skill_activations WHERE id = :id"),
            {"id": activation_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _find_activation(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        registry_id: str,
        scope: str,
        user_id: str,
        group_id: int | None,
    ) -> dict[str, Any] | None:
        """Check if an activation already exists for this combination."""
        if scope == "personal":
            result = await db.execute(
                text("""
                    SELECT * FROM skill_activations
                    WHERE sub_agent_id = :sub_agent_id
                      AND registry_id = :registry_id
                      AND scope = 'personal'
                      AND user_id = :user_id
                """),
                {"sub_agent_id": sub_agent_id, "registry_id": registry_id, "user_id": user_id},
            )
        else:
            result = await db.execute(
                text("""
                    SELECT * FROM skill_activations
                    WHERE sub_agent_id = :sub_agent_id
                      AND registry_id = :registry_id
                      AND scope = 'group'
                      AND group_id = :group_id
                """),
                {"sub_agent_id": sub_agent_id, "registry_id": registry_id, "group_id": group_id},
            )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _get_skill_name_for_activation(self, db: AsyncSession, activation: dict[str, Any]) -> str | None:
        """Get the skill slug from the registry for an activation."""
        result = await db.execute(
            text("SELECT slug FROM skill_registry WHERE id = :id"),
            {"id": str(activation["registry_id"])},
        )
        row = result.scalar_one_or_none()
        return row

    async def find_activation_by_skill_name(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        skill_name: str,
        scope: str,
        user_id: str | None,
        group_id: int | None,
    ) -> dict[str, Any] | None:
        """Find an activation by skill slug or display name (joining with registry).

        Matches on r.slug first, then falls back to case-insensitive r.name match.
        This handles the case where the caller passes the display name (from list_for_agent)
        instead of the slug.
        """
        if scope == "personal":
            result = await db.execute(
                text("""
                    SELECT a.* FROM skill_activations a
                    JOIN skill_registry r ON r.id = a.registry_id
                    WHERE a.sub_agent_id = :sub_agent_id
                      AND (r.slug = :slug OR LOWER(r.name) = LOWER(:slug))
                      AND a.scope = 'personal'
                      AND a.user_id = :user_id
                """),
                {"sub_agent_id": sub_agent_id, "slug": skill_name, "user_id": user_id},
            )
        else:
            result = await db.execute(
                text("""
                    SELECT a.* FROM skill_activations a
                    JOIN skill_registry r ON r.id = a.registry_id
                    WHERE a.sub_agent_id = :sub_agent_id
                      AND (r.slug = :slug OR LOWER(r.name) = LOWER(:slug))
                      AND a.scope = 'group'
                      AND a.group_id = :group_id
                """),
                {"sub_agent_id": sub_agent_id, "slug": skill_name, "group_id": group_id},
            )
        row = result.mappings().first()
        return dict(row) if row else None

    async def _write_snapshot_to_docstore(
        self,
        agent_name: str,
        skill_name: str,
        scope: str,
        user_id: str,
        group_id: str | None,
        files: list[dict[str, Any]],
    ) -> None:
        """Write skill files to docstore as a snapshot."""
        # Build SKILL.md content from files
        skill_md_content = None
        bundled_files = []

        for f in files:
            path = f.get("path", "")
            contents = f.get("contents", "")
            if path == "SKILL.md":
                skill_md_content = contents
            else:
                bundled_files.append({"path": path, "content": contents, "encoding": f.get("encoding")})

        if not skill_md_content:
            logger.warning("No SKILL.md found in registry files for skill %s", skill_name)
            return

        await self.playbook_service.put_skill_with_files(
            user_id=user_id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope=scope,
            content=skill_md_content,
            files=bundled_files if bundled_files else None,
            group_id=group_id,
            replace_files=True,
        )

    async def _remove_snapshot_from_docstore(
        self,
        agent_name: str,
        skill_name: str,
        scope: str,
        user_id: str,
        group_id: str | None,
    ) -> None:
        """Remove skill files from docstore."""
        await self.playbook_service.delete_skill(
            user_id=user_id,
            agent_name=agent_name,
            skill_name=skill_name,
            scope=scope,
            group_id=group_id,
        )
