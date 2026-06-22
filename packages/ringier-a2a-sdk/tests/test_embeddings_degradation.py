"""Text-only degradation for multimodal embeddings (option 1).

GeminiEmbeddings speaks the Vertex "fused input list → one vector" contract, which only
Gemini Embedding 2 honours. When the configured multimodal_embedding default is a model
that can't fuse (e.g. Bedrock Nova/Titan), image-bearing docs must degrade to text-only
instead of failing the whole sync. These tests pin that behavior at the _invoke boundary.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from ringier_a2a_sdk import embeddings as emb_mod
from ringier_a2a_sdk.embeddings import GeminiEmbeddings, supports_image_fusion

TEXT_VEC = [0.1, 0.2, 0.3]
FUSED_VEC = [0.9, 0.8, 0.7]
IMG = b"\x89PNG fake bytes"


def _emb() -> GeminiEmbeddings:
    return GeminiEmbeddings(role="document", model_id="some-embedding-model")


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


@pytest.mark.parametrize(
    "outcome",
    [RuntimeError("gateway returned per-element vectors"), _http_error(400), _http_error(422)],
)
def test_degrades_to_text_only_on_capability_error(outcome):
    emb = _emb()
    emb._invoke = _invoke_mock(outcome)

    result = emb.embed_with_image("a slide", IMG)

    assert result == TEXT_VEC
    assert emb._image_fusion_unsupported is True


def test_latches_so_later_image_docs_skip_the_fused_call():
    emb = _emb()
    emb._invoke = _invoke_mock(RuntimeError("no fusion"))

    emb.embed_with_image("doc 1", IMG)  # trips the capability error once
    emb._invoke.reset_mock()

    result = emb.embed_with_image("doc 2", IMG)  # should go straight to text-only

    assert result == TEXT_VEC
    # Exactly one call, and it was text-only (no image_bytes) — the fused call is skipped.
    emb._invoke.assert_called_once()
    assert emb._invoke.call_args.kwargs.get("image_bytes") is None


def test_transient_error_reraises_and_does_not_latch():
    emb = _emb()
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
            e = _emb()
            e._invoke = _invoke_mock(RuntimeError("no fusion"))
            e.embed_with_image("doc", IMG)
        same_alias_warnings = [r for r in caplog.records if "some-embedding-model" in r.getMessage()]
        assert len(same_alias_warnings) == 1

        # A different alias warns once on its own.
        other = GeminiEmbeddings(role="document", model_id="other-model")
        other._invoke = _invoke_mock(RuntimeError("no fusion"))
        other.embed_with_image("doc", IMG)
        assert any("other-model" in r.getMessage() for r in caplog.records)


def test_fusion_capable_model_keeps_the_image_vector():
    emb = _emb()
    emb._invoke = _invoke_mock(FUSED_VEC)

    result = emb.embed_with_image("a slide", IMG)

    assert result == FUSED_VEC
    assert emb._image_fusion_unsupported is False


@pytest.mark.parametrize(
    "litellm_model,expected",
    [
        ("vertex_ai/gemini-embedding-2", True),
        ("vertex_ai/gemini-embedding-001", True),
        ("bedrock/amazon.nova-2-multimodal-embeddings-v1:0", False),
        ("bedrock/amazon.titan-embed-text-v2:0", False),
        (None, False),
        ("", False),
    ],
)
def test_supports_image_fusion(litellm_model, expected):
    assert supports_image_fusion(litellm_model) is expected
