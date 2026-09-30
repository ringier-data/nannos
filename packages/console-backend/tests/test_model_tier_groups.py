"""Tier groups: a chat tier's ordered models (default + failover chain) — nannos#204.

What's worth pinning here is the boundary behaviour, not the CRUD:

- the chain is *chat-only*, and the refusal for embedding roles is a safety property (a
  failed-over embedding call poisons the pgvector index silently), so it is asserted rather
  than left to documentation;
- the chain is stored by us and executed by LiteLLM, which means two systems that cannot be
  written atomically — the ordering of those writes, and what happens when the second fails,
  is the actual design;
- a tier's chain is keyed proxy-side on its *head* alias, so re-pointing a tier's default has
  to move the chain and drop the old head — unless that head still serves another tier.
"""

import pytest
from console_backend.services.model_defaults_service import ModelDefaultsService
from console_backend.services.model_gateway_service import ModelGatewayError


class _FakeRepo:
    """In-memory stand-in for ModelDefaultsRepository (role → alias, role → chain)."""

    def __init__(self, defaults=None, chains=None):
        self.defaults = dict(defaults or {})
        self.chains = {k: list(v) for k, v in (chains or {}).items()}
        self.replaced: list[tuple[str, list[str]]] = []

    async def get_all(self, db):
        return dict(self.defaults)

    async def get_all_fallbacks(self, db):
        return {k: list(v) for k, v in self.chains.items()}

    async def get_fallbacks(self, db, role):
        return list(self.chains.get(role, []))

    async def replace_fallbacks(self, db, actor, role, aliases):
        self.chains[role] = list(aliases)
        self.replaced.append((role, list(aliases)))


class _FakeGateway:
    """Records each whole-list declaration (head → chain) sent to the proxy; can be made to fail."""

    def __init__(self, registered=(), fail_on_set=False):
        self.registered = list(registered)
        self.fail_on_set = fail_on_set
        self.declared: list[dict[str, list[str]]] = []

    async def list_models(self):
        return [{"model_name": name} for name in self.registered]

    async def set_all_fallbacks(self, chains):
        if self.fail_on_set:
            raise ModelGatewayError("proxy said no")
        self.declared.append({k: list(v) for k, v in chains.items()})


def _service(repo):
    svc = ModelDefaultsService()
    svc.set_repository(repo)
    return svc


_DB = object()  # the fakes never touch it


@pytest.mark.asyncio
async def test_tier_group_is_the_default_followed_by_its_chain():
    repo = _FakeRepo({"chat": "claude"}, {"chat": ["claude-vertex", "gpt"]})
    group = await _service(repo).get_tier_group(_DB, "chat")
    assert group == ["claude", "claude-vertex", "gpt"]


@pytest.mark.asyncio
async def test_a_tier_with_no_default_has_no_group():
    """There is nothing for a chain to fall back *from*, and the proxy keys fallbacks on the
    head alias, so a headless chain could not be declared even if we stored one."""
    repo = _FakeRepo({}, {"chat": ["gpt"]})
    assert await _service(repo).get_tier_group(_DB, "chat") == []


@pytest.mark.asyncio
async def test_all_tier_groups_skips_tiers_without_a_default():
    repo = _FakeRepo({"chat": "claude", "chat:low": "flash"}, {"chat": ["gpt"]})
    groups = await _service(repo).get_all_tier_groups(_DB)
    assert groups == {"chat": ["claude", "gpt"], "chat:low": ["flash"]}
    assert "chat:premium" not in groups


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["embedding", "multimodal_embedding", "search"])
async def test_non_chat_roles_are_refused(role):
    """Chat-only is a safety property, not an omission: a failed-over embedding call writes
    vectors from another embedding space into the same pgvector index. They insert cleanly
    and poison similarity search for every document embedded during the outage."""
    repo = _FakeRepo({role: "titan"})
    svc = _service(repo)
    gateway = _FakeGateway(registered=["titan", "gemini-embed"])
    with pytest.raises(ValueError, match="only supported for chat tiers"):
        await svc.set_failover_chain(_DB, actor=None, role=role, aliases=["gemini-embed"], gateway=gateway)
    assert gateway.declared == []
    assert repo.replaced == []


