"""What a user is missing to have scheduled jobs that run and reach them (#311).

Everything here is derived from state other parts of Nannos already keep: the user's
identity and vaulted offline token, and their subscriptions, whose every stop is a
``pause_code`` (#192). Nothing is stored, so there is nothing to keep in sync.

The one definition is `onboarding_issues_sql`; the user list, its filters and summary, the
user detail and the group members all read it through `onboarding_sql`.

**Two scopes.** Unscoped, it covers every subscription of the user: that is the
administrator's Users page and user detail. Scoped to a group, it covers only what that
group enabled for its members, the subscriptions its default jobs activated
(``activated_by_groups`` holds the group): that is the group page, which group managers
use. A member's sign-in state is theirs, so it is reported in both, but in a group's scope
it is only ``blocking`` when one of the group's own jobs waits on it. The scope is also a
privacy boundary: a group manager must not learn the names of jobs a member runs outside
their group.

A subscription contributes to at most one issue, the first that applies:

1. held for a sign-in (``awaiting_sign_in``, ``sign_in_expired``, ``no_offline_token``),
   or switched on while the user has no live token: counted under the user's sign-in issue;
2. held ``unreachable`` / ``undelivered`` / ``agent_inaccessible`` / ``access_revoked``;
3. switched on over a channel `reachability_sql` calls ``unreachable`` or ``unknown``.

Anything else that is off (paused by the user, auto-paused, a one-shot that ran) would not
run anyway, and is not an onboarding matter; nor is any job whose definition is suspended
(it runs for nobody until a writer resumes it).
"""

from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import (
    SCIM_PLACEHOLDER_SUB_PREFIX,
    SEVERITY_RANK,
    IssueSeverity,
    OnboardingIssueKind,
    OnboardingSummary,
    OnboardingSummaryEntry,
)
from .delivery_reachability_repository import reachability_sql

if TYPE_CHECKING:
    from ..models.scheduled_job import PauseCode


def _values(*items: "PauseCode | OnboardingIssueKind") -> str:
    return ", ".join(f"'{item.value}'" for item in items)


def _offline_token_state_sql(user_id: str) -> str:
    """The state of *user_id*'s (a SQL expression) vaulted token: 'live' (what
    `SchedulerTokenService.has_consent` counts), 'expired' (marked by `mark_expired`), or
    NULL when none was ever stored."""
    return (
        "(SELECT CASE WHEN uot.expired_at IS NULL THEN 'live' ELSE 'expired' END"
        f" FROM user_offline_tokens uot WHERE uot.user_id = {user_id})"
    )


_DELIVERY_KINDS = _values(
    OnboardingIssueKind.UNREACHABLE,
    OnboardingIssueKind.UNDELIVERED,
    OnboardingIssueKind.UNKNOWN_REACHABILITY,
)


def _holds() -> tuple[str, str]:
    """The pause codes that are sign-in holds, and those that are job holds. Imported at
    call time: `models.scheduled_job` loads the services package, which imports this
    module, so a module-level import breaks whenever this module is imported first."""
    from ..models.scheduled_job import PauseCode

    job_holds = (
        PauseCode.UNREACHABLE,
        PauseCode.UNDELIVERED,
        PauseCode.AGENT_INACCESSIBLE,
        PauseCode.ACCESS_REVOKED,
    )
    # A job hold's code doubles as its issue kind; a rename must not split them.
    assert {code.value for code in job_holds} <= {
        kind.value for kind in OnboardingIssueKind
    }
    return (
        _values(
            PauseCode.AWAITING_SIGN_IN,
            PauseCode.SIGN_IN_EXPIRED,
            PauseCode.NO_OFFLINE_TOKEN,
        ),
        _values(*job_holds),
    )


_JOBS = "jsonb_agg(jsonb_build_object('id', s.job_id, 'name', s.job_name) ORDER BY s.job_name, s.job_id)"


