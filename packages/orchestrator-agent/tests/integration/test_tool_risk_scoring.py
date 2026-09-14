"""Integration tests: LLM tool-risk scoring against the live fleet.

``_score_tool_via_llm`` asks a model to classify a tool's risk and to return,
per parameter, the value patterns that make a call dangerous. Whether a given
model can actually do that is a property of the model and the provider behind
it, not of this code — so it can only be established by asking them.

The invariant under test is therefore behavioural: *scoring a risk-bearing tool
yields a populated ``risk_factors``*. Deliberately not "the call was made with
such-and-such structured-output options" — that pins an implementation detail,
passes whenever the detail is present regardless of whether it still works, and
would go red on a correct cleanup if the detail ever stopped being needed.

An empty ``risk_factors`` is the outcome worth guarding, and it is worse than an
error. A failed call raises, falls through to the deterministic fallback, and is
retried on the next pass. An empty map arrives as HTTP 200 with a plausible
``base_score``, is indistinguishable downstream from a model that looked and
found nothing, and is persisted with a *real* schema hash — so it never
self-corrects, and per-argument risk matching stays off for that tool
permanently (see #199 / #201).

Parametrized over ``ALL_MODELS`` rather than ``one_model_per_provider()``,
because the behaviour diverges per *model* and not per provider: aliases sharing
a provider disagree with each other, so collapsing to one per provider would
discard exactly the signal this exists for. One short structured call per model,
which is cheap next to the orchestrator turns in this directory.

Two tests share that one call, split by what each can fairly assert:

* The map being non-empty is ``strict``, so no aggregate pass ratio can absolve
  it — not because the emptiness is always deterministic (at least one alias
  returns an empty map only *some* of the time) but because the result is cached
  forever. An alias that populates the map three times in four still leaves a
  quarter of the tools it scores permanently unmatched. That is a reason to
  disqualify the alias, not to average it away.
* Which parameter the model called controlling, and how high it scored the tool,
  are genuine samples of model judgement. Those stay under the pass ratio, where
  a lone odd draw is a data point rather than a verdict.
"""

from __future__ import annotations

import logging
import os

import pytest
from agent_common.core import model_factory
from agent_common.core.tool_risk_cache import ToolRiskEntry
from agent_common.core.tool_risk_scorer import _score_tool_via_llm
from agent_common.models.base import ModelType
from langsmith import testing as t

from .conftest import ALL_MODELS

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# The probe tool
# ---------------------------------------------------------------------------
# Chosen so that an empty ``risk_factors`` is unambiguously wrong. A tool that is
# destructive in itself (say, a delete) is a poor probe: "the risk is fixed and no
# single parameter controls it" is a defensible answer there, and a model that
# returns no factors still reports a high base_score, so the call stays gated and
# the miss is half-masked.
#
# An HTTP client has no fixed risk at all. GET is a read; DELETE against an
# internal address is not. A model that returns no controlling parameters for this
# tool has asserted that its risk does not depend on its arguments, which is
# simply false — and it pairs that with a *low* base_score, so the call is waved
# through with nothing left to match on. That is the failure this feature exists
# to prevent, so it is the one worth probing.

_TOOL_NAME = "http_request"
_TOOL_DESCRIPTION = "Send an HTTP request to a URL and return the response."
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "method": {
            "type": "string",
            "description": "HTTP verb: GET, POST, PUT, PATCH or DELETE.",
        },
        "url": {
            "type": "string",
            "description": "Absolute URL to call.",
        },
        "body": {
            "type": "string",
            "description": "Request body, for verbs that take one.",
        },
    },
    "required": ["method", "url"],
}

# Two calls to the same tool that any usable risk profile must separate. Same URL
# in both, so `method` is the only variable and a profile keyed on anything else
# cannot accidentally satisfy the assertion.
_SAFE_CALL = {"method": "GET", "url": "https://api.example.com/v1/items"}
_DANGEROUS_CALL = {"method": "DELETE", "url": "https://api.example.com/v1/items"}


