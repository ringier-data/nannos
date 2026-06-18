"""Best-effort invalidation of the orchestrator's per-user discovery/registry caches.

The orchestrator memoizes capability discovery (MCP tools + sub-agents) and the registry
user lookup per user, keyed by the inputs that determine entitlements and bounded by a TTL.
When an admin or group manager changes a group→MCP-server or group→default-agent mapping,
the affected users' entitlements change *without* their groups/config changing, so the cache
would otherwise serve stale tools/sub-agents until the TTL lapses.

This nudges the orchestrator to flush immediately. It is best-effort by design: any failure
is logged and swallowed so it can never block the triggering action, and the orchestrator's
TTL remains the correctness floor.

Auth: a trusted service-to-service call. console-backend authenticates with the OIDC
client-credentials grant (its own client id/secret) to mint a token for the orchestrator's
audience; the orchestrator validates that JWT via its standard middleware. This is
independent of which user triggered the change (admin or group manager) — no user token,
session state, or per-user permission is involved, and there is no bespoke shared secret.

Multi-replica note: the orchestrator cache is in-process, so a single POST flushes the one
replica that receives it. Behind a load balancer the other replicas fall back to the TTL
(or an ``ENTITLEMENT_POLICY_VERSION`` bump for a fleet-wide flush). A fan-out/pub-sub
broadcast is the follow-up for instant fleet-wide invalidation.
"""

import logging

import httpx
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


async def invalidate_orchestrator_discovery_cache(reason: str) -> None:
    """Tell the orchestrator to drop its cached discovery + registry records (best-effort).

    Authenticated service-to-service via the OIDC client-credentials grant (see module
    docstring): no user token is involved, so this works regardless of whether an admin or
    a group manager triggered the underlying entitlement change.
    """
    base = _orchestrator_base_url()
    if not base:
        logger.debug("Orchestrator base URL not configured; skipping cache invalidation (%s)", reason)
        return

    target_client_id = config.orchestrator.client_id
    if not target_client_id:
        logger.debug("ORCHESTRATOR_CLIENT_ID not configured; skipping cache invalidation (%s)", reason)
        return

    try:
        oauth2_client = OidcOAuth2Client(
            client_id=config.oidc.client_id,
            client_secret=config.oidc.client_secret.get_secret_value(),
            issuer=config.oidc.issuer,
        )
        service_token = await oauth2_client.get_token(audience=target_client_id)
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                f"{base}{_INVALIDATE_PATH}",
                headers={"Authorization": f"Bearer {service_token}"},
            )
            resp.raise_for_status()
        logger.info("Invalidated orchestrator discovery cache (%s)", reason)
    except Exception as e:  # noqa: BLE001 — best-effort; must never block the triggering action
        logger.warning("Failed to invalidate orchestrator discovery cache (%s): %s", reason, e)
