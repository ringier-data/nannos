"""Per-user MCP bearer tokens minted at call time, not at discovery time.

Why
---
Discovery used to exchange the user's token once per audience (gateway, console) and
bake the result into every tool's ``StreamableHttpConnection`` headers. Token freshness
was therefore tied to how long the discovered tools were kept — the discovery cache TTL
had to double as a token-safety bound, a sub-agent inheriting the orchestrator's tools
inherited a token of unknown age, and nothing could recover from an exchanged token
expiring in the middle of a long turn.

This module inverts that. A :class:`UserTokenProvider` is the single place that mints
exchanged tokens for one user: it memoises them per audience and re-exchanges when the
memoised one is within ``leeway`` seconds of its ``exp``. Reuse needs a readable ``exp``:
an opaque (non-JWT) token has no known lifetime and is therefore re-exchanged on every
ask — correct, just not cached. Tools carry **no** bearer in
their connection; :func:`bearer_interceptor` asks the provider for a token for the
tool's server on every call and injects the ``Authorization`` header for that call only
(the same ``request.override(headers=…)`` hook the console attribution interceptor
uses). Discovery asks the provider the same way when it needs a token to list.

Scope
-----
The provider is **per user**: it holds that user's subject token and nothing else, so
there is no cross-user cache to reason about. It is long-lived across that user's turns
(it rides in the per-user discovery-cache entry next to the tools built with it) and the
executor hands it the current subject token at the start of every turn
(:meth:`UserTokenProvider.update_subject_token`), so an exchange after the user's token
rotated uses the new one. If the *user's* token has expired mid-turn nothing can be
re-minted without the user — the exchange fails and the auth-error path surfaces it,
which is the correct outcome.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Serve a memoised exchanged token only while at least this many seconds remain, so a
# tool call that starts right before expiry still completes with a valid token.
DEFAULT_LEEWAY_S = 90.0

_SCOPES = ["openid", "profile", "offline_access"]


def jwt_exp(token: str) -> float | None:
    """Read ``exp`` (unix seconds) from a JWT without verifying it; ``None`` if unreadable.

    Only used to decide *when to re-mint*; trust in the token comes from the issuer.
    """
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment))
        exp = claims.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


@dataclass
class _Minted:
    token: str
    expires_at: float | None  # None: unknown lifetime → re-mint on every ask


class UserTokenProvider:
    """Mints and memoises exchanged bearer tokens for one user, per target audience.

    ``exchange`` is the OAuth client's ``exchange_token`` (RFC 8693); it is the only
    network call this class makes.
    """

    def __init__(
        self,
        subject_token: str,
        exchange: Callable[..., Awaitable[str]],
        *,
        leeway_seconds: float = DEFAULT_LEEWAY_S,
    ) -> None:
        self._subject_token = subject_token
        self._exchange = exchange
        self._leeway = leeway_seconds
        self._minted: dict[str, _Minted] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- subject token ---------------------------------------------------------------
    @property
    def subject_token(self) -> str:
        return self._subject_token

    def update_subject_token(self, subject_token: str) -> None:
        """Adopt the user's current token (called at every turn start).

        A different subject token invalidates every memoised exchange: they were derived
        from the old one and must not outlive it.
        """
        if subject_token != self._subject_token:
            self._subject_token = subject_token
            self._minted.clear()

    # -- exchanged tokens ------------------------------------------------------------
    def _fresh(self, audience: str) -> str | None:
        minted = self._minted.get(audience)
        if minted is None or minted.expires_at is None:
            return None
        if minted.expires_at - self._leeway <= time.time():
            return None
        return minted.token

    async def get(self, audience: str) -> str:
        """Return a bearer token for ``audience``, re-exchanging if none is fresh enough."""
        token = self._fresh(audience)
        if token is not None:
            return token
        lock = self._locks.setdefault(audience, asyncio.Lock())
        async with lock:
            token = self._fresh(audience)  # a concurrent caller may have minted meanwhile
            if token is not None:
                return token
            token = await self._exchange(
                subject_token=self._subject_token, target_client_id=audience, requested_scopes=_SCOPES
            )
            exp = jwt_exp(token)
            subject_exp = jwt_exp(self._subject_token)
            if exp is not None and subject_exp is not None:
                exp = min(exp, subject_exp)  # never reuse past the user token it came from
            self._minted[audience] = _Minted(token=token, expires_at=exp)
            logger.debug("Minted MCP bearer token for audience %s (exp=%s)", audience, exp)
            return token

    def invalidate(self, audience: str | None = None) -> None:
        if audience is None:
            self._minted.clear()
        else:
            self._minted.pop(audience, None)


def bearer_interceptor(
    provider: UserTokenProvider, audience_for_server: Callable[[str], str]
) -> Callable[[Any, Any], Awaitable[Any]]:
    """A ``langchain_mcp_adapters`` tool interceptor that injects a fresh bearer per call.

    ``audience_for_server`` maps the request's ``server_name`` to the OAuth audience
    (e.g. ``"console"`` → the console client id, anything else → the gateway client id).
    The header is added to *this call's* headers only; the connection itself stays
    token-free, so tools can be shared, cached and handed to sub-agents without carrying
    a credential.
    """

    async def _inject(request: Any, handler: Any) -> Any:
        token = await provider.get(audience_for_server(request.server_name))
        headers: Mapping[str, Any] = request.headers or {}
        return await handler(request.override(headers={**headers, "Authorization": f"Bearer {token}"}))

    return _inject