@pytest.fixture()
def fast_tier_pinned_to(monkeypatch, usage_recorder):
    """Pin the fast tier to one alias, and route its spend into the run report.

    ``_score_tool_via_llm`` takes no model argument — it resolves
    ``get_default_fast_model()`` itself and builds the model with no callbacks.
    Without the first patch every parametrization would score on whichever alias
    the fleet default happens to be; without the second the call is invisible to
    ``UsageRecorder`` and the test shows "-" in the integration cost table.

    Patching the module attributes is enough: the scorer imports both names
    *inside* the function body, so the lookup happens per call.
    """
    real_create_model = model_factory.create_model

    def _pin(model_type: ModelType) -> None:
        monkeypatch.setattr(model_factory, "get_default_fast_model", lambda: model_type)
        monkeypatch.setattr(
            model_factory,
            "create_model",
            lambda *args, **kwargs: real_create_model(
                *args, **{**kwargs, "callbacks": [usage_recorder]}
            ),
        )

    return _pin


# One call per alias, shared by both tests below. They ask two questions about the
# same single response, so scoring twice would double the spend to learn nothing —
# and would let the two tests disagree about a non-deterministic model. Session
# lifetime: parametrization runs the strict test across the fleet first, so it pays
# for every call and the qualitative test's rows show "-" in the cost table.
_SCORED: dict[ModelType, "ToolRiskEntry | BaseException"] = {}


async def _score_once(model_type: ModelType, pin) -> ToolRiskEntry:
    """Score the probe tool with *model_type*, reusing an earlier result if there is one.

    A raised exception is cached and re-raised too: a provider that rejects the
    request must fail both tests, not fail the first and be silently retried by
    the second.
    """
    if model_type not in _SCORED:
        pin(model_type)
        try:
            _SCORED[model_type] = await _score_tool_via_llm(
                _TOOL_NAME, _TOOL_DESCRIPTION, _TOOL_SCHEMA
            )
        except BaseException as exc:  # noqa: BLE001 — recorded, then re-raised below
            _SCORED[model_type] = exc

    result = _SCORED[model_type]
    if isinstance(result, BaseException):
        raise result
    return result


@pytest.mark.strict
@pytest.mark.langsmith
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
async def test_risk_scoring_never_returns_an_empty_risk_factors_map(
    model_type: ModelType, fast_tier_pinned_to
):
    """An empty ``risk_factors`` is a failure, on every alias the gateway serves.

    ``strict``, so the pass-ratio gate cannot absolve it. That gate exists for
    sampling noise, and this does not qualify even where the emptiness is itself
    intermittent: the profile is written to the cache with a real schema hash and
    never revisited, so an alias that populates the map only sometimes corrupts
    the rest permanently. "Usually works" is not a passing grade for a value that
    is computed once and then trusted forever.
    """
    t.log_inputs(
        {"model": model_type, "tool": _TOOL_NAME, "asserts": "risk_factors non-empty"}
    )

    entry = await _score_once(model_type, fast_tier_pinned_to)

    t.log_outputs(
        {"base_score": entry.base_score, "control_params": sorted(entry.risk_factors)}
    )

    assert entry.risk_factors, (
        f"{model_type} named no controlling parameters for {_TOOL_NAME} (base_score="
        f"{entry.base_score}, so the model did read the schema). The structured-output call "
        "was accepted and the per-parameter profile came back empty — that profile is persisted "
        "with a real schema hash, so unlike a hard failure it never self-corrects, and "
        "per-argument risk matching stays off for every tool scored by this alias."
    )


