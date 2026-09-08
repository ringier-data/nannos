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
