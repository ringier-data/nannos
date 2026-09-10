"""Embed bindings (ADR-0006): keep a sub-agent in sync with a host-published definition
and bind arriving users to it by their token's `azp`.

Three jobs:

* **Admin**: upsert / read / delete the binding (base URL + azp list) of a sub-agent.
  Writes go through `EmbedBindingRepository` and are audited on the sub-agent.
* **Sync**: fetch the host's `/.well-known/agent-skills/` tree, and when its revision
  changed write ONE new approved config version (`version_hash = wk<rev>`) so the
  orchestrator's caches roll over by themselves. Runs at startup, on a timer, on every
  binding change, and on the admin's refresh button.
* **Connect**: when a socket authenticates with a token whose `azp` is bound, activate the
  user for the sub-agent (`activated_by = embed`) and hand back the id to stamp on the
  socket session. handle_send_message reads the stamp, never the client payload.

The entity stays a normal sub-agent. Its content is derived, which is why bound sub-agents
are read-only in the console except for the binding itself.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from console_backend.config import config
from console_backend.models.embed_binding import (
    EmbedBinding,
    EmbedBindingProbe,
    EmbedBindingUpsert,
    WellKnownAgentInfo,
    WellKnownSkillInfo,
    is_loopback_host,
    normalize_base_url,
)
from console_backend.models.sub_agent import (
    ActivationSource,
    SkillDefinition,
    SubAgentType,
)
from console_backend.models.user import User
from console_backend.repositories.embed_binding_repository import EmbedBindingRepository
from console_backend.services.well_known_agent import (
    WELL_KNOWN_INDEX_PATH,
    WellKnownAgentClient,
    WellKnownDefinition,
    WellKnownFetchError,
    compose_system_prompt,
    thinking_params,
    version_hash_for,
)

if TYPE_CHECKING:
    from console_backend.services.sub_agent_service import SubAgentService
    from console_backend.services.user_service import UserService

logger = logging.getLogger(__name__)

#: azp → sub_agent_id lookups happen on every embedded socket connect and every
#: conversation list; a short cache keeps them off the database without letting an
#: admin's binding change go unnoticed for long.
_AZP_CACHE_TTL_SECONDS = 60.0

_SYSTEM_USER_ID = "system"

_SELECT_BINDING = """
    SELECT b.sub_agent_id, b.base_url, b.revision, b.definition, b.fetched_at,
           b.last_error, b.last_error_at, b.last_seen_at, b.azps_seen,
           b.created_by, b.created_at, b.updated_at,
           COALESCE(array_agg(a.azp ORDER BY a.azp) FILTER (WHERE a.azp IS NOT NULL), '{}') AS azps
      FROM sub_agent_embed_bindings b
      LEFT JOIN sub_agent_embed_binding_azps a ON a.sub_agent_id = b.sub_agent_id