@pytest.mark.asyncio
async def test_chain_is_stored_then_projected():
    """Order matters and is deliberate: a chain recorded but not projected is a missing
    failover (the pre-feature status quo), whereas projecting first and failing to record
    would leave the proxy routing somewhere the console can neither show nor revoke."""
    repo = _FakeRepo({"chat": "claude"})
    gateway = _FakeGateway(registered=["claude", "claude-vertex", "gpt"])
    models = await _service(repo).set_failover_chain(
        _DB, actor=None, role="chat", aliases=["claude-vertex", "gpt"], gateway=gateway
    )
    assert models == ["claude", "claude-vertex", "gpt"]
    assert repo.replaced == [("chat", ["claude-vertex", "gpt"])]
    assert gateway.declared == [{"claude": ["claude-vertex", "gpt"]}]


@pytest.mark.asyncio
async def test_projection_failure_propagates_but_leaves_the_chain_stored():
    repo = _FakeRepo({"chat": "claude"})
    gateway = _FakeGateway(registered=["claude", "gpt"], fail_on_set=True)
    with pytest.raises(ModelGatewayError):
        await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["gpt"], gateway=gateway)
    assert repo.chains["chat"] == ["gpt"]  # stored, so the admin can retry the projection


@pytest.mark.asyncio
async def test_unregistered_alias_is_refused():
    repo = _FakeRepo({"chat": "claude"})
    gateway = _FakeGateway(registered=["claude"])
    with pytest.raises(ValueError, match="Not registered on the gateway"):
        await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["ghost"], gateway=gateway)
    assert gateway.declared == []


@pytest.mark.asyncio
async def test_a_chain_may_not_revisit_the_tier_default():
    """The head is already attempt one; repeating it just retries a provider known to be down."""
    repo = _FakeRepo({"chat": "claude"})
    gateway = _FakeGateway(registered=["claude", "gpt"])
    with pytest.raises(ValueError, match="appears twice"):
        await _service(repo).set_failover_chain(
            _DB, actor=None, role="chat", aliases=["gpt", "claude"], gateway=gateway
        )


@pytest.mark.asyncio
async def test_a_chain_may_not_repeat_an_alias():
    repo = _FakeRepo({"chat": "claude"})
    gateway = _FakeGateway(registered=["claude", "gpt"])
    with pytest.raises(ValueError, match="appears twice"):
        await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["gpt", "gpt"], gateway=gateway)


@pytest.mark.asyncio
async def test_setting_a_chain_requires_the_tier_to_have_a_default():
    repo = _FakeRepo({})
    gateway = _FakeGateway(registered=["gpt"])
    with pytest.raises(ValueError, match="no default model"):
        await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["gpt"], gateway=gateway)


@pytest.mark.asyncio
async def test_empty_chain_clears_the_route():
    repo = _FakeRepo({"chat": "claude"}, {"chat": ["gpt"]})
    gateway = _FakeGateway(registered=["claude", "gpt"])
    models = await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=[], gateway=gateway)
    assert models == ["claude"]
    assert repo.chains["chat"] == []
    assert gateway.declared == [{"claude": []}]  # the gateway leaves an empty chain undeclared


# --- re-pointing a tier's default --------------------------------------------------------


@pytest.mark.asyncio
async def test_changing_the_default_moves_the_chain_to_the_new_head():
    repo = _FakeRepo({"chat": "gpt"}, {"chat": ["claude-vertex"]})  # already re-pointed from 'claude'
    gateway = _FakeGateway(registered=["gpt", "claude", "claude-vertex"])
    await _service(repo).reproject_tier_group(_DB, "chat", actor=None, gateway=gateway)
    # The whole list replaces what the proxy held: the old head is simply absent, so it stops
    # failing over — no per-entry delete that a stale proxy cache can 404.
    assert gateway.declared == [{"gpt": ["claude-vertex"]}]


@pytest.mark.asyncio
async def test_the_old_head_is_kept_when_it_still_serves_another_tier():
    """One alias can be the default of several tiers at once; dropping its chain because one
    tier moved on would silently disarm the tier still using it."""
    repo = _FakeRepo(
        {"chat": "gpt", "chat:premium": "claude"}, {"chat": ["claude-vertex"], "chat:premium": ["gpt"]}
    )
    gateway = _FakeGateway(registered=["gpt", "claude", "claude-vertex"])
    await _service(repo).reproject_tier_group(_DB, "chat", actor=None, gateway=gateway)
    assert gateway.declared == [{"gpt": ["claude-vertex"], "claude": ["gpt"]}]


@pytest.mark.asyncio
async def test_reprojecting_a_non_chat_role_is_a_no_op():
    repo = _FakeRepo({"embedding": "titan"})
    gateway = _FakeGateway(registered=["titan"])
    await _service(repo).reproject_tier_group(_DB, "embedding", actor=None, gateway=gateway)
    assert gateway.declared == []


