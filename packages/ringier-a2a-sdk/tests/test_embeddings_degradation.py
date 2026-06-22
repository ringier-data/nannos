"""Provider-aware request shaping + text-only multimodal degradation for GatewayEmbeddings.

The adapter speaks to whatever embedding alias the gateway resolves; an ``EmbeddingProfile``
(selected from the litellm model / provider family) decides the asymmetric-retrieval mechanism,
whether to send the Matryoshka ``dimensions`` param, and whether text+image fusion is attempted.
Only Gemini Embedding 2 fuses; for every other model image-bearing docs degrade to text-only
instead of failing the sync. These tests pin profile selection and the _invoke/degradation paths.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from ringier_a2a_sdk import embeddings as emb_mod
from ringier_a2a_sdk.embeddings import GatewayEmbeddings, profile_for, supports_image_fusion

TEXT_VEC = [0.1, 0.2, 0.3]
FUSED_VEC = [0.9, 0.8, 0.7]
IMG = b"\x89PNG fake bytes"

# A fusion-capable (Gemini) deployment — used by the multimodal degradation tests so the
# runtime fused-call → capability-error path is actually exercised.
GEMINI_MODEL = "vertex_ai/gemini-embedding-2"


def _gemini_emb(model_id: str = "some-embedding-model") -> GatewayEmbeddings:
    return GatewayEmbeddings(role="document", model_id=model_id, litellm_model=GEMINI_MODEL)


def _generic_emb(model_id: str = "titan-alias") -> GatewayEmbeddings:
    return GatewayEmbeddings(role="document", model_id=model_id, provider="bedrock")


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://gateway/embeddings")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _invoke_mock(fusion_outcome):
    """Mock _invoke: text-only returns TEXT_VEC; text+image yields `fusion_outcome`."""

    def side_effect(*, text, image_bytes=None, mime_type="image/png"):
        if image_bytes is None:
            return TEXT_VEC
        if isinstance(fusion_outcome, Exception):
            raise fusion_outcome
        return fusion_outcome

    return MagicMock(side_effect=side_effect)


# --------------------------------------------------------------------------- profile selection


@pytest.mark.parametrize(
    "litellm_model,provider,expect_prefix,expect_dims,expect_input_type,expect_fusion",
    [
        # Gemini Embedding 2 (precise string): prefixes + dimensions + fusion.
        ("vertex_ai/gemini-embedding-2", None, True, True, False, True),
        # Provider-only Gemini/Vertex signal (worker path) → full Gemini profile, so multimodal
        # indexing still attempts fusion (a non-fusing Vertex model degrades via the runtime latch).
        (None, "vertex_ai", True, True, False, True),
        (None, "gemini", True, True, False, True),
        # Cohere: asymmetric via input_type param, no dimensions, text-only.
        ("bedrock/cohere.embed-english-v3", None, False, False, True, False),
        (None, "cohere", False, False, True, False),
        # Bedrock Titan / generic: symmetric, dimensions on, text-only, no prefixes.
        ("bedrock/amazon.titan-embed-text-v2:0", None, False, True, False, False),
        (None, "bedrock", False, True, False, False),
        # Unknown on both → conservative generic.
        (None, None, False, True, False, False),
    ],
)
def test_profile_for(litellm_model, provider, expect_prefix, expect_dims, expect_input_type, expect_fusion):
    p = profile_for(litellm_model, provider)
    assert bool(p.text_prefixes) is expect_prefix
    assert p.send_dimensions is expect_dims
    assert bool(p.input_type) is expect_input_type
    assert p.supports_fusion is expect_fusion


def test_format_text_applies_prefix_only_for_gemini():
    gemini = profile_for("vertex_ai/gemini-embedding-2")
    assert gemini.format_text("query", "hello") == "task: search result | query: hello"
    assert gemini.format_text("document", "hello") == "title: none | text: hello"
    # Generic/Cohere never prepend a prefix (Cohere asymmetry rides input_type instead).
    assert profile_for(None, "bedrock").format_text("query", "hello") == "hello"
    assert profile_for(None, "cohere").format_text("document", "hello") == "hello"


@pytest.mark.parametrize(
    "litellm_model,expected",
    [
        ("vertex_ai/gemini-embedding-2", True),
        ("vertex_ai/gemini-embedding-001", True),
        ("bedrock/amazon.nova-2-multimodal-embeddings-v1:0", False),
        ("bedrock/amazon.titan-embed-text-v2:0", False),
        # A non-embedding Vertex text model must NOT claim fusion just from the string.
        ("vertex_ai/text-embedding-005", False),
        (None, False),
        ("", False),
    ],
)
def test_supports_image_fusion(litellm_model, expected):
    assert supports_image_fusion(litellm_model) is expected


# --------------------------------------------------------------------------- _invoke body shape


def _capture_invoke_body(emb: GatewayEmbeddings, monkeypatch) -> dict:
    """Run _invoke once against a fake gateway and return the POSTed JSON body."""
    monkeypatch.setattr(emb_mod, "gateway_base_url", lambda: "http://gateway")
    emb._attribution_header = lambda: {}
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": TEXT_VEC}]}

    class _FakeClient:
        def post(self, url, json, headers):
            captured.update(json)
            return _FakeResp()

    monkeypatch.setattr(emb_mod, "_client", emb_mod.LazyClient(lambda: _FakeClient()))
    emb._invoke(text="hello")
    return captured


def test_invoke_body_gemini_sends_dimensions_and_prefix(monkeypatch):
    body = _capture_invoke_body(GatewayEmbeddings(role="query", model_id="g", litellm_model=GEMINI_MODEL), monkeypatch)
    assert body["dimensions"] == emb_mod._DEFAULT_DIMENSION
    assert body["input"] == ["task: search result | query: hello"]
    assert "input_type" not in body


def test_invoke_body_cohere_sends_input_type_not_dimensions(monkeypatch):
    body = _capture_invoke_body(GatewayEmbeddings(role="document", model_id="c", provider="cohere"), monkeypatch)
    assert "dimensions" not in body  # v3 rejects the Matryoshka param
    assert body["input_type"] == "search_document"
    assert body["input"] == ["hello"]  # no Gemini prefix


def test_invoke_body_generic_sends_dimensions_no_prefix(monkeypatch):
    body = _capture_invoke_body(GatewayEmbeddings(role="document", model_id="t", provider="bedrock"), monkeypatch)
    assert body["dimensions"] == emb_mod._DEFAULT_DIMENSION
    assert body["input"] == ["hello"]
    assert "input_type" not in body


# --------------------------------------------------------------------------- multimodal degradation


@pytest.mark.parametrize(
    "outcome",
    [RuntimeError("gateway returned per-element vectors"), _http_error(400), _http_error(422)],
)
def test_degrades_to_text_only_on_capability_error(outcome):
    emb = _gemini_emb()
    emb._invoke = _invoke_mock(outcome)

    result = emb.embed_with_image("a slide", IMG)

    assert result == TEXT_VEC
    assert emb._image_fusion_unsupported is True


def test_text_only_profile_skips_the_fused_call_entirely():
    """A model the profile already knows is text-only must not waste a doomed fused round-trip."""
    emb = _generic_emb()
    emb._invoke = _invoke_mock(RuntimeError("should never be reached for image input"))

    result = emb.embed_with_image("a slide", IMG)

    assert result == TEXT_VEC
    assert emb._image_fusion_unsupported is True
    emb._invoke.assert_called_once()
    assert emb._invoke.call_args.kwargs.get("image_bytes") is None


def test_latches_so_later_image_docs_skip_the_fused_call():
    emb = _gemini_emb()
    emb._invoke = _invoke_mock(RuntimeError("no fusion"))

    emb.embed_with_image("doc 1", IMG)  # trips the capability error once
    emb._invoke.reset_mock()

    result = emb.embed_with_image("doc 2", IMG)  # should go straight to text-only

    assert result == TEXT_VEC
    # Exactly one call, and it was text-only (no image_bytes) — the fused call is skipped.
    emb._invoke.assert_called_once()
    assert emb._invoke.call_args.kwargs.get("image_bytes") is None


def test_transient_error_reraises_and_does_not_latch():
    emb = _gemini_emb()
    emb._invoke = _invoke_mock(_http_error(503))

    with pytest.raises(httpx.HTTPStatusError):
        emb.embed_with_image("a slide", IMG)
    # A 5xx is transient — we must not permanently downgrade the whole sync to text-only.
    assert emb._image_fusion_unsupported is False


def test_warning_emitted_once_per_alias_process_wide(caplog):
    emb_mod._FUSION_WARNED.discard("some-embedding-model")
    emb_mod._FUSION_WARNED.discard("other-model")

    with caplog.at_level("WARNING", logger="ringier_a2a_sdk.embeddings"):
        # Two separate instances (e.g. two sync jobs) for the same alias → one warning total.
        for _ in range(2):
            e = _gemini_emb()
            e._invoke = _invoke_mock(RuntimeError("no fusion"))
            e.embed_with_image("doc", IMG)
        same_alias_warnings = [r for r in caplog.records if "some-embedding-model" in r.getMessage()]
        assert len(same_alias_warnings) == 1

        # A different alias warns once on its own.
        other = _gemini_emb("other-model")
        other._invoke = _invoke_mock(RuntimeError("no fusion"))
        other.embed_with_image("doc", IMG)
        assert any("other-model" in r.getMessage() for r in caplog.records)


def test_fusion_capable_model_keeps_the_image_vector():
    emb = _gemini_emb()
    emb._invoke = _invoke_mock(FUSED_VEC)

    result = emb.embed_with_image("a slide", IMG)

    assert result == FUSED_VEC
    assert emb._image_fusion_unsupported is False


# --------------------------------------------------------------------------- transport / batching


def test_invoke_reuses_one_pooled_client(monkeypatch):
    """_invoke must share one process-wide httpx.Client, not build one per call (#8)."""
    monkeypatch.setattr(emb_mod, "gateway_base_url", lambda: "http://gateway")
    constructed = {"n": 0}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": TEXT_VEC}]}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            constructed["n"] += 1

        def post(self, url, json, headers):
            return _FakeResp()

    monkeypatch.setattr(emb_mod.httpx, "Client", _FakeClient)
    # Fresh LazyClient so the count starts clean and the real singleton is restored on teardown.
    monkeypatch.setattr(emb_mod, "_client", emb_mod.LazyClient(lambda: emb_mod.httpx.Client()))

    emb = GatewayEmbeddings(role="document", model_id="g", litellm_model=GEMINI_MODEL)
    emb._attribution_header = lambda: {}  # isolate from cost-attribution wiring

    assert emb._invoke(text="a") == TEXT_VEC
    assert emb._invoke(text="b") == TEXT_VEC
    assert emb.embed_query("c") == TEXT_VEC

    assert constructed["n"] == 1  # one client reused across all calls (was one-per-call)


def test_embed_documents_preserves_order_when_fanned_out():
    """The parallel embed_documents must return vectors aligned to the input order."""
    emb = _gemini_emb()
    # One distinct vector per text; if the thread-pool fan-out scrambled results, the
    # order assertion below would fail.
    emb._invoke = MagicMock(side_effect=lambda text, **_: [float(len(text))])

    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    result = emb.embed_documents(texts)

    assert result == [[1.0], [2.0], [3.0], [4.0], [5.0]]
    assert emb._invoke.call_count == len(texts)
