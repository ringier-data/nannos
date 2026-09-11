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
    """Records what would be declared on the proxy; can be made to fail."""

    def __init__(self, registered=(), fail_on_set=False):
        self.registered = list(registered)
        self.fail_on_set = fail_on_set
        self.set_calls: list[tuple[str, list[str]]] = []
        self.deleted: list[str] = []

    async def list_models(self):
        return [{"model_name": name} for name in self.registered]

    async def set_fallbacks(self, model_name, fallback_models):
        if self.fail_on_set:
            raise ModelGatewayError("proxy said no")
        self.set_calls.append((model_name, list(fallback_models)))

    async def delete_fallbacks(self, model_name):
        self.deleted.append(model_name)


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
    assert gateway.set_calls == []
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
    assert gateway.set_calls == [("claude", ["claude-vertex", "gpt"])]


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
    assert gateway.set_calls == []


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
    assert gateway.set_calls == [("claude", [])]


# --- re-pointing a tier's default --------------------------------------------------------


@pytest.mark.asyncio
async def test_changing_the_default_moves_the_chain_to_the_new_head():
    repo = _FakeRepo({"chat": "gpt"}, {"chat": ["claude-vertex"]})  # already re-pointed
    gateway = _FakeGateway(registered=["gpt", "claude", "claude-vertex"])
    await _service(repo).reproject_tier_group(_DB, "chat", gateway=gateway, previous_head="claude")
    assert gateway.set_calls == [("gpt", ["claude-vertex"])]
    assert gateway.deleted == ["claude"]  # the old head must stop failing over


@pytest.mark.asyncio
async def test_the_old_head_is_kept_when_it_still_serves_another_tier():
    """One alias can be the default of several tiers at once; dropping its chain because one
    tier moved on would silently disarm the tier still using it."""
    repo = _FakeRepo({"chat": "gpt", "chat:premium": "claude"}, {"chat": ["claude-vertex"]})
    gateway = _FakeGateway(registered=["gpt", "claude", "claude-vertex"])
    await _service(repo).reproject_tier_group(_DB, "chat", gateway=gateway, previous_head="claude")
    assert gateway.deleted == []
    assert gateway.set_calls == [("gpt", ["claude-vertex"])]


@pytest.mark.asyncio
async def test_reprojecting_a_non_chat_role_is_a_no_op():
    repo = _FakeRepo({"embedding": "titan"})
    gateway = _FakeGateway(registered=["titan"])
    await _service(repo).reproject_tier_group(_DB, "embedding", gateway=gateway)
    assert gateway.set_calls == [] and gateway.deleted == []
