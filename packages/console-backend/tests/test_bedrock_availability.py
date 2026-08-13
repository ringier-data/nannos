"""Bedrock per-region availability lookup — the advisory behind the registration UI's region hint.

The contract that matters is the failure one: this needs IAM permissions a deployment may not grant,
and it must never turn a missing permission into "AWS doesn't offer this model" (which would send an
admin to change a model id that was fine all along) nor break registration.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from console_backend.services import bedrock_availability_service as svc


class _FakeClient:
    """One region's Bedrock control plane."""

    def __init__(self, foundation: list[str], profiles: list[str] | None = None, boom: bool = False):
        self._foundation = foundation
        self._profiles = profiles
        self._boom = boom
        self.calls = 0

    async def __aenter__(self):
        if self._boom:
            raise RuntimeError("AccessDeniedException: bedrock:ListFoundationModels")
        return self

    async def __aexit__(self, *exc):
        return False

    async def list_foundation_models(self):
        self.calls += 1
        return {"modelSummaries": [{"modelId": m} for m in self._foundation]}

    async def list_inference_profiles(self):
        if self._profiles is None:
            raise RuntimeError("ValidationException: unknown operation")
        return {"inferenceProfileSummaries": [{"inferenceProfileId": p} for p in self._profiles]}


@pytest.fixture(autouse=True)
def _clear_cache():
    svc._cache.clear()
    yield
    svc._cache.clear()


def _patch_regions(monkeypatch, per_region: dict[str, _FakeClient], probed: list[str] | None = None):
    def _create_client(service, region_name):
        assert service == "bedrock"
        return per_region[region_name]

    monkeypatch.setattr(svc, "get_session", lambda: SimpleNamespace(create_client=_create_client))
    monkeypatch.setattr(svc, "probed_regions", lambda: probed or list(per_region))


@pytest.mark.asyncio
async def test_reports_only_the_regions_that_offer_the_model(monkeypatch):
    """The case that started this: Nova 2 multimodal embeddings exist in us-east-1, not eu-central-1."""
    _patch_regions(
        monkeypatch,
        {
            "eu-central-1": _FakeClient(["amazon.titan-embed-image-v1"]),
            "us-east-1": _FakeClient(["amazon.titan-embed-image-v1", "amazon.nova-2-multimodal-embeddings-v1:0"]),
        },
    )

    assert await svc.model_regions("amazon.nova-2-multimodal-embeddings-v1:0") == ["us-east-1"]
    assert await svc.model_regions("amazon.titan-embed-image-v1") == ["eu-central-1", "us-east-1"]


@pytest.mark.asyncio
async def test_inference_profile_ids_count_as_available(monkeypatch):
    """`eu.anthropic.…` ids are inference profiles, not foundation models — listing only the latter
    would report every cross-region-inference model as unavailable."""
    _patch_regions(
        monkeypatch,
        {"eu-central-1": _FakeClient(["anthropic.claude-x"], profiles=["eu.anthropic.claude-x"])},
    )

    assert await svc.model_regions("eu.anthropic.claude-x") == ["eu-central-1"]


@pytest.mark.asyncio
async def test_missing_inference_profile_api_still_answers_from_foundation_models(monkeypatch):
    """An older botocore (or no profile permission) must degrade, not blank the whole answer."""
    _patch_regions(monkeypatch, {"eu-central-1": _FakeClient(["amazon.titan-embed-image-v1"], profiles=None)})

    assert await svc.model_regions("amazon.titan-embed-image-v1") == ["eu-central-1"]


@pytest.mark.asyncio
async def test_no_permission_anywhere_is_unknown_not_unavailable(monkeypatch):
    """The important one: without bedrock:ListFoundationModels the answer is None ("can't say"), never
    [] — the UI shows nothing instead of accusing a perfectly good model id."""
    _patch_regions(
        monkeypatch,
        {"eu-central-1": _FakeClient([], boom=True), "us-east-1": _FakeClient([], boom=True)},
    )

    assert await svc.model_regions("amazon.titan-embed-image-v1") is None


@pytest.mark.asyncio
async def test_partial_failure_answers_from_the_readable_regions(monkeypatch):
    _patch_regions(
        monkeypatch,
        {
            "eu-central-1": _FakeClient([], boom=True),
            "us-east-1": _FakeClient(["amazon.nova-2-multimodal-embeddings-v1:0"]),
        },
    )

    assert await svc.model_regions("amazon.nova-2-multimodal-embeddings-v1:0") == ["us-east-1"]


@pytest.mark.asyncio
async def test_unknown_model_is_an_empty_list(monkeypatch):
    """Distinct from None: we could read every region and none of them has it."""
    _patch_regions(monkeypatch, {"eu-central-1": _FakeClient(["amazon.titan-embed-image-v1"])})

    assert await svc.model_regions("amazon.typo-v9") == []


@pytest.mark.asyncio
async def test_region_catalogs_are_cached_across_lookups(monkeypatch):
    """Registration must never wait on an AWS round-trip per keystroke or per model."""
    client = _FakeClient(["a", "b"])
    _patch_regions(monkeypatch, {"eu-central-1": client})

    await svc.model_regions("a")
    await svc.model_regions("b")

    assert client.calls == 1


@pytest.mark.asyncio
async def test_endpoint_stays_advisory_when_the_probe_fails(monkeypatch):
    """The route must answer 200 with regions=null, not raise — it is a hint, not a gate."""
    import console_backend.routers.admin_model_gateway_router as router

    monkeypatch.setattr(router, "model_regions", AsyncMock(return_value=None))
    monkeypatch.setattr(router, "probed_regions", lambda: ["eu-central-1"])

    out = await router.bedrock_model_regions(model_id="amazon.whatever", _=SimpleNamespace())

    assert out.regions is None
    assert out.probed_regions == ["eu-central-1"]


@pytest.mark.asyncio
async def test_gateway_ui_config_serves_every_deployment_default():
    """Every field the registration form reads must actually resolve. The endpoint reads them straight
    off `config.model_gateway`, so a field removed or renamed there is an AttributeError → 500 on the
    whole config fetch, which silently blanks the UI's placeholders (the Bedrock region hint read
    "the gateway's region" for exactly this reason). Nothing else covered this route."""
    import console_backend.routers.admin_model_gateway_router as router

    out = await router.gateway_ui_config(user=SimpleNamespace(id="admin"))

    assert out.default_vertex_location  # DEFAULT_VERTEXAI_LOCATION, never blank (has a default)
    assert out.default_bedrock_region  # AWS_BEDROCK_REGION → AWS_REGION → eu-central-1
    assert isinstance(out.default_vertex_project, str)  # may legitimately be "" (resolved from ADC)


def test_probed_regions_puts_the_gateway_region_first_without_duplicating_it():
    """The gateway's own region is the one that decides success, so it leads — and configuring it in
    the extras list must not make it appear twice."""
    regions = svc.probed_regions()

    assert regions[0] == svc.config.model_gateway.default_bedrock_region
    assert len(regions) == len(set(regions))