def onboarding_issues_sql(user_id: str, group_id: str | None = None) -> str:
    """A query with one row per issue of *user_id* (a SQL expression), shaped like
    `OnboardingIssue`. No rows for a service account. With *group_id* (a SQL expression),
    only the subscriptions that group activated count.

    Its aliases all start with ``o`` so *user_id* can name an outer ``u``."""
    sign_in_holds, job_holds = _holds()
    in_scope = (
        ""
        if group_id is None
        else f"AND COALESCE(os.activated_by_groups, '[]'::jsonb) @> jsonb_build_array({group_id})"
    )
    return f"""
        WITH who AS (
            SELECT ow.id, ow.sub, {_offline_token_state_sql("ow.id")} AS token
            FROM users ow
            WHERE ow.id = {user_id} AND NOT ow.is_service_account
        ), subs AS (
            SELECT od.id AS job_id, od.name AS job_name, od.sub_agent_id,
                   oc.client_id, oc.workspace_id, oc.name AS channel_name,
                   CASE
                       WHEN os.pause_code IN ({sign_in_holds}) THEN 'sign_in'
                       WHEN os.pause_code IN ({job_holds}) THEN os.pause_code
                       WHEN NOT os.enabled THEN NULL
                       WHEN who.token IS DISTINCT FROM 'live' THEN 'sign_in'
                       WHEN oc.id IS NULL THEN NULL
                       ELSE CASE {reachability_sql("who.id", "oc.client_id", "oc.workspace_id")}
                           WHEN 'unreachable' THEN '{OnboardingIssueKind.UNREACHABLE.value}'
                           WHEN 'unknown' THEN '{OnboardingIssueKind.UNKNOWN_REACHABILITY.value}'
                       END
                   END AS kind
            FROM who
            JOIN scheduled_job_subscriptions os ON os.user_id = who.id AND os.deleted_at IS NULL {in_scope}
            JOIN scheduled_job_definitions od ON od.id = os.definition_id AND od.deleted_at IS NULL
                 AND od.suspended_at IS NULL
            LEFT JOIN delivery_channels oc ON oc.id = os.delivery_channel_id
        )
        SELECT CASE
                   WHEN starts_with(who.sub, '{SCIM_PLACEHOLDER_SUB_PREFIX}')
                       THEN '{OnboardingIssueKind.NOT_SIGNED_IN.value}'
                   WHEN who.token = 'expired' THEN '{OnboardingIssueKind.SIGN_IN_EXPIRED.value}'
                   ELSE '{OnboardingIssueKind.SCHEDULER_NOT_READY.value}'
               END AS kind,
               CASE WHEN count(s.job_id) > 0 THEN '{IssueSeverity.BLOCKING.value}'
                    ELSE '{IssueSeverity.PENDING.value}' END AS severity,
               NULL::text AS client_id, NULL::text AS client_name, NULL::text AS workspace_id,
               '{{}}'::text[] AS channel_names, NULL::int AS agent_id, NULL::text AS agent_name,
               COALESCE({_JOBS} FILTER (WHERE s.job_id IS NOT NULL), '[]'::jsonb) AS jobs
        FROM who
        LEFT JOIN subs s ON s.kind = 'sign_in'
        WHERE who.token IS DISTINCT FROM 'live'
        GROUP BY who.sub, who.token
        UNION ALL
        SELECT s.kind,
               CASE WHEN s.kind = '{OnboardingIssueKind.UNKNOWN_REACHABILITY.value}'
                    THEN '{IssueSeverity.INFO.value}' ELSE '{IssueSeverity.BLOCKING.value}' END,
               s.client_id, COALESCE(obc.name, s.client_id), s.workspace_id,
               COALESCE(array_agg(DISTINCT s.channel_name) FILTER (WHERE s.channel_name IS NOT NULL), '{{}}'),
               s.agent_id, osa.name,
               {_JOBS}
        FROM (
            SELECT kind, job_id, job_name,
                   CASE WHEN kind IN ({_DELIVERY_KINDS}) THEN client_id END AS client_id,
                   CASE WHEN kind IN ({_DELIVERY_KINDS}) THEN workspace_id END AS workspace_id,
                   CASE WHEN kind IN ({_DELIVERY_KINDS}) THEN channel_name END AS channel_name,
                   CASE WHEN kind = '{OnboardingIssueKind.AGENT_INACCESSIBLE.value}' THEN sub_agent_id END AS agent_id
            FROM subs
            WHERE kind IS NOT NULL AND kind <> 'sign_in'
        ) s
        LEFT JOIN broker_clients obc ON obc.client_id = s.client_id
        LEFT JOIN sub_agents osa ON osa.id = s.agent_id
        GROUP BY s.kind, s.client_id, obc.name, s.workspace_id, s.agent_id, osa.name"""


_RANK = (
    f"CASE oi.severity WHEN '{IssueSeverity.BLOCKING.value}' THEN {SEVERITY_RANK[IssueSeverity.BLOCKING]}"
    f" WHEN '{IssueSeverity.PENDING.value}' THEN {SEVERITY_RANK[IssueSeverity.PENDING]} ELSE 0 END"
)


