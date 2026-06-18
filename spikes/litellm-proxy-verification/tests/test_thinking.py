"""Check 1 — extended thinking via reasoning_effort reaches Bedrock Claude 4.6 (ADR-0003).

Verifies: no 400 (thinking.type/budget rejection), thinking actually happens
(reasoning tokens > 0), and quantifies the resolved budget per effort level —
the empirical "medium != 10k tokens" number ADR-0003 wants validated.
"""

import pytest

MODEL = "claude-sonnet-4.6"


def _reasoning_tokens(usage) -> int | None:
    details = getattr(usage, "completion_tokens_details", None)
    if details is None:
        return None
    return getattr(details, "reasoning_tokens", None)


@pytest.mark.integration
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_reasoning_effort_passthrough(aclient, effort):
    resp = await aclient.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Think step by step: what is 17 * 23? Show brief reasoning."}],
        max_tokens=2048,
        extra_body={"reasoning_effort": effort},
    )
    msg = resp.choices[0].message
    # reasoning content surfaces either as reasoning_content or thinking_blocks
    reasoning_content = getattr(msg, "reasoning_content", None) or (msg.model_extra or {}).get("reasoning_content")
    thinking_blocks = (msg.model_extra or {}).get("thinking_blocks")
    rtoks = _reasoning_tokens(resp.usage)

    print(f"\n[Check1] effort={effort} reasoning_tokens={rtoks} "
          f"has_reasoning_content={bool(reasoning_content)} has_thinking_blocks={bool(thinking_blocks)}")

    assert resp.choices[0].message.content, "no answer content returned"
    assert (rtoks and rtoks > 0) or reasoning_content or thinking_blocks, (
        f"effort={effort}: no evidence of thinking (reasoning_tokens={rtoks})"
    )


@pytest.mark.integration
async def test_minimal_maps_to_low(aclient):
    """App maps thinking_level 'minimal' -> reasoning_effort 'low'; must not 400 (Bedrock floors at 1024)."""
    resp = await aclient.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Reply with one word."}],
        max_tokens=1500,
        extra_body={"reasoning_effort": "low"},
    )
    assert resp.choices[0].message.content
