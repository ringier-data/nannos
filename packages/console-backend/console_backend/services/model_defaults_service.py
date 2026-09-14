"""Per-role default model aliases (graceful degradation).

Authoritative store for the fleet default chat / embedding / multimodal-embedding
model. Lives here (not in the gateway model_info) because LiteLLM's /model/update
can't persist a custom default flag — only /model/new can — so a DB-backed,
runtime-editable record is the robust home. The apps read these via the unauthenticated
in-cluster /api/v1/models/defaults endpoint and fall back to them when a referenced
alias has been retired.
"""

import logging
from typing import TYPE_CHECKING, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.model_gateway import CHAT_TIER_ROLES, VALID_ROLES  # single source of truth for role keys
from ..models.user import User

from .model_gateway_service import ModelGatewayError

if TYPE_CHECKING:
    from ..repositories.model_defaults_repository import ModelDefaultsRepository
    from .model_gateway_service import ModelGatewayService

logger = logging.getLogger(__name__)


class ModelDefaultsService:
    def __init__(self) -> None:
        self._repository: Optional["ModelDefaultsRepository"] = None

    def set_repository(self, repository: "ModelDefaultsRepository") -> None:
        """Inject the repository (writes go through it so audit logging is automatic)."""
        self._repository = repository

    @property
    def repository(self) -> "ModelDefaultsRepository":
        if self._repository is None:
            raise RuntimeError("ModelDefaultsRepository not injected. Call set_repository() during init.")
        return self._repository

    async def get_all(self, db: AsyncSession) -> dict[str, str]:
        """{role: model_alias} for every role that has a default set."""
        return await self.repository.get_all(db)

    async def get_alias_tiers(self, db: AsyncSession) -> dict[str, str]:
        """{alias: chat-tier role} — the most-recent chat tier each alias served as default."""
        return await self.repository.get_alias_tiers(db)

    async def set_default(self, db: AsyncSession, actor: User, role: str, model_alias: str) -> None:
        """Upsert the default alias for a role (exactly one alias per role).

        Writes through the audited repository so the fleet-wide config change is recorded
        automatically (AGENTS.md repository-pattern rule)."""
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        await self.repository.upsert_default(db, actor=actor, role=role, model_alias=model_alias)

    # --- Tier groups (nannos#204) --------------------------------------------------------

    async def get_tier_group(self, db: AsyncSession, role: str) -> list[str]:
        """The full tier group for a chat tier: [default, *failover chain].

        The head comes from model_defaults and the tail from model_tier_fallbacks, so a tier
        with no default has no group at all — there is nothing for a chain to fall back
        *from*, and the proxy keys its fallbacks on the head alias.
        """
        self._require_chat_tier(role)
        head = (await self.get_all(db)).get(role)
        if not head:
            return []
        return [head, *await self.repository.get_fallbacks(db, role)]

    async def get_all_tier_groups(self, db: AsyncSession) -> dict[str, list[str]]:
        """{chat tier role: [default, *failover chain]} for every tier that has a default."""
        defaults = await self.get_all(db)
        chains = await self.repository.get_all_fallbacks(db)
        return {role: [defaults[role], *chains.get(role, [])] for role in CHAT_TIER_ROLES if defaults.get(role)}

    async def set_failover_chain(
        self,
        db: AsyncSession,
        actor: User,
        role: str,
        aliases: list[str],
        *,
        gateway: "ModelGatewayService",
    ) -> list[str]:
        """Replace a chat tier's failover chain and project it onto the gateway.

        ``aliases`` is the tail only — the head is the tier's current default. Returns the
        resulting full tier group.

        The DB write and the proxy projection are two systems, so they cannot be made atomic.
        We write ours first and project second, and let the projection's failure surface to
        the caller: a chain recorded but not projected is a *missing* failover (the status quo
        before this feature), whereas projecting first and failing to record would leave the
        proxy routing somewhere the console cannot show or revoke.
        """
        self._require_chat_tier(role)
        head = (await self.get_all(db)).get(role)
        if not head:
            raise ValueError(f"Tier '{role}' has no default model; set one before giving it a failover chain.")

        seen: set[str] = {head}
        for alias in aliases:
            if alias in seen:
                raise ValueError(
                    f"'{alias}' appears twice in the '{role}' tier group; a chain must not "
                    f"revisit a model it has already tried."
                )
            seen.add(alias)

        deployments = await gateway.list_models()
        registered = {m.get("model_name") for m in deployments}
        unknown = [a for a in aliases if a not in registered]
        if unknown:
            raise ValueError(
                f"Not registered on the gateway: {', '.join(sorted(unknown))}. "
                f"Register a model before routing traffic to it."
            )
        # Mode, not just existence: _require_chat_tier guards the tier's ROLE, but said nothing
        # about the chain's members, so an API caller could route chat traffic onto an embedding
        # deployment — which fails hard at the moment the chain is finally needed.
        chat_aliases = {
            m.get("model_name") for m in deployments if (m.get("model_info") or {}).get("mode", "chat") == "chat"
        }
        not_chat = [a for a in aliases if a not in chat_aliases]
        if not_chat:
            raise ValueError(
                f"Not chat models: {', '.join(sorted(not_chat))}. A chat tier may only fail over to chat models."
            )

        await self.repository.replace_fallbacks(db, actor=actor, role=role, aliases=aliases)
        await gateway.set_fallbacks(head, aliases)
        return [head, *aliases]

    async def reproject_tier_group(
        self,
        db: AsyncSession,
        role: str,
        *,
        actor: User,
        gateway: "ModelGatewayService",
        previous_head: str | None = None,
    ) -> None:
        """Re-declare a tier's chain on the proxy after its *default* changed.

        Proxy-side a chain is keyed on its head alias, so re-pointing a tier's default has to
        move the chain rather than add a second one. Both halves matter: without re-writing,
        the new default has no chain at all; without deleting, the old head keeps failing
        over long after it stopped being anyone's default.

        ``previous_head`` is only dropped when it is no longer the default of *any* chat tier
        — one alias may serve several tiers at once (``model_alias_tiers`` exists precisely
        because of that), and deleting its chain would silently disarm the tier still using it.
        """
        if role not in CHAT_TIER_ROLES:
            return
        defaults = await self.get_all(db)
        head = defaults.get(role)
        if previous_head and previous_head != head:
            still_in_use = any(defaults.get(other) == previous_head for other in CHAT_TIER_ROLES)
            if not still_in_use:
                await gateway.delete_fallbacks(previous_head)
        if not head:
            return
        chain = await self.repository.get_fallbacks(db, role)
        if head in chain:
            # The alias just promoted to default was already in this tier's chain. Projecting it
            # unchanged would declare a chain that falls back from the head to itself — burning a
            # hop on the provider just found unavailable — and would then make every later edit
            # 400, since set_failover_chain rejects a chain containing the head. Drop it here and
            # persist the correction so the console and the proxy agree.
            chain = [a for a in chain if a != head]
            await self.repository.replace_fallbacks(db, actor=actor, role=role, aliases=chain)
            logger.info("Removed newly-promoted default '%s' from the '%s' failover chain", head, role)
        await gateway.set_fallbacks(head, chain)

    async def drop_alias_from_chains(
        self, db: AsyncSession, actor: User, alias: str, *, gateway: "ModelGatewayService"
    ) -> list[str]:
        """Remove a retired alias from every failover chain and reproject the affected tiers.

        Deleting a deployment otherwise leaves it named in the chains — both in our table and on
        the proxy — so the gateway would fail over to a model it no longer serves, breaking at
        precisely the moment the primary is down. The stored registration check only catches it
        on the next manual edit, which may never come.

        Returns the roles that changed. Best-effort per tier: one unprojectable tier must not
        stop the others from being cleaned up.
        """
        chains = await self.repository.get_all_fallbacks(db)
        defaults = await self.get_all(db)
        changed: list[str] = []
        for role, chain in chains.items():
            if alias not in chain:
                continue
            await self.repository.replace_fallbacks(
                db, actor=actor, role=role, aliases=[a for a in chain if a != alias]
            )
            changed.append(role)
            head = defaults.get(role)
            if not head:
                continue
            try:
                await gateway.set_fallbacks(head, [a for a in chain if a != alias])
            except ModelGatewayError as e:
                logger.error("Removed '%s' from tier '%s' but could not reproject: %s", alias, role, e)
        if changed:
            logger.info("Removed retired alias '%s' from failover chains: %s", alias, changed)
        return changed

    @staticmethod
    def _require_chat_tier(role: str) -> None:
        """Tier groups are a chat-only feature, and the refusal is deliberate rather than an
        oversight: failing an embedding call over to a different model writes vectors from
        another embedding space into the same pgvector index. They insert cleanly (both sides
        are pinned to EMBEDDING_DIMENSION) and silently poison similarity search for every
        document embedded during the outage, permanently. For a chat turn another provider's
        answer beats an error; for an embedding write an error beats a corrupted index."""
        if role not in CHAT_TIER_ROLES:
            raise ValueError(
                f"Failover chains are only supported for chat tiers ({', '.join(CHAT_TIER_ROLES)}); "
                f"'{role}' must not fail over."
            )
