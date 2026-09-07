"""UserTokenProvider: per-user exchanged bearers minted at call time, with expiry-aware reuse."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_common.core.token_provider import UserTokenProvider, bearer_interceptor, jwt_exp


def _jwt(exp: float, **claims) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp, **claims})}.sig"


def _exchange(lifetime: float = 900):
    """An exchange stub minting a JWT per audience, counting calls."""
    calls: list[str] = []

    async def exchange(*, subject_token, target_client_id, requested_scopes):
        calls.append(target_client_id)
        return _jwt(time.time() + lifetime, aud=target_client_id, n=len(calls))

    return exchange, calls


def test_jwt_exp_reads_or_returns_none():
    assert jwt_exp(_jwt(1234.0)) == 1234.0
    assert jwt_exp("opaque") is None
    assert jwt_exp(_jwt(1.0).rsplit(".", 1)[0] + ".x") == 1.0  # signature is irrelevant


@pytest.mark.asyncio
async def test_memoises_per_audience_and_reuses_while_fresh():
    exchange, calls = _exchange()
    p = UserTokenProvider(_jwt(time.time() + 3600), exchange)
    a1 = await p.get("gatana")
    a2 = await p.get("gatana")
    c1 = await p.get("agent-console")
    assert a1 == a2 and a1 != c1
    assert calls == ["gatana", "agent-console"]


@pytest.mark.asyncio
async def test_reminted_inside_the_leeway_window():
    exchange, calls = _exchange(lifetime=120)
    p = UserTokenProvider(_jwt(time.time() + 3600), exchange, leeway_seconds=90)
    t1 = await p.get("gatana")
    with patch("agent_common.core.token_provider.time.time", return_value=time.time() + 40):  # 80 s left < leeway
        t2 = await p.get("gatana")
    assert t1 != t2 and calls == ["gatana", "gatana"]


@pytest.mark.asyncio
async def test_never_reused_past_the_subject_tokens_expiry():
    exchange, calls = _exchange(lifetime=3600)
    p = UserTokenProvider(_jwt(time.time() + 100), exchange, leeway_seconds=90)  # user token nearly done
    await p.get("gatana")
    with patch("agent_common.core.token_provider.time.time", return_value=time.time() + 20):  # 80 s of user token left
        await p.get("gatana")
    assert calls == ["gatana", "gatana"]


@pytest.mark.asyncio
async def test_unparseable_token_is_reminted_every_time():
    calls = []

    async def exchange(**kw):
        calls.append(1)
        return "opaque-token"

    p = UserTokenProvider(_jwt(time.time() + 3600), exchange)
    assert await p.get("gatana") == await p.get("gatana") == "opaque-token"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_new_subject_token_invalidates_minted_tokens():
    exchange, calls = _exchange()
    subject = _jwt(time.time() + 3600, sub="u")
    p = UserTokenProvider(subject, exchange)
    await p.get("gatana")
    p.update_subject_token(subject)  # identical → keep
    await p.get("gatana")
    assert calls == ["gatana"]
    p.update_subject_token(_jwt(time.time() + 7200, sub="u"))  # rotated → re-mint
    await p.get("gatana")
    assert calls == ["gatana", "gatana"]


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_exchange():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def exchange(**kw):
        calls.append(1)
        started.set()
        await release.wait()
        return _jwt(time.time() + 900)

    p = UserTokenProvider(_jwt(time.time() + 3600), exchange)
    tasks = [asyncio.create_task(p.get("gatana")) for _ in range(5)]
    await started.wait()
    release.set()
    tokens = await asyncio.gather(*tasks)
    assert len(set(tokens)) == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_interceptor_injects_a_bearer_for_the_servers_audience():
    exchange, calls = _exchange()
    p = UserTokenProvider(_jwt(time.time() + 3600), exchange)
    intercept = bearer_interceptor(p, lambda server: "agent-console" if server == "console" else "gatana")
    seen = {}

    class Req(SimpleNamespace):
        def override(self, **kw):
            return Req(**{**self.__dict__, **kw})

    async def handler(req):
        seen[req.server_name] = req.headers
        return "ok"

    await intercept(Req(server_name="github", headers={"x-nannos-context": "c1"}), handler)
    await intercept(Req(server_name="console", headers=None), handler)
    assert seen["github"]["x-nannos-context"] == "c1"  # existing headers preserved
    assert seen["github"]["Authorization"].startswith("Bearer ") and "gatana" in _claims(seen["github"]["Authorization"])["aud"]
    assert _claims(seen["console"]["Authorization"])["aud"] == "agent-console"
    assert calls == ["gatana", "agent-console"]


def _claims(bearer: str) -> dict:
    seg = bearer.removeprefix("Bearer ").split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


@pytest.mark.asyncio
async def test_exchange_failure_propagates_and_caches_nothing():
    exchange = AsyncMock(side_effect=RuntimeError("keycloak down"))
    p = UserTokenProvider(_jwt(time.time() + 3600), exchange)
    with pytest.raises(RuntimeError):
        await p.get("gatana")
    assert p._minted == {}