@pytest.mark.langsmith
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
async def test_risk_scoring_separates_a_dangerous_call_from_a_safe_one(
    model_type: ModelType, fast_tier_pinned_to
):
    """The profile actually discriminates, measured the way HITL measures it.

    ``match_args`` is what the middleware calls, and it floors every result at
    ``base_score`` — so a profile with no usable per-argument patterns scores a
    DELETE exactly like a GET, and the gate can no longer tell them apart. That is
    the end-to-end symptom of the empty map the strict test above catches, and it
    also catches a profile that is populated but keyed on nothing useful.

    Not strict: which parameter a model treats as controlling, and which patterns
    it writes, are samples of its judgement. A lone odd draw belongs under the
    pass ratio.
    """
    t.log_inputs(
        {"model": model_type, "tool": _TOOL_NAME, "asserts": "DELETE scores above GET"}
    )

    entry = await _score_once(model_type, fast_tier_pinned_to)

    safe = entry.match_args(_SAFE_CALL)
    dangerous = entry.match_args(_DANGEROUS_CALL)
    outcome = {
        "base_score": entry.base_score,
        "control_params": sorted(entry.risk_factors),
        "risky_values": {
            name: sorted(profile.risky_values)
            for name, profile in entry.risk_factors.items()
        },
        "GET": safe,
        "DELETE": dangerous,
    }
    t.log_outputs(outcome)
    logger.info("[%s] risk profile: %s", model_type, outcome)

    assert dangerous > safe, (
        f"{model_type} scored DELETE and GET identically ({dangerous}) on the same URL, so the "
        f"profile cannot separate them. Control params: {sorted(entry.risk_factors)}, "
        f"base_score={entry.base_score}. Per-argument gating is inert for this tool."
    )


# ---------------------------------------------------------------------------
# The strict-schema matrix, recorded rather than asserted
# ---------------------------------------------------------------------------

_ENV_STRICT_MATRIX = "RISK_SCORER_STRICT_MATRIX"


class _ForceStrictJsonSchema:
    """Model proxy that forces the langchain-openai >= 0.3 default back on.

    Wrapping the model rather than rebuilding the call means the probe runs the
    *real* scorer — same system prompt, same schema, same attribution scope — so
    the only difference from the test above is the one variable being measured.
    """

    def __init__(self, inner):
        self._inner = inner

    def with_structured_output(self, schema, **kwargs):
        return self._inner.with_structured_output(
            schema, **{**kwargs, "method": "json_schema"}
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.skipif(
    not os.getenv(_ENV_STRICT_MATRIX),
    reason=f"Strict-schema matrix is opt-in: set {_ENV_STRICT_MATRIX}=1 to regenerate it.",
)
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
async def test_report_strict_json_schema_matrix(
    model_type: ModelType, monkeypatch, usage_recorder
):
    """Record what each alias does under langchain's strict ``json_schema`` default.

    Deliberately assertion-free. Whether a provider rejects the strict schema
    outright or accepts it and quietly drops fields is a fact about that
    provider's validator: a test pinning it would go red the day they fix it,
    which is the opposite of what you want to hear. So this records the behaviour
    per alias and leaves the judgement to the reader — run it when the fleet
    changes, when langchain moves, or before proposing to drop the ``method``
    override.
    """
    real_create_model = model_factory.create_model
    monkeypatch.setattr(model_factory, "get_default_fast_model", lambda: model_type)
    monkeypatch.setattr(
        model_factory,
        "create_model",
        lambda *args, **kwargs: _ForceStrictJsonSchema(
            real_create_model(*args, **{**kwargs, "callbacks": [usage_recorder]})
        ),
    )

    try:
        entry = await _score_tool_via_llm(_TOOL_NAME, _TOOL_DESCRIPTION, _TOOL_SCHEMA)
    except Exception as exc:  # noqa: BLE001 — any provider rejection is a result, not an error
        verdict = f"REJECTED — {type(exc).__name__}: {str(exc)[:300]}"
    else:
        verdict = (
            f"ACCEPTED but risk_factors EMPTY (silent degradation), base_score={entry.base_score}"
            if not entry.risk_factors
            else f"ACCEPTED, base_score={entry.base_score}, factors={sorted(entry.risk_factors)}"
        )

    print(f"\n[strict json_schema] {model_type}: {verdict}")
    logger.warning("[strict json_schema] %s: %s", model_type, verdict)