# --- every chain in one write (live QA 2026-09-30) ----------------------------------------
# LiteLLM's per-entry /fallback endpoints read-modify-write ONE row holding every chain,
# through a 60 s config cache they never invalidate: a quick second edit worked on a stale
# copy — a delete 404'd while the chain stayed live, and a second add erased the first.


@pytest.mark.asyncio
async def test_editing_one_tier_redeclares_every_tier_from_the_table():
    """The other tiers' chains ride along on every write, so no edit can erase them."""
    repo = _FakeRepo({"chat": "claude", "chat:low": "flash"}, {"chat:low": ["flash-lite"]})
    gateway = _FakeGateway(registered=["claude", "gpt", "flash", "flash-lite"])
    await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["gpt"], gateway=gateway)
    assert gateway.declared == [{"claude": ["gpt"], "flash": ["flash-lite"]}]


@pytest.mark.asyncio
async def test_a_head_shared_by_two_tiers_declares_the_first_tiers_chain():
    """The proxy holds one chain per alias; which one it gets is deterministic (tier order),
    and the other tier shows as drifted in the listing rather than silently flapping."""
    repo = _FakeRepo({"chat": "claude", "chat:premium": "claude"}, {"chat": ["gpt"], "chat:premium": ["opus"]})
    gateway = _FakeGateway(registered=["claude", "gpt", "opus"])
    declared = await _service(repo).project_chains(_DB, gateway=gateway)
    assert declared == {"claude": ["gpt"]}


@pytest.mark.asyncio
async def test_retiring_an_alias_cleans_every_chain_and_declares_once():
    repo = _FakeRepo({"chat": "claude", "chat:low": "flash"}, {"chat": ["gpt", "old"], "chat:low": ["old"]})
    gateway = _FakeGateway(registered=["claude", "gpt", "flash"])
    changed = await _service(repo).drop_alias_from_chains(_DB, None, "old", gateway=gateway)
    assert sorted(changed) == ["chat", "chat:low"]
    assert gateway.declared == [{"claude": ["gpt"], "flash": []}]


# --- validation and repair added after review round 1 -------------------------------------


class _ModeGateway(_FakeGateway):
    """Gateway whose deployments carry a mode, as /model/info reports them."""

    def __init__(self, modes: dict[str, str]):
        super().__init__(registered=list(modes))
        self.modes = modes

    async def list_models(self):
        return [{"model_name": n, "model_info": {"mode": m}} for n, m in self.modes.items()]


@pytest.mark.asyncio
async def test_an_embedding_alias_cannot_be_a_chat_tier_fallback():
    """_require_chat_tier guards the tier's ROLE; this guards the chain's MEMBERS. Without it an
    API caller could route chat traffic onto an embedding deployment, which fails hard at the one
    moment the chain is needed."""
    repo = _FakeRepo({"chat": "claude"})
    gateway = _ModeGateway({"claude": "chat", "titan-embed": "embedding"})
    with pytest.raises(ValueError, match="Not chat models"):
        await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["titan-embed"], gateway=gateway)
    assert gateway.declared == []


@pytest.mark.asyncio
async def test_a_deployment_with_no_declared_mode_is_treated_as_chat():
    """/model/info omits `mode` for plain chat deployments, so absent must not mean 'rejected'."""
    repo = _FakeRepo({"chat": "claude"})
    gateway = _FakeGateway(registered=["claude", "gpt"])  # no model_info at all
    models = await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["gpt"], gateway=gateway)
    assert models == ["claude", "gpt"]


@pytest.mark.asyncio
async def test_promoting_an_alias_already_in_the_chain_removes_it_from_the_chain():
    """Otherwise the tier declares a chain that falls back from the head to itself — burning a hop
    on the provider just found unavailable — and every later edit 400s, because set_failover_chain
    rejects a chain containing the head."""
    repo = _FakeRepo({"chat": "gpt"}, {"chat": ["gpt", "vertex"]})  # 'gpt' just promoted
    gateway = _FakeGateway(registered=["gpt", "claude", "vertex"])
    await _service(repo).reproject_tier_group(_DB, "chat", actor=None, gateway=gateway)

    assert gateway.declared == [{"gpt": ["vertex"]}]  # no self-reference projected
    assert repo.chains["chat"] == ["vertex"]  # and the correction is persisted, not just projected
    assert repo.replaced == [("chat", ["vertex"])]