"""


class EmbedBindingError(ValueError):
    """A binding request that cannot be honoured (bad URL, azp already bound, wrong agent type)."""


class EmbedBindingService:
    def __init__(
        self,
        sub_agent_service: "SubAgentService",
        user_service: "UserService",
        session_factory: async_sessionmaker[AsyncSession],
        client: WellKnownAgentClient | None = None,
        repository: EmbedBindingRepository | None = None,
    ) -> None:
        self._sub_agents = sub_agent_service
        self._users = user_service
        self._session_factory = session_factory
        # Binding writes go through the audited repository (AGENTS.md: every write with
        # business meaning is audited). It needs its AuditService injected — done in
        # service_instances.py; a bare default fails loudly on the first write.
        self._repo = repository or EmbedBindingRepository()
        # Local development binds localhost or a docker network; everywhere else the
        # authority must resolve to public addresses (SSRF guard in the client).
        self._client = client or WellKnownAgentClient(
            allow_private_destinations=config.is_local()
        )
        # azp -> (monotonic expiry, sub_agent_id or None)
        self._azp_cache: dict[str, tuple[float, int | None]] = {}
        # sub_agent_id -> last error text already logged at WARNING
        self._logged_errors: dict[int, str] = {}

    # ------------------------------------------------------------------ reads

    async def get_binding(
        self, db: AsyncSession, sub_agent_id: int
    ) -> EmbedBinding | None:
        result = await db.execute(
            text(
                _SELECT_BINDING + " WHERE b.sub_agent_id = :id GROUP BY b.sub_agent_id"
            ),
            {"id": sub_agent_id},
        )
        row = result.mappings().first()
        return _row_to_binding(row) if row else None

    async def list_bindings(self, db: AsyncSession) -> list[EmbedBinding]:
        result = await db.execute(
            text(_SELECT_BINDING + " GROUP BY b.sub_agent_id ORDER BY b.sub_agent_id")
        )
        return [_row_to_binding(row) for row in result.mappings().all()]

    async def is_bound(self, db: AsyncSession, sub_agent_id: int) -> bool:
        result = await db.execute(
            text("SELECT 1 FROM sub_agent_embed_bindings WHERE sub_agent_id = :id"),
            {"id": sub_agent_id},
        )
        return result.first() is not None

    async def sub_agent_id_for_azp(
        self, azp: str | None, db: AsyncSession | None = None
    ) -> int | None:
        """The sub-agent bound to this token `azp`, or None. Cached for a minute."""
        if not azp:
            return None
        cached = self._azp_cache.get(azp)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        if db is not None:
            sub_agent_id = await self._lookup_azp(db, azp)
        else:
            async with self._session_factory() as own_db:
                sub_agent_id = await self._lookup_azp(own_db, azp)
        self._azp_cache[azp] = (time.monotonic() + _AZP_CACHE_TTL_SECONDS, sub_agent_id)
        return sub_agent_id

    async def _lookup_azp(self, db: AsyncSession, azp: str) -> int | None:
        result = await db.execute(
            text(
                "SELECT sub_agent_id FROM sub_agent_embed_binding_azps WHERE azp = :azp"
            ),
            {"azp": azp},
        )
        row = result.first()
        return int(row[0]) if row else None

    # ------------------------------------------------------------------ admin

    async def upsert_binding(
        self, db: AsyncSession, actor: User, sub_agent_id: int, data: EmbedBindingUpsert
    ) -> EmbedBinding:
        """Create or replace the binding, then sync immediately so a bad URL fails in the same request."""
        self._validate_base_url(data.base_url)
        sub_agent = await self._sub_agents.get_sub_agent_by_id(db, sub_agent_id)
        if sub_agent is None:
            raise LookupError(f"Sub-agent {sub_agent_id} not found")
        if sub_agent.type != SubAgentType.LOCAL:
            raise EmbedBindingError(
                f"Only local sub-agents can be bound to a host; sub-agent {sub_agent_id} is {sub_agent.type.value}"
            )

        await self._refuse_taken_azps(db, data.azps, exclude_sub_agent_id=sub_agent_id)
        await self._write_binding_rows(db, actor, sub_agent_id, data)
        return await self.sync_binding(db, sub_agent_id, force=True)

    async def create_bound_sub_agent(
        self, db: AsyncSession, actor: User, data: EmbedBindingUpsert
    ) -> EmbedBinding:
        """Create a local sub-agent FROM the host's published definition and bind it.

        Unlike :meth:`upsert_binding`, the definition is fetched *before* anything is
        written: without it there is no name to create the sub-agent with, so a bad URL
        fails the request and leaves no half-made sub-agent behind. The row is created
        without a version and the first publish writes version 1 as the approved default,
        all in the caller's transaction.
        """
        self._validate_base_url(data.base_url)
        await self._refuse_taken_azps(db, data.azps, exclude_sub_agent_id=None)
        try:
            definition = await self._client.fetch(data.base_url, force=True)
        except WellKnownFetchError as e:
            raise EmbedBindingError(
                f"Could not read the host definition at {data.base_url}{WELL_KNOWN_INDEX_PATH}: {e}"
            ) from e

        # The row name is the derived one: it doubles as the orchestrator's task-tool
        # identifier, so it cannot hold the host's display name verbatim.
        sub_agent_id = await self._sub_agents.create_managed_sub_agent(
            db, actor, name=definition.agent.sub_agent_name
        )
        await self._write_binding_rows(db, actor, sub_agent_id, data)
        binding = await self.get_binding(db, sub_agent_id)
        assert binding is not None
        await self._apply_definition(
            db, binding, definition, datetime.now(timezone.utc)
        )
        logger.info(
            f"Embed binding: created sub-agent {sub_agent_id} "
            f"'{definition.agent.sub_agent_name}' (published as '{definition.agent.name}') "
            f"from {data.base_url} by {actor.id}"
        )
        refreshed = await self.get_binding(db, sub_agent_id)
        assert refreshed is not None
        return refreshed

    async def probe(self, base_url: str) -> EmbedBindingProbe:
        """Read an authority and report what it publishes. Writes nothing.

        Every failure — a malformed origin, an unreachable host, a digest mismatch —
        comes back as ``ok=False`` with the reason, so the admin can fix the URL before
        creating anything. A successful probe warms the fetch cache the create call uses.
        """
        raw = (base_url or "").strip()
        try:
            normalized = normalize_base_url(raw)
            self._validate_base_url(normalized)
        except ValueError as e:
            return EmbedBindingProbe(
                ok=False,
                base_url=raw,
                index_url=raw.rstrip("/") + WELL_KNOWN_INDEX_PATH,
                error=str(e),
            )

        index_url = normalized + WELL_KNOWN_INDEX_PATH
        try:
            definition = await self._client.fetch(normalized, force=True)
        except WellKnownFetchError as e:
            logger.info(f"Embed binding probe of {normalized} failed: {e}")
            return EmbedBindingProbe(
                ok=False, base_url=normalized, index_url=index_url, error=str(e)
            )

        summary = _definition_summary(definition)
        return EmbedBindingProbe(
            ok=True,
            base_url=normalized,
            index_url=definition.index_url,
            agent=WellKnownAgentInfo(**summary["agent"]),
            skills=[WellKnownSkillInfo(**skill) for skill in summary["skills"]],
            revision=definition.revision,
        )

    async def _refuse_taken_azps(
        self, db: AsyncSession, azps: list[str], *, exclude_sub_agent_id: int | None
    ) -> None:
        """An azp selects exactly one sub-agent; refuse any that another binding already claims."""
        taken = await db.execute(
            text(
                "SELECT azp, sub_agent_id FROM sub_agent_embed_binding_azps "
                "WHERE azp = ANY(:azps) AND sub_agent_id IS DISTINCT FROM :id"
            ),
            {"azps": azps, "id": exclude_sub_agent_id},
        )
        conflicts = [f"'{r[0]}' (sub-agent {r[1]})" for r in taken.all()]
        if conflicts:
            raise EmbedBindingError(
                "azp already bound to another sub-agent: " + ", ".join(conflicts)
            )

    async def _write_binding_rows(
        self, db: AsyncSession, actor: User, sub_agent_id: int, data: EmbedBindingUpsert
    ) -> None:
        """Insert or replace the binding row and its azp rows (audited). No fetch, no commit."""
        await self._repo.write_binding(
            db, actor, sub_agent_id, base_url=data.base_url, azps=data.azps
        )
        self._azp_cache.clear()
        logger.info(
            f"Embed binding for sub-agent {sub_agent_id} set to {data.base_url} azps={data.azps} by {actor.id}"
        )

    async def delete_binding(self, db: AsyncSession, actor: User, sub_agent_id: int) -> bool:
        deleted = await self._repo.delete_binding(db, actor, sub_agent_id)
        self._azp_cache.clear()
        self._logged_errors.pop(sub_agent_id, None)
        if deleted:
            logger.info(
                f"Embed binding for sub-agent {sub_agent_id} removed by {actor.id}; "
                "the sub-agent is editable again"
            )
        return deleted

    def _validate_base_url(self, base_url: str) -> None:
        if base_url.startswith("http://") and not (
            is_loopback_host(base_url) and config.is_local()
        ):
            raise EmbedBindingError(
                "base_url must use https:// (http:// is allowed for localhost in local development only)"
            )

    # ------------------------------------------------------------------- sync

    async def sync_binding(
        self, db: AsyncSession, sub_agent_id: int, *, force: bool = False
    ) -> EmbedBinding:
        """Fetch the host definition; write a new approved version when its revision changed."""
        binding = await self.get_binding(db, sub_agent_id)
        if binding is None:
            raise LookupError(f"Sub-agent {sub_agent_id} has no embed binding")
        now = datetime.now(timezone.utc)
        try:
            definition = await self._client.fetch(binding.base_url, force=force)
        except WellKnownFetchError as e:
            await db.execute(
                text(
                    "UPDATE sub_agent_embed_bindings SET last_error = :err, last_error_at = :now, updated_at = :now "
                    "WHERE sub_agent_id = :id"
                ),
                {"err": str(e), "now": now, "id": sub_agent_id},
            )
            if self._logged_errors.get(sub_agent_id) != str(e):
                self._logged_errors[sub_agent_id] = str(e)
                logger.warning(
                    f"Embed binding sync failed for sub-agent {sub_agent_id} ({binding.base_url}): {e}"
                )
            refreshed = await self.get_binding(db, sub_agent_id)
            assert refreshed is not None
            return refreshed

        self._logged_errors.pop(sub_agent_id, None)
        await self._apply_definition(db, binding, definition, now)
        refreshed = await self.get_binding(db, sub_agent_id)
        assert refreshed is not None
        return refreshed

    async def _apply_definition(
        self,
        db: AsyncSession,
        binding: EmbedBinding,
        definition: WellKnownDefinition,
        now: datetime,
    ) -> None:
        """Record a successful fetch; write a new approved version when the revision changed."""
        sub_agent_id = binding.sub_agent_id
        if definition.revision == binding.revision:
            await db.execute(
                text(
                    "UPDATE sub_agent_embed_bindings SET fetched_at = :now, last_error = NULL, last_error_at = NULL, "
                    "updated_at = :now WHERE sub_agent_id = :id"
                ),
                {"now": now, "id": sub_agent_id},
            )
        else:
            new_version = await self._publish_version(
                db, sub_agent_id, binding.created_by, definition
            )
            await db.execute(
                text(
                    "UPDATE sub_agent_embed_bindings SET revision = :revision, definition = CAST(:definition AS jsonb), "
                    "fetched_at = :now, last_error = NULL, last_error_at = NULL, updated_at = :now WHERE sub_agent_id = :id"
                ),
                {
                    "revision": definition.revision,
                    "definition": json.dumps(_definition_summary(definition)),
                    "now": now,
                    "id": sub_agent_id,
                },
            )
            logger.info(
                f"Embed binding sub-agent {sub_agent_id}: synced revision {definition.revision} from "
                f"{binding.base_url} as version {new_version} ({version_hash_for(definition.revision)}), "
                f"{len(definition.skills)} skill(s), "
                f"{'Nannos-side' if definition.agent.tools is None else len(definition.agent.tools)} tool(s)"
            )

    async def _publish_version(
        self,
        db: AsyncSession,
        sub_agent_id: int,
        created_by: str,
        definition: WellKnownDefinition,
    ) -> int:
        actor = await self._actor_for(db, created_by)
        agent = definition.agent
        enable_thinking, thinking_level = thinking_params(agent.thinking_level)
        skills = [
            SkillDefinition(
                name=s.name,
                description=s.description,
                body=s.body,
                files=[],
                scope="sub-agent",
            )
            for s in definition.skills
        ]
        return await self._sub_agents.publish_managed_version(
            db,
            actor,
            sub_agent_id,
            version_hash=version_hash_for(definition.revision),
            change_summary=f"well-known revision {definition.revision} from {definition.base_url}",
            description=agent.description,
            system_prompt=compose_system_prompt(
                definition.base_url, agent, definition.revision
            ),
            mcp_tools=list(agent.tools) if agent.tools is not None else None,
            model_tier=agent.model_tier,
            enable_thinking=enable_thinking,
            thinking_level=thinking_level,
            skills=skills,
        )

    async def _actor_for(self, db: AsyncSession, user_id: str) -> User:
        """The admin who created the binding signs the synced versions; the seeded system user if they are gone."""
        actor = await self._users.get_user(db, user_id)
        if actor is None:
            actor = await self._users.get_user(db, _SYSTEM_USER_ID)
        if actor is None:
            raise LookupError(
                f"Neither user {user_id} nor the system user exists to sign the synced version"
            )
        return actor

    async def sync_all(self) -> None:
        """One pass over every binding, each in its own transaction so one bad host cannot block the rest."""
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT sub_agent_id FROM sub_agent_embed_bindings ORDER BY sub_agent_id"
                    )
                )
            ).all()
        for (sub_agent_id,) in rows:
            async with self._session_factory() as db:
                try:
                    await self.sync_binding(db, int(sub_agent_id))
                    await db.commit()
                except Exception:  # noqa: BLE001 — isolate per binding, the loop must go on
                    await db.rollback()
                    logger.exception(
                        f"Embed binding sync crashed for sub-agent {sub_agent_id}"
                    )

    # ---------------------------------------------------------------- connect

    async def bind_connection(
        self, db: AsyncSession, user: User, azp: str | None
    ) -> int | None:
        """Called from the socket token path. Activates the user for the bound sub-agent and
        records the sighting. Returns the sub-agent id to stamp on the socket session, or None
        when the token's `azp` is not bound."""
        sub_agent_id = await self.sub_agent_id_for_azp(azp, db)
        if sub_agent_id is None or azp is None:
            return None
        await self._sub_agents.repo.bulk_activate_sub_agent(
            db=db,
            actor=user,
            user_ids=[user.id],
            sub_agent_id=sub_agent_id,
            activated_by=ActivationSource.EMBED,
        )
        now = datetime.now(timezone.utc)
        await db.execute(
            text(
                "UPDATE sub_agent_embed_bindings SET last_seen_at = :now, "
                "azps_seen = azps_seen || jsonb_build_object(CAST(:azp AS text), CAST(:now_iso AS text)) "
                "WHERE sub_agent_id = :id"
            ),
            {"now": now, "azp": azp, "now_iso": now.isoformat(), "id": sub_agent_id},
        )
        return sub_agent_id


