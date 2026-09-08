"""Who a console-backend gateway call bills to, and which service's work it was.

Two things the gateway needs that console-backend cannot state casually.

**The subject.** The proxy attributes spend by OIDC subject. Internally we key users by
``users.id``, which for anyone onboarded before the id/sub split *is* their sub and for
everyone after is a UUID that is not. Both `scheduled_jobs.user_id` and the socket
session's `user_id` are that internal id, so a caller holding one has to resolve the
subject rather than pass it through. (The usage ingest happens to accept either — it
falls back to an id lookup for legacy callers — but LiteLLM's own spend table does no
such resolution, and the header's contract is a subject.)

**The service.** The usage views break spend down by service, and derived it from
whichever id was set: a `scheduled_job_id` meant 'scheduler', a `catalog_id` without a
conversation meant 'catalog', everything else fell through to 'orchestrator'. A console
utility call carries none of those — naming a conversation, drafting a scheduled job —
so console-backend's own overhead was booked as agent spend. Callers that know what they
are declare it with the constants below; the derivation in
``usage_repository.get_usage_by_service`` stays as the fallback for everything else, so
these strings must keep agreeing with the ones it produces.

**Which mechanism.** Console-backend's own work runs inside an ``attribution_scope`` — the
scheduler's dispatch, a catalog sync, a titling task, a console request handler. The scope
is set once where the work begins, so every gateway call under it is attributed by
construction and a call added later cannot forget: forgetting does not misclassify the
spend, it loses it, because the proxy discards a record carrying no subject. Per-call
``metadata`` is for the exception — a call billed to someone other than whoever the
enclosing block runs as, which today means ``console_web_search``: a step in an agent's
turn that this service merely hosts, carrying the caller's forwarded context.
"""

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: console-backend's own utility calls — conversation titling, scheduled-job drafting,
#: watch-condition generation. Work the console does for a user, not an agent run.
SERVICE_CONSOLE = "console"

#: The scheduler deciding and announcing on its own: the watch judge, the notification
#: writer. Recurring, unattended, and the spend most worth telling apart from the rest.
SERVICE_SCHEDULER = "scheduler"

#: Catalog ingestion — document summarization during a sync.
SERVICE_CATALOG = "catalog"


async def billing_subject(db: Any, user_id: str, *, context: str = "") -> str:
    """What to bill ``user_id``'s work to: their OIDC subject, or the internal id itself
    when the subject cannot be read.

    The fallback is deliberate and is the reason this exists as its own function. The
    subject is the field's contract and what LiteLLM's own spend table keeps, so it is
    always preferred — but the usage ingest resolves *either* (`get_user_by_sub(...) or
    get_user(...)`, a fallback whose comment names the callers that pass an internal id).
    Given a failed lookup, a wrong-shaped-but-resolvable value bills the right person,
    while `None` drops the record on the floor: the proxy's logger discards anything with
    no subject at all. That trade is the same on every path, so it is made once here
    rather than remembered at three call sites.
    """
    return await resolve_user_sub(db, user_id, context=context) or user_id


async def resolve_user_sub(db: Any, user_id: str, *, context: str = "") -> str | None:
    """The OIDC subject of ``users.id``, or None when it cannot be read.

    None is not a failure: an unattributed call is worse accounting than an attributed
    one, but no user-facing work should stop because a lookup came back empty.

    Rolls back on error, which is the part that is easy to miss. Callers often pass a
    session that the rest of their work continues on, and a failed statement leaves it
    refusing every next one (``PendingRollbackError``) — so swallowing the error without
    rolling back turns a best-effort lookup into a fatal one.
    """
    where = f" ({context})" if context else ""
    try:
        result = await db.execute(text("SELECT sub FROM users WHERE id = :user_id"), {"user_id": user_id})
        sub = result.scalar_one_or_none()
    except Exception:
        logger.warning("Could not resolve the subject of user %s%s; LLM spend goes unattributed", user_id, where)
        try:
            await db.rollback()
        except Exception:
            logger.warning("Rollback after the failed subject lookup for %s%s failed too", user_id, where, exc_info=True)
        return None
    if not sub:
        logger.warning("User %s has no subject on file%s; LLM spend goes unattributed", user_id, where)
    return sub
