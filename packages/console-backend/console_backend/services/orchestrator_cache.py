"""Best-effort, scoped invalidation of the orchestrator's per-user discovery/registry caches.

The orchestrator memoizes capability discovery (MCP tools + sub-agents) and the registry
user lookup per user, keyed by the inputs that determine entitlements and bounded by a TTL.
When an admin or group manager changes a group→MCP-server / group→default-agent mapping, or
a per-user entitlement (role, tool whitelist, bypass rules), the affected users' entitlements
change *without* their groups/config necessarily changing, so the cache would otherwise serve
stale tools/sub-agents until the TTL lapses.

This nudges the orchestrator to flush immediately. It is best-effort by design: any failure
is logged and swallowed so it can never block the triggering action, and the orchestrator's
TTL remains the correctness floor.

Scoping: the caller passes the ``user_subs`` whose entitlements changed (a single user, or
the members of an affected group). The orchestrator drops only those users' entries, so one
group edit never evicts every active user's cache. Pass ``user_subs=None`` only for a
deliberate fleet-wide flush.

Auth: a trusted service-to-service call. console-backend authenticates with the OIDC
client-credentials grant (its own client id/secret, via the shared ``app.state.oauth_service``)
to mint a token for the orchestrator's audience; the orchestrator validates that JWT and
additionally checks the token's ``azp`` is console-backend's client. No user token, session
state, or per-user permission is involved, and there is no bespoke shared secret.

Dispatch: callers schedule this via ``schedule_orchestrator_discovery_cache_invalidation`` so
it runs as a FastAPI background task — the token mint + cross-service POST never block the
triggering request's response.

Multi-replica note: the orchestrator cache is in-process, so a single POST flushes the one
replica that receives it. Behind a load balancer the other replicas fall back to the TTL
(kept short for this reason) or an ``ENTITLEMENT_POLICY_VERSION`` bump for a fleet-wide flush.
A fan-out/pub-sub broadcast is the follow-up for instant fleet-wide invalidation.
"""

import logging

import httpx
from fastapi import BackgroundTasks, Request
from ringier_a2a_sdk.oauth import OidcOAuth2Client

from ..config import config

logger = logging.getLogger(__name__)

_INVALIDATE_PATH = "/internal/discovery-cache/invalidate"


def _orchestrator_base_url() -> str | None:
    """Build the orchestrator base URL from config, or None if not configured."""
    domain = config.orchestrator.base_domain
    if not domain:
        return None
    schema = "http" if config.orchestrator.is_local() or "localhost" in domain else "https"
    return f"{schema}://{domain}"


async def invalidate_orchestrator_discovery_cache(
    oauth_client: OidcOAuth2Client,
    reason: str,
    user_subs: list[str] | None = None,
) -> None:
    """Tell the orchestrator to drop cached discovery + registry records (best-effort, scoped).

    Args:
        oauth_client: the shared, long-lived OIDC client (``app.state.oauth_service``); reused
            so we don't leak an httpx pool or re-mint a service token on every call.
        reason: human-readable reason, logged for traceability.
        user_subs: the users whose entitlements changed. An empty list means "no affected
            users" → no-op. ``None`` means a deliberate fleet-wide flush.
    """
    if user_subs is not None and len(user_subs) == 0:
        logger.debug("No affected users for cache invalidation (%s); skipping", reason)
        return

    base = _orchestrator_base_url()
    if not base:
        logger.debug("Orchestrator base URL not configured; skipping cache invalidation (%s)", reason)
        return

    target_client_id = config.orchestrator.client_id
    if not target_client_id:
        logger.debug("ORCHESTRATOR_CLIENT_ID not configured; skipping cache invalidation (%s)", reason)
        return

    try:
        service_token = await oauth_client.get_token(audience=target_client_id)
        payload: dict = {} if user_subs is None else {"user_subs": user_subs}
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{base}{_INVALIDATE_PATH}",
                headers={"Authorization": f"Bearer {service_token}"},
                json=payload,
            )
            resp.raise_for_status()
        scope = "all" if user_subs is None else f"{len(user_subs)} user(s)"
        logger.info("Invalidated orchestrator discovery cache (%s; scope=%s)", reason, scope)
    except Exception as e:  # noqa: BLE001 — best-effort; must never block the triggering action
        logger.warning("Failed to invalidate orchestrator discovery cache (%s): %s", reason, e)


def schedule_orchestrator_discovery_cache_invalidation(
    background_tasks: BackgroundTasks,
    request: Request,
    reason: str,
    user_subs: list[str] | None = None,
) -> None:
    """Schedule a best-effort, scoped cache invalidation to run after the response is sent.

    Pulls the shared OIDC client off ``app.state`` and enqueues the POST as a background task,
    so the triggering request never waits on the token mint or the cross-service call.
    """
    if user_subs is not None and len(user_subs) == 0:
        return  # nothing to invalidate
    oauth_client = getattr(request.app.state, "oauth_service", None)
    if oauth_client is None:
        logger.debug("No oauth_service on app.state; skipping cache invalidation (%s)", reason)
        return
    background_tasks.add_task(invalidate_orchestrator_discovery_cache, oauth_client, reason, user_subs)
