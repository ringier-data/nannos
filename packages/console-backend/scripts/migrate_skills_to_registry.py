"""One-shot migration: existing docstore skills → skill_registry + skill_activations.

This script reads all skills currently stored in the docstore (LangGraph store table)
and migrates them to the new registry-based model:

1. For each unique skill (by slug), creates a `skill_registry` entry
2. Creates `skill_activations` records linking agents ↔ registry entries
3. Leaves docstore content in place (it becomes the valid snapshot)

Usage:
    cd packages/console-backend
    uv run python scripts/migrate_skills_to_registry.py [--dry-run]

Environment variables required:
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
    DOCSTORE_HOST (or POSTGRES_HOST), DOCSTORE_PORT (or POSTGRES_PORT),
    DOCSTORE_DB, DOCSTORE_USER (or POSTGRES_USER), DOCSTORE_PASSWORD (or POSTGRES_PASSWORD)
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _build_dsn(prefix: str = "") -> str:
    """Build a PostgreSQL DSN from environment variables."""
    host = os.getenv(f"{prefix}HOST", os.getenv("POSTGRES_HOST", "localhost"))
    port = os.getenv(f"{prefix}PORT", os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv(f"{prefix}USER", os.getenv("POSTGRES_USER", "postgres"))
    password = os.getenv(f"{prefix}PASSWORD", os.getenv("POSTGRES_PASSWORD", "password"))
    database = os.getenv(f"{prefix}DB", os.getenv("POSTGRES_DB", "console"))
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"


def _compute_content_hash(files: list[dict]) -> str:
    """Compute SHA-256 hash of skill files (sorted by path for determinism)."""
    hasher = hashlib.sha256()
    for f in sorted(files, key=lambda x: x["path"]):
        hasher.update(f["path"].encode())
        hasher.update(f["contents"].encode())
    return hasher.hexdigest()


def _slugify(name: str) -> str:
    """Convert a skill folder name to a URL-safe slug."""
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")


def _parse_scope_from_prefix(prefix: str) -> tuple[str, str | None, str | None]:
    """Parse a docstore prefix into (scope, user_id, group_id).

    Prefix format:
      - "{user_id}.agent-data" → personal scope
      - "{group_id}.agent-data" → group scope (group_id is integer-like)
    """
    parts = prefix.split(".", 1)
    identifier = parts[0]

    # If identifier looks like an integer, it's a group scope
    if identifier.isdigit():
        return "group", None, identifier
    else:
        return "personal", identifier, None


async def _get_all_docstore_skills(docstore_session: AsyncSession) -> list[dict]:
    """Query all skill entries from the docstore store table.

    Returns a list of dicts: {prefix, agent_name, skill_name, files: [{path, contents}]}
    """
    result = await docstore_session.execute(
        text("SELECT prefix, key, value FROM store WHERE key LIKE '%/skills/%' ORDER BY prefix, key")
    )
    rows = result.all()

    # Group by (prefix, agent_name, skill_name)
    skills: dict[tuple[str, str, str], list[dict]] = {}
    for prefix, key, value in rows:
        # Key format: /{agent_name}/skills/{skill_name}/{file_path}
        # e.g., /orchestrator/skills/python-tools/SKILL.md
        match = re.match(r"^/([^/]+)/skills/([^/]+)/(.+)$", key)
        if not match:
            continue

        agent_name, skill_name, file_path = match.groups()
        group_key = (prefix, agent_name, skill_name)

        if group_key not in skills:
            skills[group_key] = []

        # Extract content from JSONB value
        content = ""
        if isinstance(value, dict):
            content = value.get("content", "")
        elif isinstance(value, str):
            try:
                parsed = json.loads(value)
                content = parsed.get("content", "")
            except (json.JSONDecodeError, TypeError):
                content = value

        skills[group_key].append({"path": file_path, "contents": content})

    # Convert to flat list
    result_list = []
    for (prefix, agent_name, skill_name), files in skills.items():
        result_list.append(
            {
                "prefix": prefix,
                "agent_name": agent_name,
                "skill_name": skill_name,
                "files": files,
            }
        )

    return result_list


async def _get_sub_agent_map(console_session: AsyncSession) -> dict[str, int]:
    """Get mapping of agent name → sub_agent_id."""
    result = await console_session.execute(text("SELECT id, name FROM sub_agents"))
    return {row[1]: row[0] for row in result.all()}


async def _get_user_ids(console_session: AsyncSession) -> set[str]:
    """Get all valid user IDs."""
    result = await console_session.execute(text("SELECT id FROM users"))
    return {row[0] for row in result.all()}


async def _get_group_ids(console_session: AsyncSession) -> set[int]:
    """Get all valid group IDs."""
    result = await console_session.execute(text("SELECT id FROM user_groups"))
    return {row[0] for row in result.all()}


async def _migrate_docstore_skills(dry_run: bool = False) -> None:
    """Main migration logic."""
    console_dsn = _build_dsn("")
    docstore_dsn = _build_dsn("DOCSTORE_")

    console_engine = create_async_engine(console_dsn, echo=False)
    docstore_engine = create_async_engine(docstore_dsn, echo=False)

    ConsoleSession = async_sessionmaker(console_engine, expire_on_commit=False)
    DocstoreSession = async_sessionmaker(docstore_engine, expire_on_commit=False)

    try:
        async with DocstoreSession() as docstore_db:
            all_skills = await _get_all_docstore_skills(docstore_db)

        logger.info(f"Found {len(all_skills)} skill instances in docstore")

        async with ConsoleSession() as console_db:
            sub_agent_map = await _get_sub_agent_map(console_db)
            valid_users = await _get_user_ids(console_db)
            valid_groups = await _get_group_ids(console_db)

            # Track created registry entries to avoid duplicates
            # Key: (slug, source_context) → registry_id
            registry_cache: dict[str, str] = {}
            created_registry = 0
            created_activations = 0
            skipped = 0

            for skill_info in all_skills:
                prefix = skill_info["prefix"]
                agent_name = skill_info["agent_name"]
                skill_name = skill_info["skill_name"]
                files = skill_info["files"]

                # Resolve scope
                scope, user_id, group_id = _parse_scope_from_prefix(prefix)

                # Validate references
                sub_agent_id = sub_agent_map.get(agent_name)
                if not sub_agent_id:
                    logger.warning(f"  Skipping {skill_name} on {agent_name}: agent not found in sub_agents table")
                    skipped += 1
                    continue

                if scope == "personal" and user_id and user_id not in valid_users:
                    logger.warning(f"  Skipping {skill_name} on {agent_name}: user {user_id} not found")
                    skipped += 1
                    continue

                if scope == "group" and group_id and int(group_id) not in valid_groups:
                    logger.warning(f"  Skipping {skill_name} on {agent_name}: group {group_id} not found")
                    skipped += 1
                    continue

                # Compute content hash
                content_hash = _compute_content_hash(files)
                slug = _slugify(skill_name)

                # Check if registry entry already exists
                cache_key = f"{slug}:{content_hash}"
                if cache_key not in registry_cache:
                    # Check DB
                    existing = await console_db.execute(
                        text("SELECT id FROM skill_registry WHERE slug = :slug AND content_hash = :hash"),
                        {"slug": slug, "hash": content_hash},
                    )
                    existing_row = existing.first()

                    if existing_row:
                        registry_id = str(existing_row[0])
                        registry_cache[cache_key] = registry_id
                        logger.info(f"  Registry entry exists for {slug} (hash={content_hash[:8]})")
                    else:
                        if dry_run:
                            registry_id = f"dry-run-{slug}"
                            logger.info(f"  [DRY RUN] Would create registry entry: {slug}")
                        else:
                            # Extract description from SKILL.md frontmatter
                            skill_md = next((f for f in files if f["path"] == "SKILL.md"), None)
                            description = ""
                            if skill_md and skill_md["contents"].startswith("---"):
                                try:
                                    import yaml

                                    fm_end = skill_md["contents"].index("---", 3)
                                    fm = yaml.safe_load(skill_md["contents"][3:fm_end])
                                    description = fm.get("description", "")
                                except Exception:
                                    pass

                            # Determine creator — use user_id if personal, else 'system'
                            created_by = user_id if user_id else "system"

                            # Determine visibility and group
                            visibility = "group" if scope == "group" else "private"
                            group_ids_val = f"{{{group_id}}}" if group_id else None

                            result = await console_db.execute(
                                text("""
                                    INSERT INTO skill_registry (slug, name, description, source_type, files, content_hash, visibility, group_ids, owner_id, created_by)
                                    VALUES (:slug, :name, :description, 'nannos', :files, :content_hash, :visibility, :group_ids, :owner_id, :created_by)
                                    RETURNING id
                                """),
                                {
                                    "slug": slug,
                                    "name": skill_name,
                                    "description": description,
                                    "files": json.dumps(
                                        [{"path": f["path"], "contents": f["contents"]} for f in files]
                                    ),
                                    "content_hash": content_hash,
                                    "visibility": visibility,
                                    "group_ids": group_ids_val,
                                    "owner_id": created_by if created_by != "system" else None,
                                    "created_by": created_by,
                                },
                            )
                            registry_id = str(result.scalar_one())
                            created_registry += 1
                            logger.info(f"  Created registry entry: {slug} → {registry_id}")

                        registry_cache[cache_key] = registry_id
                else:
                    registry_id = registry_cache[cache_key]

                # Create activation record
                if dry_run:
                    logger.info(
                        f"  [DRY RUN] Would create activation: {skill_name} on {agent_name} "
                        f"(scope={scope}, user={user_id}, group={group_id})"
                    )
                    created_activations += 1
                else:
                    # Check if activation already exists
                    existing_activation = await console_db.execute(
                        text("""
                            SELECT id FROM skill_activations
                            WHERE sub_agent_id = :sub_agent_id
                              AND registry_id = :registry_id::uuid
                              AND scope = :scope
                              AND COALESCE(user_id, '') = COALESCE(:user_id, '')
                              AND COALESCE(group_id, 0) = COALESCE(:group_id, 0)
                        """),
                        {
                            "sub_agent_id": sub_agent_id,
                            "registry_id": registry_id,
                            "scope": scope,
                            "user_id": user_id,
                            "group_id": int(group_id) if group_id else None,
                        },
                    )
                    if existing_activation.first():
                        logger.info(f"  Activation already exists for {skill_name} on {agent_name}")
                        continue

                    activated_by = user_id if user_id and user_id in valid_users else "system"

                    await console_db.execute(
                        text("""
                            INSERT INTO skill_activations (sub_agent_id, registry_id, scope, user_id, group_id, content_hash, locked, activated_by)
                            VALUES (:sub_agent_id, :registry_id::uuid, :scope, :user_id, :group_id, :content_hash, FALSE, :activated_by)
                        """),
                        {
                            "sub_agent_id": sub_agent_id,
                            "registry_id": registry_id,
                            "scope": scope,
                            "user_id": user_id,
                            "group_id": int(group_id) if group_id else None,
                            "content_hash": content_hash,
                            "activated_by": activated_by,
                        },
                    )
                    created_activations += 1

            if not dry_run:
                await console_db.commit()

            logger.info("=" * 60)
            logger.info("Migration complete:")
            logger.info(f"  Registry entries created: {created_registry}")
            logger.info(f"  Activations created: {created_activations}")
            logger.info(f"  Skipped (invalid references): {skipped}")
            if dry_run:
                logger.info("  (DRY RUN — no changes were made)")

    finally:
        await console_engine.dispose()
        await docstore_engine.dispose()


async def _migrate_config_version_skills(dry_run: bool = False) -> None:
    """Migrate inline skills from sub_agent_config_versions to registry references.

    For each config version with inline skills[]:
    1. Find or create registry entry for each skill
    2. Create locked activation record
    """
    console_dsn = _build_dsn("")
    console_engine = create_async_engine(console_dsn, echo=False)
    ConsoleSession = async_sessionmaker(console_engine, expire_on_commit=False)

    try:
        async with ConsoleSession() as db:
            # Find config versions with non-empty skills
            result = await db.execute(
                text("""
                    SELECT cv.id, cv.sub_agent_id, cv.version, cv.skills, sa.name as agent_name
                    FROM sub_agent_config_versions cv
                    JOIN sub_agents sa ON sa.id = cv.sub_agent_id
                    WHERE cv.skills IS NOT NULL AND cv.skills != '[]'::jsonb
                    ORDER BY cv.sub_agent_id, cv.version
                """)
            )
            config_versions = result.mappings().all()

            logger.info(f"Found {len(config_versions)} config versions with inline skills")

            registry_cache: dict[str, str] = {}
            created_registry = 0
            created_activations = 0

            for cv in config_versions:
                cv_id = cv["id"]
                sub_agent_id = cv["sub_agent_id"]
                agent_name = cv["agent_name"]
                skills_json = cv["skills"]

                if not skills_json:
                    continue

                skills = skills_json if isinstance(skills_json, list) else json.loads(skills_json)

                for skill_def in skills:
                    name = skill_def.get("name", "")
                    description = skill_def.get("description", "")
                    body = skill_def.get("body", "")
                    inline_files = skill_def.get("files", [])

                    # Build files list in registry format
                    files = [{"path": "SKILL.md", "contents": body}]
                    for f in inline_files:
                        files.append({"path": f.get("path", ""), "contents": f.get("content", "")})

                    slug = _slugify(name)
                    content_hash = _compute_content_hash(files)
                    cache_key = f"{slug}:{content_hash}"

                    # Find or create registry entry
                    if cache_key not in registry_cache:
                        existing = await db.execute(
                            text("SELECT id FROM skill_registry WHERE slug = :slug AND content_hash = :hash"),
                            {"slug": slug, "hash": content_hash},
                        )
                        existing_row = existing.first()

                        if existing_row:
                            registry_id = str(existing_row[0])
                            registry_cache[cache_key] = registry_id
                        else:
                            if dry_run:
                                registry_id = f"dry-run-{slug}"
                                logger.info(f"  [DRY RUN] Would create registry entry for config skill: {slug}")
                            else:
                                result = await db.execute(
                                    text("""
                                        INSERT INTO skill_registry (slug, name, description, source_type, files, content_hash, visibility, created_by)
                                        VALUES (:slug, :name, :description, 'nannos', :files, :content_hash, 'group', 'system')
                                        RETURNING id
                                    """),
                                    {
                                        "slug": slug,
                                        "name": name,
                                        "description": description,
                                        "files": json.dumps(files),
                                        "content_hash": content_hash,
                                    },
                                )
                                registry_id = str(result.scalar_one())
                                created_registry += 1
                                logger.info(f"  Created registry entry for config skill: {slug} → {registry_id}")

                            registry_cache[cache_key] = registry_id
                    else:
                        registry_id = registry_cache[cache_key]

                    # Create locked activation
                    if dry_run:
                        logger.info(
                            f"  [DRY RUN] Would create locked activation: {name} on {agent_name} (config_version={cv_id})"
                        )
                    else:
                        # Check if locked activation already exists
                        existing_activation = await db.execute(
                            text("""
                                SELECT id FROM skill_activations
                                WHERE sub_agent_id = :sub_agent_id
                                  AND registry_id = :registry_id::uuid
                                  AND locked = TRUE
                                  AND config_version_id = :cv_id
                            """),
                            {"sub_agent_id": sub_agent_id, "registry_id": registry_id, "cv_id": cv_id},
                        )
                        if existing_activation.first():
                            continue

                        await db.execute(
                            text("""
                                INSERT INTO skill_activations (sub_agent_id, registry_id, scope, content_hash, locked, config_version_id, activated_by)
                                VALUES (:sub_agent_id, :registry_id::uuid, 'group', :content_hash, TRUE, :cv_id, 'system')
                            """),
                            {
                                "sub_agent_id": sub_agent_id,
                                "registry_id": registry_id,
                                "content_hash": content_hash,
                                "cv_id": cv_id,
                            },
                        )
                        created_activations += 1

            if not dry_run:
                await db.commit()

            logger.info("=" * 60)
            logger.info("Config version migration complete:")
            logger.info(f"  Registry entries created: {created_registry}")
            logger.info(f"  Locked activations created: {created_activations}")
            if dry_run:
                logger.info("  (DRY RUN — no changes were made)")

    finally:
        await console_engine.dispose()


async def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        logger.info("Running in DRY RUN mode — no changes will be made")

    logger.info("Phase 1: Migrating docstore skills → registry + activations")
    await _migrate_docstore_skills(dry_run=dry_run)

    logger.info("")
    logger.info("Phase 2: Migrating config version inline skills → registry + locked activations")
    await _migrate_config_version_skills(dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())
