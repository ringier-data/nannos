"""Integration tests: "thinking off" actually turns thinking off, on every alias the gateway serves.

Clients ask for it with ``reasoning_effort: "none"`` alone; the gateway adds the provider's own
off switch per deployment (litellm-proxy ``_apply_thinking_off``). How that lands is decided by
live models and the LiteLLM version, neither of which a unit test controls:

  * Claude 5 thinks by default and LiteLLM turns "none" into "no thinking parameter". Without
    the gateway's explicit ``thinking: disabled`` the model spends the small utility budget
    thinking and replies with nothing, cut off at ``finish_reason=length``.
  * Gemini 3 refuses ``thinking`` next to ``reasoning_effort`` with a 400 ("Cannot specify
    both"), so the gateway must also remove the switch from a client that still sends it.
  * The OpenAI SDK rejects a ``thinking`` keyword before sending, so it must never ride in
    ``model_kwargs`` (nannos#277).

A gateway without the hook, or a LiteLLM bump that moves the translation, fails here on the
affected family.

Run with: RUN_INTEGRATION_TESTS=1 uv run pytest tests/integration/test_thinking_off.py -m integration -v
(the env opt-in because a path argument currently defeats `-m integration` discovery: the
directory's conftest is imported before `pytest_configure` records the marker expression)
"""

import os

import httpx
import pytest
from agent_common.core.model_factory import REASONING_OFF, create_fast_model
from agent_common.models.base import ModelType
from langsmith import testing as t

from .conftest import ALL_MODELS

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Tempts a reasoning model to think, with a one-word answer. With thinking on, Claude 5 spends
# the whole fast-model budget before answering; with it off, the reply is a handful of tokens.
_PROMPT = "Is 1000003 a prime number? Answer with yes or no only, nothing else."


@pytest.mark.strict
@pytest.mark.langsmith
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
async def test_the_fast_model_answers_within_its_budget(
    model_type: ModelType, usage_recorder
):
    """``create_fast_model`` (thinking off, 1024-token cap) gets a finished answer back.

    ``strict``: thinking-off is a switch, not a probability. A 400, an SDK TypeError or an
    empty reply cut off at ``length`` is the regression, on whichever family it hits.
    """
    t.log_inputs({"prompt": _PROMPT, "model": model_type})

    llm = create_fast_model(model_type)
    reply = await llm.ainvoke(_PROMPT, config={"callbacks": [usage_recorder]})

    finish_reason = reply.response_metadata.get("finish_reason")
    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    t.log_outputs(
        {"text": text, "finish_reason": finish_reason, "usage": reply.usage_metadata}
    )

    assert finish_reason != "length", (
        f"{model_type} was cut off at max_tokens with thinking off ({len(text)} chars): "
        "it is still thinking, so the gateway did not switch thinking off for it"
    )
    assert text.strip(), f"{model_type} returned an empty reply with thinking off"


@pytest.mark.strict
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
async def test_a_client_that_still_sends_the_provider_switch_is_not_refused(
    model_type: ModelType,
):
    """The wire shape clients sent before nannos#277 (``reasoning_effort: "none"`` plus
    ``thinking: disabled``) is accepted on every family.

    Raw HTTP on purpose: this is what a not-yet-redeployed console-backend sends, and it
    must not reach Gemini 3 as the pair it refuses. The gateway removes the switch there.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{os.environ['LLM_GATEWAY_URL'].rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['LLM_GATEWAY_API_KEY']}"},
            json={
                "model": model_type,
                "messages": [{"role": "user", "content": _PROMPT}],
                "max_tokens": 1024,
                "reasoning_effort": REASONING_OFF,
                "thinking": {"type": "disabled"},
            },
        )

    assert resp.status_code == 200, (
        f"{model_type}: {resp.status_code} {resp.text[:300]}"
    )
    choice = (resp.json().get("choices") or [{}])[0]
    assert choice.get("finish_reason") != "length", (
        f"{model_type} was cut off at max_tokens"
    )
    assert (choice.get("message") or {}).get("content"), (
        f"{model_type} returned no content"
    )
