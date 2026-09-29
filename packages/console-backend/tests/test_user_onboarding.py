"""Tests for the onboarding issues administrators see on users and group members (#258, #311).

Four people cover the sign-in states: provisioned over SCIM and never signed in, signed in
but with no vaulted offline token, signed in with a token Keycloak has since refused, and
fully onboarded. A fifth, also fully onboarded, carries one subscription per delivery and
access problem. Every listing that shows people to an administrator must report the same
issues for each, and the Users page's filters, sort and summary are derived from those same
issues in SQL.
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from console_backend.models.scheduled_job import PauseCode
from console_backend.models.user import (
    IssueSeverity,
    OnboardingIssue,
    OnboardingIssueJob,
    OnboardingIssueKind,
    UserOnboarding,
    UserSort,
    placeholder_sub,
)
from console_backend.repositories.user_group_repository import UserGroupRepository
from console_backend.services.user_group_service import UserGroupService
from console_backend.services.user_service import UserService
from sqlalchemy import text

GROUP_ID = 1
SLACK = "slack-client"
GCHAT = "google-chat-client"

K = OnboardingIssueKind
S = IssueSeverity


def _issue(kind, severity, jobs, **extra) -> OnboardingIssue:
    return OnboardingIssue(
        kind=kind, severity=severity, jobs=[OnboardingIssueJob(id=i, name=n) for i, n in jobs], **extra
    )


@pytest_asyncio.fixture
async def people(pg_session):
    """Returns the ids of the seeded jobs by name."""
    db = pg_session
    await db.execute(
        text("""
            INSERT INTO user_groups (id, name, description, created_at, updated_at)
            VALUES (:id, 'Sales', 'Test group', NOW(), NOW())
        """),
        {"id": GROUP_ID},
    )
    for user_id, sub in [
        ("scim-user", placeholder_sub("scim-user")),
        ("chat-only-user", "idp-sub-chat"),
        ("expired-user", "idp-sub-expired"),
        ("ready-user", "idp-sub-ready"),
        ("stuck-user", "idp-sub-stuck"),
    ]:
        await db.execute(
            text("""
                INSERT INTO users (id, sub, email, first_name, last_name, role, status, created_at, updated_at)
                VALUES (:id, :sub, :email, 'Test', :id, 'member', 'active', NOW(), NOW())
            """),
            {"id": user_id, "sub": sub, "email": f"{user_id}@example.com"},
        )
        await db.execute(
            text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, :u, 'read')"),
            {"g": GROUP_ID, "u": user_id},
        )
    for user_id in ("ready-user", "stuck-user"):
        await db.execute(
            text("INSERT INTO user_offline_tokens (user_id, encrypted_token) VALUES (:u, '\\x00')"), {"u": user_id}
        )
    # Marked the way SchedulerTokenService.mark_expired marks a token Keycloak refused.
    await db.execute(
        text(
            "INSERT INTO user_offline_tokens (user_id, encrypted_token, expired_at) "
            "VALUES ('expired-user', '\\x00', NOW())"
        ),
    )

    for client_id, name in ((SLACK, "Slack"), (GCHAT, "Google Chat")):
        await db.execute(
            text("INSERT INTO broker_clients (client_id, name, created_by) VALUES (:c, :n, 'ready-user')"),
            {"c": client_id, "n": name},
        )
    channels = {}
    for key, client_id, workspace_id, name in [
        ("A1", SLACK, "T1", "Slack Nannos (T1)"),
        ("B1", SLACK, "T2", "Slack Nannos (T2)"),
        ("P1", GCHAT, None, "Google Chat"),
    ]:
        channels[key] = (
            await db.execute(
                text("""
                    INSERT INTO delivery_channels
                        (name, webhook_url, secret, client_id, registered_by, installation_id, workspace_id)
                    VALUES (:name, 'https://client.test/cb', 's', :c, 'sa', :key, :w)
                    RETURNING id
                """),
                {"name": name, "c": client_id, "key": key, "w": workspace_id},
            )
        ).scalar_one()
    # stuck-user signed in to Slack in T1 only.
    await db.execute(
        text("""
            INSERT INTO broker_bindings (client_id, account_key, user_id, workspace_id, secret_hash)
            VALUES (:c, 'T1:stuck', 'stuck-user', 'T1', 'h')
        """),
        {"c": SLACK},
    )
    agent_id = (
        await db.execute(
            text("INSERT INTO sub_agents (name, owner_user_id, type) VALUES ('Forecaster', 'ready-user', 'automated') RETURNING id")
        )
    ).scalar_one()

    jobs: dict[str, int] = {}

    async def subscribe(user_id, job, *, enabled=True, code=None, channel=None, agent=None, by_group=False):
        if job not in jobs:
            jobs[job] = (
                await db.execute(
                    text("""
                        INSERT INTO scheduled_job_definitions
                            (owner_user_id, name, job_type, prompt, sub_agent_id, schedule_kind, cron_expr, trigger_policy)
                        VALUES ('ready-user', :name, 'task', 'p', :agent, 'cron', '0 8 * * *', 'overridable')
                        RETURNING id
                    """),
                    {"name": job, "agent": agent or agent_id},
                )
            ).scalar_one()
        await db.execute(
            text("""
                INSERT INTO scheduled_job_subscriptions
                    (definition_id, user_id, next_run_at, enabled, pause_code, delivery_channel_id,
                     activated_by, activated_by_groups)
                VALUES (:d, :u, NOW(), :enabled, :code, :channel, :source, CAST(:groups AS jsonb))
            """),
            {
                "d": jobs[job],
                "u": user_id,
                "enabled": enabled,
                "code": code,
                "channel": channel,
                "source": "group" if by_group else "user",
                "groups": f"[{GROUP_ID}]" if by_group else None,
            },
        )

    # The group's default jobs: what the group page, scoped to the group, may show.
    await subscribe("scim-user", "Morning brief", enabled=False, code=PauseCode.AWAITING_SIGN_IN.value, by_group=True)
    await subscribe("expired-user", "Pipeline digest", enabled=False, code=PauseCode.SIGN_IN_EXPIRED.value)
    # Switched on, but runs under no live token: counted under the sign-in issue.
    await subscribe("expired-user", "Legacy watch", enabled=True, channel=channels["B1"])

    await subscribe("stuck-user", "Reaches them", channel=channels["A1"])
    await subscribe("stuck-user", "Other team 1", channel=channels["B1"], by_group=True)
    await subscribe("stuck-user", "Other team 2", enabled=False, code=PauseCode.UNREACHABLE.value, channel=channels["B1"])
    await subscribe("stuck-user", "No recipient", enabled=False, code=PauseCode.UNDELIVERED.value, channel=channels["A1"])
    await subscribe("stuck-user", "Chat update", channel=channels["P1"])
    await subscribe("stuck-user", "Lost agent", enabled=False, code=PauseCode.AGENT_INACCESSIBLE.value)
    await subscribe("stuck-user", "Unshared", enabled=False, code=PauseCode.ACCESS_REVOKED.value)
    # Stops the user chose are not onboarding matters.
    await subscribe("stuck-user", "Paused by them", enabled=False, code=PauseCode.MANUALLY_PAUSED.value)
    # A suspended definition runs for nobody, so its hold is not an onboarding matter.
    await subscribe("stuck-user", "Suspended", enabled=False, code=PauseCode.UNREACHABLE.value, channel=channels["B1"])
    await db.execute(
        text("UPDATE scheduled_job_definitions SET suspended_at = NOW() WHERE id = :d"), {"d": jobs["Suspended"]}
    )
    # ready-user's only job is on a channel Nannos can't judge: info, never a severity.
    await subscribe("ready-user", "Chat update", channel=channels["P1"])
    await db.commit()
    return {"jobs": jobs, "agent_id": agent_id}


def expected(people) -> dict[str, UserOnboarding]:
    j = people["jobs"]
    return {
        "scim-user": UserOnboarding(
            severity=S.BLOCKING,
            issues=[_issue(K.NOT_SIGNED_IN, S.BLOCKING, [(j["Morning brief"], "Morning brief")])],
        ),
        "chat-only-user": UserOnboarding(severity=S.PENDING, issues=[_issue(K.SCHEDULER_NOT_READY, S.PENDING, [])]),
        "expired-user": UserOnboarding(
            severity=S.BLOCKING,
            issues=[
                _issue(
                    K.SIGN_IN_EXPIRED,
                    S.BLOCKING,
                    [(j["Legacy watch"], "Legacy watch"), (j["Pipeline digest"], "Pipeline digest")],
                )
            ],
        ),
        "ready-user": UserOnboarding(
            severity=None,
            issues=[
                _issue(
                    K.UNKNOWN_REACHABILITY,
                    S.INFO,
                    [(j["Chat update"], "Chat update")],
                    client_id=GCHAT,
                    client_name="Google Chat",
                    channel_names=["Google Chat"],
                )
            ],
        ),
        "stuck-user": UserOnboarding(
            severity=S.BLOCKING,
            issues=[
                # Worst first, then most jobs, then by kind.
                _issue(
                    K.UNREACHABLE,
                    S.BLOCKING,
                    [(j["Other team 1"], "Other team 1"), (j["Other team 2"], "Other team 2")],
                    client_id=SLACK,
                    client_name="Slack",
                    workspace_id="T2",
                    channel_names=["Slack Nannos (T2)"],
                ),
                _issue(K.ACCESS_REVOKED, S.BLOCKING, [(j["Unshared"], "Unshared")]),
                _issue(
                    K.AGENT_INACCESSIBLE,
                    S.BLOCKING,
                    [(j["Lost agent"], "Lost agent")],
                    agent_id=people["agent_id"],
                    agent_name="Forecaster",
                ),
                _issue(
                    K.UNDELIVERED,
                    S.BLOCKING,
                    [(j["No recipient"], "No recipient")],
                    client_id=SLACK,
                    client_name="Slack",
                    workspace_id="T1",
                    channel_names=["Slack Nannos (T1)"],
                ),
                _issue(
                    K.UNKNOWN_REACHABILITY,
                    S.INFO,
                    [(j["Chat update"], "Chat update")],
                    client_id=GCHAT,
                    client_name="Google Chat",
                    channel_names=["Google Chat"],
                ),
            ],
        ),
    }


def expected_in_group(people) -> dict[str, UserOnboarding]:
    """The same people as the group page sees them: only the group's default jobs count,
    and a job the member runs outside the group is never named."""
    j = people["jobs"]
    return {
        "scim-user": expected(people)["scim-user"],
        "chat-only-user": expected(people)["chat-only-user"],
        # Their sign-in is theirs, so it shows; the held jobs are not the group's.
        "expired-user": UserOnboarding(severity=S.PENDING, issues=[_issue(K.SIGN_IN_EXPIRED, S.PENDING, [])]),
        "ready-user": UserOnboarding(severity=None, issues=[]),
        "stuck-user": UserOnboarding(
            severity=S.BLOCKING,
            issues=[
                _issue(
                    K.UNREACHABLE,
                    S.BLOCKING,
                    [(j["Other team 1"], "Other team 1")],
                    client_id=SLACK,
                    client_name="Slack",
                    workspace_id="T2",
                    channel_names=["Slack Nannos (T2)"],
                )
            ],
        ),
    }


@pytest.fixture
def group_service():
    repo = UserGroupRepository()
    repo.set_audit_service(AsyncMock())
    return UserGroupService(
        user_group_repository=repo,
        keycloak_admin_service=AsyncMock(),
        notification_service=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_list_users_reports_onboarding(pg_session, people):
    users, _ = await UserService().list_users(pg_session, group_id=GROUP_ID)

    assert {u.id: u.onboarding for u in users} == expected(people)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_id", ["scim-user", "chat-only-user", "expired-user", "ready-user", "stuck-user"]
)
async def test_user_detail_reports_onboarding(pg_session, people, user_id):
    user = await UserService().get_user_with_groups(pg_session, user_id)

    assert user is not None
    assert user.onboarding == expected(people)[user_id]


@pytest.mark.asyncio
async def test_group_members_report_onboarding(pg_session, people, group_service):
    members, total = await group_service.list_members(pg_session, GROUP_ID)

    assert total == 5
    assert {m.user_id: m.onboarding for m in members} == expected_in_group(people)


@pytest.mark.asyncio
async def test_group_detail_members_report_onboarding(pg_session, people, group_service):
    group = await group_service.get_group_with_members(pg_session, GROUP_ID)

    assert group is not None
    assert {m.user_id: m.onboarding for m in group.members} == expected_in_group(people)


@pytest.mark.asyncio
async def test_first_sign_in_clears_the_issue(pg_session, people, group_service):
    """Vaulting a token is what a sign-in does; the next listing must reflect it."""
    await pg_session.execute(
        text("INSERT INTO user_offline_tokens (user_id, encrypted_token) VALUES ('chat-only-user', '\\x00')"),
    )
    await pg_session.commit()

    members, _ = await group_service.list_members(pg_session, GROUP_ID)

    assert {m.user_id: m.onboarding for m in members}["chat-only-user"] == UserOnboarding(severity=None, issues=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "matched"),
    [
        ({"severity": [S.BLOCKING]}, {"scim-user", "expired-user", "stuck-user"}),
        ({"severity": [S.BLOCKING, S.PENDING]}, {"scim-user", "expired-user", "stuck-user", "chat-only-user"}),
        ({"severity": [S.PENDING]}, {"chat-only-user"}),
        # Info never counts towards a user's severity, so it matches nobody.
        ({"severity": [S.INFO]}, set()),
        ({"issue": [K.UNKNOWN_REACHABILITY]}, {"ready-user", "stuck-user"}),
        ({"issue": [K.NOT_SIGNED_IN, K.SIGN_IN_EXPIRED]}, {"scim-user", "expired-user"}),
        ({"client_id": SLACK}, {"stuck-user"}),
        # Kind and client have to hold for the same issue: stuck-user's Google Chat issue is
        # unknown, not unreachable.
        ({"issue": [K.UNREACHABLE], "client_id": GCHAT}, set()),
        ({"issue": [K.UNKNOWN_REACHABILITY], "client_id": GCHAT}, {"ready-user", "stuck-user"}),
        ({"issue": [K.UNREACHABLE], "severity": [S.PENDING]}, set()),
    ],
)
async def test_list_users_filters_on_onboarding(pg_session, people, filters, matched):
    users, total = await UserService().list_users(pg_session, group_id=GROUP_ID, **filters)

    assert {u.id for u in users} == matched
    assert total == len(matched), "the count applies the same filters"


@pytest.mark.asyncio
async def test_list_users_sorts_worst_first(pg_session, people):
    users, _ = await UserService().list_users(pg_session, group_id=GROUP_ID, sort=UserSort.SEVERITY)

    order = [u.id for u in users]
    # Blocking by jobs stopped (stuck 5, expired 2, scim 1), then pending, then nothing.
    assert order == ["stuck-user", "expired-user", "scim-user", "chat-only-user", "ready-user"]


@pytest.mark.asyncio
async def test_list_users_pages_the_filtered_set(pg_session, people):
    page_1, total = await UserService().list_users(
        pg_session, group_id=GROUP_ID, severity=[S.BLOCKING], sort=UserSort.SEVERITY, limit=2
    )
    page_2, _ = await UserService().list_users(
        pg_session, group_id=GROUP_ID, severity=[S.BLOCKING], sort=UserSort.SEVERITY, limit=2, page=2
    )

    assert total == 3
    assert [u.id for u in page_1] == ["stuck-user", "expired-user"]
    assert [u.id for u in page_2] == ["scim-user"]


@pytest.mark.asyncio
async def test_onboarding_summary_counts_users(pg_session, people):
    summary = await UserService().onboarding_summary(pg_session, group_id=GROUP_ID)

    assert (summary.blocking, summary.pending) == (3, 1)
    counts = {(e.kind, e.client_id): e.users for e in summary.issues}
    assert counts == {
        (K.UNKNOWN_REACHABILITY, GCHAT): 2,
        (K.NOT_SIGNED_IN, None): 1,
        (K.SCHEDULER_NOT_READY, None): 1,
        (K.SIGN_IN_EXPIRED, None): 1,
        (K.UNREACHABLE, SLACK): 1,
        (K.UNDELIVERED, SLACK): 1,
        (K.AGENT_INACCESSIBLE, None): 1,
        (K.ACCESS_REVOKED, None): 1,
    }
    assert summary.issues[0].kind == K.UNKNOWN_REACHABILITY, "most users first"
    assert next(e for e in summary.issues if e.client_id == SLACK).client_name == "Slack"


@pytest.mark.asyncio
async def test_onboarding_summary_follows_the_list_filters(pg_session, people):
    summary = await UserService().onboarding_summary(pg_session, group_id=GROUP_ID, search="scim")

    assert (summary.blocking, summary.pending) == (1, 0)
    assert [(e.kind, e.users) for e in summary.issues] == [(K.NOT_SIGNED_IN, 1)]


@pytest.mark.asyncio
async def test_a_member_of_two_groups_is_scoped_to_each(pg_session, people, group_service):
    """The same subscription belongs to the group that activated it, not to every group of
    the member."""
    await pg_session.execute(
        text("INSERT INTO user_groups (id, name, description, created_at, updated_at) VALUES (2, 'Ops', '', NOW(), NOW())")
    )
    await pg_session.execute(
        text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (2, 'stuck-user', 'read')")
    )
    await pg_session.commit()

    [in_ops], _ = await group_service.list_members(pg_session, 2)

    assert in_ops.onboarding == UserOnboarding(severity=None, issues=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "matched"),
    [
        ({"severity": [S.BLOCKING]}, {"scim-user", "stuck-user"}),
        ({"severity": [S.PENDING]}, {"chat-only-user", "expired-user"}),
        ({"issue": [K.UNREACHABLE], "client_id": SLACK}, {"stuck-user"}),
        # Outside the group's scope: stuck-user's undelivered job is their own.
        ({"issue": [K.UNDELIVERED]}, set()),
        ({"issue": [K.UNKNOWN_REACHABILITY]}, set()),
    ],
)
async def test_group_members_filter_within_the_group(pg_session, people, group_service, filters, matched):
    members, total = await group_service.list_members(pg_session, GROUP_ID, **filters)

    assert {m.user_id for m in members} == matched
    assert total == len(matched)


@pytest.mark.asyncio
async def test_group_members_sort_worst_first(pg_session, people, group_service):
    members, _ = await group_service.list_members(pg_session, GROUP_ID, sort=UserSort.SEVERITY)

    # Blocking by jobs stopped (both 1: then by name), pending by name, then nothing.
    assert [m.user_id for m in members] == [
        "scim-user", "stuck-user", "chat-only-user", "expired-user", "ready-user"
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_group_summary_counts_within_the_group(pg_session, people, group_service):
    summary = await group_service.members_onboarding_summary(pg_session, GROUP_ID)

    assert (summary.blocking, summary.pending) == (2, 2)
    assert {(e.kind, e.client_id): e.users for e in summary.issues} == {
        (K.NOT_SIGNED_IN, None): 1,
        (K.SCHEDULER_NOT_READY, None): 1,
        (K.SIGN_IN_EXPIRED, None): 1,
        (K.UNREACHABLE, SLACK): 1,
    }


@pytest.mark.asyncio
async def test_service_account_has_no_onboarding(pg_session, people, group_service):
    """A machine identity never signs in interactively, so every listing reports none,
    whether it is reached through the user pages or as a group member."""
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, role, status, is_service_account,
                               created_at, updated_at)
            VALUES ('svc-user', 'service-account-client', 'svc@example.com', 'Svc', 'Account', 'member',
                    'active', true, NOW(), NOW())
        """),
    )
    await pg_session.execute(
        text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, 'svc-user', 'read')"),
        {"g": GROUP_ID},
    )
    await pg_session.commit()

    users, _ = await UserService().list_users(pg_session, group_id=GROUP_ID)
    detail = await UserService().get_user_with_groups(pg_session, "svc-user")
    members, _ = await group_service.list_members(pg_session, GROUP_ID)
    group = await group_service.get_group_with_members(pg_session, GROUP_ID)
    attention, _ = await UserService().list_users(pg_session, group_id=GROUP_ID, severity=[S.BLOCKING, S.PENDING])

    assert {u.id: u.onboarding for u in users}["svc-user"] is None
    assert detail is not None and detail.onboarding is None
    assert {m.user_id: m.onboarding for m in members}["svc-user"] is None
    assert group is not None and {m.user_id: m.onboarding for m in group.members}["svc-user"] is None
    assert "svc-user" not in {u.id for u in attention}