@pytest.mark.asyncio
async def test_an_explicit_null_mode_is_treated_as_chat():
    """`.get("mode", "chat")` would reject this: the default applies only when the key is
    ABSENT, and deployments exist whose model_info carries an explicit null."""
    repo = _FakeRepo({"chat": "claude"})
    gateway = _ModeGateway({"claude": "chat", "gpt": None})
    models = await _service(repo).set_failover_chain(_DB, actor=None, role="chat", aliases=["gpt"], gateway=gateway)
    assert models == ["claude", "gpt"]


# --- utility-tier guard (nannos#318) -------------------------------------------------------
# The registration probe records ``response_format`` support on the deployment. chat and
# chat:low carry every classifier/summarizer call, which has no other shape, so a model
# recorded as rejecting it must not become their default or enter their chain.

_NO_RF = {"nannos_capabilities": {"response_format": False, "forced_tool_choice": False}}


class _FakeGatewayWithInfo(_FakeGateway):
    def __init__(self, infos: dict):
        super().__init__(registered=list(infos))
        self.infos = infos

    async def list_models(self):
        return [{"model_name": name, "model_info": {"mode": "chat", **info}} for name, info in self.infos.items()]


class _RecordingRepo(_FakeRepo):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.upserts: list[tuple[str, str]] = []

    async def upsert_default(self, db, actor, role, model_alias):
        self.upserts.append((role, model_alias))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["chat", "chat:low"])
async def test_a_model_that_rejects_response_format_cannot_default_a_utility_tier(role):
    repo = _RecordingRepo()
    with pytest.raises(ValueError, match="rejects response_format"):
        await _service(repo).set_default(_DB, actor=None, role=role, model_alias="sonnet-5-5", model_info=_NO_RF)
    assert repo.upserts == []


@pytest.mark.asyncio
async def test_the_same_model_may_default_chat_premium_or_an_unprobed_deployment_any_tier():
    """chat:premium is a user's explicit pick and carries no utility traffic; an unprobed
    deployment has no record, and the absence of a record is not a verdict."""
    repo = _RecordingRepo()
    svc = _service(repo)
    await svc.set_default(_DB, actor=None, role="chat:premium", model_alias="sonnet-5-5", model_info=_NO_RF)
    await svc.set_default(_DB, actor=None, role="chat", model_alias="legacy", model_info={})
    await svc.set_default(_DB, actor=None, role="chat:low", model_alias="old", model_info=None)
    assert [r for r, _ in repo.upserts] == ["chat:premium", "chat", "chat:low"]


@pytest.mark.asyncio
async def test_a_utility_tier_chain_refuses_a_member_that_rejects_response_format():
    """Failover lands the tier's traffic — utility calls included — on the member."""
    repo = _FakeRepo({"chat:low": "flash"})
    gateway = _FakeGatewayWithInfo({"flash": {}, "sonnet-5-5": _NO_RF, "gpt": {"nannos_capabilities": {"response_format": True}}})
    with pytest.raises(ValueError, match="rejects response_format"):
        await _service(repo).set_failover_chain(_DB, actor=None, role="chat:low", aliases=["gpt", "sonnet-5-5"], gateway=gateway)
    assert gateway.declared == [] and repo.replaced == []

    models = await _service(repo).set_failover_chain(_DB, actor=None, role="chat:low", aliases=["gpt"], gateway=gateway)
    assert models == ["flash", "gpt"]


@pytest.mark.asyncio
async def test_a_premium_chain_takes_the_same_member():
    repo = _FakeRepo({"chat:premium": "opus"})
    gateway = _FakeGatewayWithInfo({"opus": {}, "sonnet-5-5": _NO_RF})
    models = await _service(repo).set_failover_chain(_DB, actor=None, role="chat:premium", aliases=["sonnet-5-5"], gateway=gateway)
    assert models == ["opus", "sonnet-5-5"]


@pytest.mark.asyncio
async def test_utility_tiers_served_by_names_default_and_chain_membership():
    repo = _FakeRepo({"chat": "sonnet-5-5", "chat:low": "flash", "chat:premium": "sonnet-5-5"}, {"chat:low": ["sonnet-5-5"]})
    served = await _service(repo).utility_tiers_served_by(_DB, "sonnet-5-5")
    assert served == ["chat (default)", "chat:low (failover chain)"]
    assert await _service(repo).utility_tiers_served_by(_DB, "other") == []