def _definition_summary(definition: WellKnownDefinition) -> dict[str, Any]:
    """What the admin view shows about a revision. Bodies live in the config version, not here."""
    agent = definition.agent
    return {
        "agent": WellKnownAgentInfo(
            name=agent.name,
            description=agent.description,
            organization=agent.organization,
            prompt_url=agent.url,
            prompt_digest=agent.digest,
            tools=list(agent.tools) if agent.tools is not None else None,
            model_tier=agent.model_tier,
            thinking_level=agent.thinking_level,
        ).model_dump(),
        "skills": [
            WellKnownSkillInfo(name=s.name, url=s.url, digest=s.digest).model_dump()
            for s in definition.skills
        ],
    }


def _row_to_binding(row: Any) -> EmbedBinding:
    definition = row["definition"]
    if isinstance(definition, str):
        definition = json.loads(definition)
    definition = definition or {}
    azps_seen = row["azps_seen"]
    if isinstance(azps_seen, str):
        azps_seen = json.loads(azps_seen)
    revision = row["revision"]
    return EmbedBinding(
        sub_agent_id=row["sub_agent_id"],
        base_url=row["base_url"],
        index_url=row["base_url"].rstrip("/") + WELL_KNOWN_INDEX_PATH,
        azps=list(row["azps"] or []),
        revision=revision,
        version_hash=version_hash_for(revision) if revision else None,
        fetched_at=row["fetched_at"],
        last_error=row["last_error"],
        last_error_at=row["last_error_at"],
        last_seen_at=row["last_seen_at"],
        azps_seen=dict(azps_seen or {}),
        agent=WellKnownAgentInfo(**definition["agent"])
        if definition.get("agent")
        else None,
        skills=[WellKnownSkillInfo(**s) for s in definition.get("skills", [])],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
