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

from ringier_a2a_sdk.model_capabilities import RESPONSE_FORMAT, capabilities_of

from ..models.model_gateway import CHAT_TIER_ROLES, VALID_ROLES  # single source of truth for role keys
from ..models.user import User

from .model_gateway_service import ModelGatewayError

if TYPE_CHECKING:
    from ..repositories.model_defaults_repository import ModelDefaultsRepository
    from .model_gateway_service import ModelGatewayService

logger = logging.getLogger(__name__)


# The chat tiers whose default (and chain) must serve the harness's utility calls: every
# classifier, summarizer and risk-scorer call goes to the fast model (chat:low, falling back to
# chat), and those calls use ``response_format`` with no alternative shape. A model recorded as
# rejecting it would break each of them the moment it became the tier's default or was failed
# over to — so it is refused for those tiers, and only those. chat:premium is a user's explicit
# choice for a conversation and carries no utility traffic.
UTILITY_TIER_ROLES = ("chat", "chat:low")


def _require_utility_capable(role: str, alias: str, model_info: dict | None) -> None:
    """Refuse ``alias`` for a utility tier when its probe recorded ``response_format`` as
    unsupported (nannos#318). Unprobed deployments pass: the flag is knowledge, and its
    absence is not a verdict."""
    if role not in UTILITY_TIER_ROLES:
        return
    if capabilities_of(model_info).get(RESPONSE_FORMAT) is False:
        raise ValueError(
            f"'{alias}' cannot serve the '{role}' tier: its registration probe found it rejects "
            f"response_format, which every classifier and summarizer call on this tier sends. "
            f"Use it in chat:premium or as a user-selected model instead."
        )


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

    async def set_default(
        self, db: AsyncSession, actor: User, role: str, model_alias: str, *, model_info: dict | None = None
    ) -> None:
        """Upsert the default alias for a role (exactly one alias per role).

        Writes through the audited repository so the fleet-wide config change is recorded
        automatically (AGENTS.md repository-pattern rule). ``model_info`` is the deployment's
        gateway model_info, checked by ``_require_utility_capable`` for the tiers the
        harness's utility calls run on."""
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        _require_utility_capable(role, model_alias, model_info)
        await self.repository.upsert_default(db, actor=actor, role=role, model_alias=model_alias)

    async def utility_tiers_served_by(self, db: AsyncSession, alias: str) -> list[str]:
        """The utility tiers (see UTILITY_TIER_ROLES) ``alias`` serves right now, as default or
        as a chain member — what a fresh `response_format: false` record breaks (nannos#318)."""
        defaults = await self.get_all(db)
        chains = await self.repository.get_all_fallbacks(db)
        out = []
        for role in UTILITY_TIER_ROLES:
            if defaults.get(role) == alias:
                out.append(f"{role} (default)")
            elif alias in chains.get(role, []):
                out.append(f"{role} (failover chain)")
        return out

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
        defaults = await self.get_all(db)
        head = defaults.get(role)
        if not head:
            raise ValueError(f"Tier '{role}' has no default model; set one before giving it a failover chain.")
        # The gateway holds ONE chain per default alias. A second tier with the same default and
        # a different non-empty chain cannot both be honoured, so refuse rather than store a
        # chain the gateway will never hold and report it as saved.
        if aliases:
            chains = await self.repository.get_all_fallbacks(db)
            for other in CHAT_TIER_ROLES:
                other_chain = chains.get(other) or []
                if other != role and defaults.get(other) == head and other_chain and other_chain != list(aliases):
                    raise ValueError(
                        f"'{head}' also defaults the '{other}' tier, whose chain is {other_chain}. The gateway "
                        f"holds one chain per default model: give both tiers the same chain, or different defaults."
                    )

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
        # `or "chat"`, not `.get("mode", "chat")`: the default only applies when the key is
        # ABSENT, and deployments exist whose model_info carries an explicit null (models_router
        # guards for the same falsy case). Treating null as "not chat" would have the picker
        # offer a candidate this then rejects.
        chat_aliases = {
            m.get("model_name") for m in deployments if ((m.get("model_info") or {}).get("mode") or "chat") == "chat"
        }
        not_chat = [a for a in aliases if a not in chat_aliases]
        if not_chat:
            raise ValueError(
                f"Not chat models: {', '.join(sorted(not_chat))}. A chat tier may only fail over to chat models."
            )
        # A chain member serves the tier's traffic when failover lands on it — including the
        # utility calls the tier's default was vetted for.
        infos = {m.get("model_name"): m.get("model_info") or {} for m in deployments}
        for alias in aliases:
            _require_utility_capable(role, alias, infos.get(alias))

        await self.repository.replace_fallbacks(db, actor=actor, role=role, aliases=aliases)
        await self.project_chains(db, gateway=gateway)
        return [head, *aliases]

    async def project_chains(self, db: AsyncSession, *, gateway: "ModelGatewayService") -> dict[str, list[str]]:
        """Declare every chat tier's chain on the proxy, from our table, in one write.

        The proxy keys a chain on its head alias, so the declaration is head → chain. One
        alias may default several tiers at once, but the proxy holds one chain per head: a
        non-empty chain wins over an empty one (a tier with no chain asks for nothing, so it
        must not cancel another tier's), and between two different non-empty chains — which
        ``set_failover_chain`` refuses, but a default change can still create — the first tier in
        ``CHAT_TIER_ROLES`` order wins and the conflict is logged; the tier listing's drift
        check shows the other tier as not what the gateway holds. Returns what was declared.
        """
        defaults = await self.get_all(db)
        chains = await self.repository.get_all_fallbacks(db)
        declared: dict[str, list[str]] = {}
        for role in CHAT_TIER_ROLES:
            head = defaults.get(role)
            if not head:
                continue
            chain = [a for a in chains.get(role, []) if a != head]
            if head in declared:
                if not declared[head]:
                    declared[head] = chain
                elif chain and declared[head] != chain:
                    logger.warning(
                        "'%s' defaults several tiers with different chains; the gateway holds one chain per "
                        "alias, so %s's chain %s is not declared (keeping %s)",
                        head, role, chain, declared[head],
                    )
                continue
            declared[head] = chain
        await gateway.set_all_fallbacks(declared)
        return declared

    async def reproject_tier_group(
        self,
        db: AsyncSession,
        role: str,
        *,
        actor: User,
        gateway: "ModelGatewayService",
    ) -> None:
        """Re-declare the chains on the proxy after a tier's *default* changed.

        Proxy-side a chain is keyed on its head alias, so re-pointing a tier's default moves
        the chain: the new head gets it and the old head, unless it still defaults another
        tier, stops failing over. Both fall out of declaring every chain from our table in one
        write (``project_chains``) — there is no per-entry delete left to get wrong.
        """
        if role not in CHAT_TIER_ROLES:
            return
        head = (await self.get_all(db)).get(role)
        if head:
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
        await self.project_chains(db, gateway=gateway)

    async def drop_alias_from_chains(
        self, db: AsyncSession, actor: User, alias: str, *, gateway: "ModelGatewayService"
    ) -> list[str]:
        """Remove a retired alias from every failover chain and reproject the affected tiers.

        Deleting a deployment otherwise leaves it named in the chains — both in our table and on
        the proxy — so the gateway would fail over to a model it no longer serves, breaking at
        precisely the moment the primary is down. The stored registration check only catches it
        on the next manual edit, which may never come.

        Returns the roles that changed. The table is cleaned up whatever the proxy says; a failed
        projection is logged and shows as drift until the next chain write re-declares them all.
        """
        chains = await self.repository.get_all_fallbacks(db)
        changed: list[str] = []
        for role, chain in chains.items():
            if alias not in chain:
                continue
            await self.repository.replace_fallbacks(
                db, actor=actor, role=role, aliases=[a for a in chain if a != alias]
            )
            changed.append(role)
        if changed:
            try:
                await self.project_chains(db, gateway=gateway)
            except ModelGatewayError as e:
                logger.error("Removed '%s' from tiers %s but could not reproject: %s", alias, changed, e)
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
