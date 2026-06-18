"""Check 4 — attribution transport + concurrency isolation (ADR-0002).

The ContextVar->httpx-header hook must (a) carry all Nannos fields to the proxy,
(b) stay correct under concurrent async requests sharing ONE cached client (the
real risk, since there's no thread pool around LLM calls), and (c) the to_thread
boundary rule must be understood.
"""

import asyncio
import contextvars

import pytest
from conftest import MASTER_KEY, PROXY_URL

from attribution_hook import attribution, current_attribution, make_chat_client


def _md(record) -> dict:
    return record.get("spend_logs_metadata") or {}


async def test_4a_round_trip(captures):
    client = make_chat_client(PROXY_URL, MASTER_KEY, model="mock-fast")
    with attribution(
        user_sub="u1", conversation_id="c1", sub_agent_id=42, scheduled_job_id=7, sub_agent_config_version_id=99
    ):
        await client.ainvoke("hello")

    recs = await captures.wait(1, timeout=30)
    md = _md(recs[-1])
    print(f"\n[Check4a] spend_logs_metadata={md} raw_metadata_keys={list((recs[-1].get('raw_metadata') or {}).keys())}")
    assert str(md.get("user_sub")) == "u1"
    assert str(md.get("conversation_id")) == "c1"
    assert str(md.get("sub_agent_id")) == "42"
    assert str(md.get("scheduled_job_id")) == "7"
    assert str(md.get("sub_agent_config_version_id")) == "99"

    # Persisted into the SpendLogs table too (batch-flushed, so poll).
    assert await captures.wait_persisted("u1", timeout=40), (
        "spend_logs_metadata.user_sub=u1 never persisted to the SpendLogs table"
    )


async def test_4b_concurrency_isolation(captures):
    """50 concurrent calls sharing one client; each must carry only its own attribution."""
    client = make_chat_client(PROXY_URL, MASTER_KEY, model="mock-fast")
    N = 50

    async def worker(i: int):
        with attribution(user_sub=f"u{i}", sub_agent_id=i):
            await client.ainvoke("hi")

    await asyncio.gather(*(worker(i) for i in range(N)))

    recs = await captures.wait(N, timeout=60)
    mds = [_md(r) for r in recs if _md(r)]
    print(f"\n[Check4b] captured={len(recs)} with_metadata={len(mds)}")

    # Invariant baked into the test data: sub_agent_id == int(user_sub[1:]).
    bleed = [m for m in mds if int(m["sub_agent_id"]) != int(str(m["user_sub"])[1:])]
    assert not bleed, f"attribution bleed across concurrent requests: {bleed[:5]}"
    seen = {int(m["sub_agent_id"]) for m in mds}
    assert seen == set(range(N)), f"missing/duplicated attributions; got {len(seen)} distinct of {N}"


async def test_4c_threadpool_boundary_rule():
    """Document the ContextVar/thread rule (no network).

    - asyncio.to_thread copies the context  -> attribution preserved (the sandbox path is safe)
    - raw run_in_executor does NOT copy      -> attribution lost
    - run_in_executor + copy_context().run   -> preserved
    """
    loop = asyncio.get_running_loop()
    with attribution(user_sub="x", sub_agent_id=1):
        via_to_thread = await asyncio.to_thread(current_attribution)
        via_raw_executor = await loop.run_in_executor(None, current_attribution)
        ctx = contextvars.copy_context()
        via_copied_ctx = await loop.run_in_executor(None, lambda: ctx.run(current_attribution))

    print(f"\n[Check4c] to_thread={via_to_thread} raw_executor={via_raw_executor} copied_ctx={via_copied_ctx}")
    assert via_to_thread.get("user_sub") == "x", "to_thread should copy context (sandbox path safe)"
    assert via_raw_executor == {}, "raw run_in_executor should NOT see contextvars"
    assert via_copied_ctx.get("user_sub") == "x", "copy_context().run should preserve attribution"