def onboarding_sql(user_id: str, group_id: str | None = None) -> str:
    """A query with exactly one row for *user_id* (a SQL expression): ``issues`` (a JSON
    array of `OnboardingIssue`, worst first), ``severity_rank`` (`SEVERITY_RANK` of the
    worst non-info issue, 0 for none) and ``affected_jobs`` (jobs stopped by non-info
    issues). Meant for ``CROSS JOIN LATERAL``, or ``to_jsonb`` of it as a column.
    *group_id* scopes it to what that group enabled (see the module docstring)."""
    return f"""
        SELECT COALESCE(
                   jsonb_agg(to_jsonb(oi) ORDER BY {_RANK} DESC, jsonb_array_length(oi.jobs) DESC, oi.kind,
                             oi.client_name, oi.workspace_id, oi.agent_name),
                   '[]'::jsonb) AS issues,
               COALESCE(max({_RANK}), 0) AS severity_rank,
               COALESCE(sum(jsonb_array_length(oi.jobs)) FILTER (WHERE {_RANK} > 0), 0) AS affected_jobs
        FROM ({onboarding_issues_sql(user_id, group_id)}) oi"""


def onboarding_column_sql(user_id: str, group_id: str | None = None) -> str:
    """`onboarding_sql` as one JSON column, for listings that only show it."""
    return f"(SELECT to_jsonb(ob) FROM ({onboarding_sql(user_id, group_id)}) ob)"


#: The severity sort over an `onboarding_sql` row aliased ``ob``: worst first, then the most
#: jobs stopped. Callers append their own tie-break.
ONBOARDING_SORT = "ob.severity_rank DESC, ob.affected_jobs DESC"


def onboarding_conditions(
    severity: list[IssueSeverity] | None,
    issue: list[OnboardingIssueKind] | None,
    client_id: str | None,
) -> tuple[list[str], dict[str, Any]]:
    """WHERE conditions over an `onboarding_sql` row aliased ``ob``, for the listings'
    onboarding filters. *severity* matches the user's worst non-info issue, so an info-only
    filter matches nobody. *issue* and *client_id* must hold for the same issue."""
    conditions: list[str] = []
    params: dict[str, Any] = {}
    if severity:
        conditions.append("ob.severity_rank = ANY(:severity_ranks)")
        params["severity_ranks"] = sorted(
            {SEVERITY_RANK[s] for s in severity if s in SEVERITY_RANK}
        )
    matches = []
    if issue:
        matches.append("oe->>'kind' = ANY(:issue_kinds)")
        params["issue_kinds"] = [k.value for k in issue]
    if client_id:
        matches.append("oe->>'client_id' = :client_id")
        params["client_id"] = client_id
    if matches:
        conditions.append(
            f"EXISTS (SELECT 1 FROM jsonb_array_elements(ob.issues) oe WHERE {' AND '.join(matches)})"
        )
    return conditions, params


async def summarize_onboarding(
    db: AsyncSession,
    where_clause: str,
    params: dict[str, Any],
    joins: str = "",
    group_id: str | None = None,
) -> OnboardingSummary:
    """How many of the users ``users u {joins} {where_clause}`` keeps have each severity,
    and each issue (per client for delivery issues). *group_id* scopes the onboarding like
    `onboarding_sql`."""
    rows = (
        await db.execute(
            text(f"""
                WITH ob AS (
                    SELECT u.id, o.issues, o.severity_rank
                    FROM users u
                    {joins}
                    CROSS JOIN LATERAL ({onboarding_sql("u.id", group_id)}) o
                    {where_clause}
                )
                SELECT NULL AS kind, NULL AS client_id, NULL AS client_name, severity_rank, COUNT(*) AS users
                FROM ob WHERE severity_rank > 0 GROUP BY severity_rank
                UNION ALL
                SELECT oe->>'kind', oe->>'client_id', max(oe->>'client_name'), NULL, COUNT(DISTINCT ob.id)
                FROM ob CROSS JOIN LATERAL jsonb_array_elements(ob.issues) oe
                GROUP BY oe->>'kind', oe->>'client_id'
            """),
            params,
        )
    ).mappings()
    by_rank: dict[int, int] = {}
    entries: list[OnboardingSummaryEntry] = []
    for row in rows:
        if row["kind"] is None:
            by_rank[row["severity_rank"]] = row["users"]
        else:
            entries.append(
                OnboardingSummaryEntry(
                    kind=OnboardingIssueKind(row["kind"]),
                    client_id=row["client_id"],
                    client_name=row["client_name"],
                    users=row["users"],
                )
            )
    entries.sort(key=lambda e: (-e.users, e.kind.value, e.client_name or ""))
    return OnboardingSummary(
        blocking=by_rank.get(SEVERITY_RANK[IssueSeverity.BLOCKING], 0),
        pending=by_rank.get(SEVERITY_RANK[IssueSeverity.PENDING], 0),
        issues=entries,
    )
