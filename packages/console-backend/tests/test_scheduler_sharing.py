"""Shared scheduled jobs (ADR-0010), against a real database.

A job is a DEFINITION plus one SUBSCRIPTION per user who runs it. These tests pin the
contract the split introduces: who may do what to a shared definition, that every
subscription runs in its subscriber's own timezone, that suspension holds every
subscription out of the claim, and that group defaults and group membership move
subscriptions the way they move default agents.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from console_backend.models.notification import NotificationType
from console_backend.models.scheduled_job import (
    JobType,
    ScheduledJobCreate,
    ScheduledJobUpdate,
    ScheduleKind,
    TriggerPolicy,
)
from console_backend.models.sub_agent import SubAgentType
from console_backend.models.user import User, UserRole, UserSettings, UserStatus
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.notification_service import NotificationService
from console_backend.services.scheduler_service import SchedulerAccessError, SchedulerService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TZ = {"owner": "Europe/Zurich", "member": "Asia/Tokyo", "writer": "America/New_York", "outsider": "UTC"}


def _user(name: str) -> User:
    return User(
        id=f"share-{name}",
        sub=f"share-sub-{name}",
        email=f"{name}@share.test",
        first_name=name.title(),
        last_name="T",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )


async def _seed_users(db: AsyncSession) -> dict[str, User]:
    users = {name: _user(name) for name in TZ}
    for name, u in users.items():
        await db.execute(
            text(
                "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) "
                "VALUES (:id, :sub, :email, :fn, :ln, false, 'member', 'active')"
            ),
            {"id": u.id, "sub": u.sub, "email": u.email, "fn": u.first_name, "ln": u.last_name},
        )
        await db.execute(
            text("INSERT INTO user_settings (user_id, timezone) VALUES (:id, :tz)"),
            {"id": u.id, "tz": TZ[name]},
        )
    return users


async def _seed_group(db: AsyncSession, name: str, members: dict[str, str]) -> int:
    """A group with *members* as {user_id: group_role}. Returns the group id."""
    gid = (
        await db.execute(text("INSERT INTO user_groups (name) VALUES (:name) RETURNING id"), {"name": name})
    ).scalar_one()
    for uid, role in members.items():
        await db.execute(
            text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, :u, :r)"),
            {"g": gid, "u": uid, "r": role},
        )
    return gid


def _watch_create(**overrides) -> ScheduledJobCreate:
    fields = dict(
        name="Monday report",
        job_type=JobType.WATCH,
        schedule_kind=ScheduleKind.CRON,
        cron_expr="0 9 * * 1-5",
        check_tool="ping_tool",
        cel_expr="result != null",
        trigger_policy=TriggerPolicy.OVERRIDABLE,
    )
    fields.update(overrides)
    return ScheduledJobCreate(**fields)


@pytest_asyncio.fixture
async def world(pg_session: AsyncSession):
    """Users in four timezones, one group (member: read, writer: write), a wired service."""
    users = await _seed_users(pg_session)
    gid = await _seed_group(pg_session, "team", {users["member"].id: "read", users["writer"].id: "write"})
    await pg_session.commit()

    repo = ScheduledJobRepository()
    repo.set_audit_service(AuditService())
    settings = AsyncMock()
    settings.get_settings.side_effect = lambda db, uid: UserSettings(user_id=uid, timezone=TZ[uid.split("-", 1)[1]])
    channels = AsyncMock()
    channels.get_channel_by_id.return_value = object()
    # Only what sharing an inline automated agent consults: its type.
    sub_agents = AsyncMock()
    sub_agents.get_sub_agent_by_id.return_value = MagicMock(type=SubAgentType.AUTOMATED, name="inline-auto")
    service = SchedulerService(repository=repo, sub_agent_service=sub_agents)
    service.set_user_settings_service(settings)
    service.set_delivery_channel_repository(channels)
    service.set_notification_service(NotificationService())
    return {"db": pg_session, "users": users, "group": gid, "service": service, "repo": repo}


async def _audit_actions(db: AsyncSession, entity_type: str) -> list[str]:
    rows = await db.execute(
        text("SELECT action::text FROM audit_logs WHERE entity_type = CAST(:t AS audit_entity_type) ORDER BY id"),
        {"t": entity_type},
    )
    return [r[0] for r in rows.fetchall()]


async def _notifications(db: AsyncSession, user_id: str) -> list[str]:
    rows = await db.execute(
        text("SELECT type FROM user_notifications WHERE user_id = :u ORDER BY id"), {"u": user_id}
    )
    return [r[0] for r in rows.fetchall()]


class TestCreateIsDefinitionPlusOwnSubscription:
    @pytest.mark.asyncio
    async def test_owner_is_the_first_subscriber_in_their_own_timezone(self, world):
        svc, db, owner = world["service"], world["db"], world["users"]["owner"]

        job = await svc.create_job(db, _watch_create(), owner)

        assert job.user_id == owner.id
        assert job.owner_user_id == owner.id
        assert job.effective_permission == "owner"
        assert job.subscriber_count == 1
        assert job.trigger_inherited is True
        # The definition names no zone; the owner's occurrence resolves to their settings.
        assert job.trigger_defaults is not None and job.trigger_defaults.timezone is None
        assert job.timezone == "Europe/Zurich"
        assert job.next_run_at.astimezone(ZoneInfo("Europe/Zurich")).hour == 9
        definition = await world["repo"].get_definition(db, job.definition_id)
        assert definition is not None and definition["timezone"] is None and definition["revision"] == 1


class TestSharingAndSubscribing:
    @pytest.mark.asyncio
    async def test_outsider_cannot_see_or_subscribe(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])

        assert await svc.list_available_definitions(db, u["outsider"].id) == []
        with pytest.raises(LookupError):
            await svc.subscribe(db, job.definition_id, u["outsider"])

    @pytest.mark.asyncio
    async def test_member_subscribes_and_runs_at_nine_tokyo_time(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
        )

        available = await svc.list_available_definitions(db, u["member"].id)
        assert [d.id for d in available] == [job.definition_id]
        assert available[0].subscription_id is None
        assert available[0].effective_permission == "read"

        mine = await svc.subscribe(db, job.definition_id, u["member"])
        assert mine.id != job.id
        assert mine.definition_id == job.definition_id
        assert mine.user_id == u["member"].id
        assert mine.effective_permission == "read"
        assert mine.trigger_inherited is True
        assert mine.timezone == "Asia/Tokyo"
        assert mine.next_run_at.astimezone(ZoneInfo("Asia/Tokyo")).hour == 9
        assert mine.subscriber_count == 2
        # Idempotent.
        assert (await svc.subscribe(db, job.definition_id, u["member"])).id == mine.id
        assert NotificationType.JOB_SHARED.value in await _notifications(db, u["member"].id)
        # Every write left its audit row: the share on the definition, the subscriptions.
        assert "permission_update" in await _audit_actions(db, "scheduled_job")
        assert await _audit_actions(db, "scheduled_job_subscription") == ["create", "create"]

    @pytest.mark.asyncio
    async def test_write_implies_read(self, world):
        """A grant of ['write'] alone must not list a job that subscribe then refuses."""
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["write"]}], u["owner"]
        )
        perms = await svc.get_permissions(db, job.definition_id, u["owner"])
        assert perms[0]["permissions"] == ["read", "write"]
        listed = await svc.list_available_definitions(db, u["member"].id)
        assert [d.id for d in listed] == [job.definition_id]
        assert (await svc.subscribe(db, job.definition_id, u["member"])).effective_permission == "read"

    @pytest.mark.asyncio
    async def test_a_past_one_shot_arrives_disabled(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(
            db,
            _watch_create(
                schedule_kind=ScheduleKind.ONCE,
                cron_expr=None,
                run_at=datetime.now(timezone.utc) + timedelta(seconds=2),
            ),
            u["owner"],
        )
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
        )
        await db.execute(
            text("UPDATE scheduled_job_definitions SET run_at = NOW() - INTERVAL '1 hour' WHERE id = :id"),
            {"id": job.definition_id},
        )
        await db.commit()

        mine = await svc.subscribe(db, job.definition_id, u["member"])
        assert mine.enabled is False and "already ran" in (mine.paused_reason or "")
        assert await world["repo"].claim_due_jobs(db) == []
        await db.rollback()

    @pytest.mark.asyncio
    async def test_write_permission_follows_grant_and_group_role(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read", "write"]}], u["owner"]
        )
        # The grant says write; the member's group role is read → read. The writer's is write → write.
        assert (await svc.subscribe(db, job.definition_id, u["member"])).effective_permission == "read"
        assert (await svc.subscribe(db, job.definition_id, u["writer"])).effective_permission == "write"


class TestOneUpdatePathRoutesEachField:
    @pytest_asyncio.fixture
    async def shared(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
        )
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        return {**world, "job": job, "mine": mine}

    @pytest.mark.asyncio
    async def test_reader_may_change_own_state_but_not_the_definition(self, shared):
        svc, db, u, mine = shared["service"], shared["db"], shared["users"], shared["mine"]

        with pytest.raises(SchedulerAccessError):
            await svc.update_job(db, mine.id, ScheduledJobUpdate(), u["member"], prompt="mine now")

        updated = await svc.update_job(db, mine.id, ScheduledJobUpdate(enabled=False), u["member"])
        assert updated is not None and updated.enabled is False and updated.paused_reason
        owners = await svc.get_job(db, shared["job"].id, u["owner"].id)
        assert owners is not None and owners.enabled is True

    @pytest.mark.asyncio
    async def test_a_schedule_edit_with_other_subscribers_defaults_to_mine(self, shared):
        svc, db, u, job, mine = shared["service"], shared["db"], shared["users"], shared["job"], shared["mine"]

        # The owner, with another subscriber, no scope: their OWN schedule, an override.
        owners = await svc.update_job(db, job.id, ScheduledJobUpdate(cron_expr="0 8 * * 1-5"), u["owner"])
        assert owners is not None
        assert owners.trigger_inherited is False and owners.cron_expr == "0 8 * * 1-5"
        assert owners.trigger_defaults is not None and owners.trigger_defaults.cron_expr == "0 9 * * 1-5"
        theirs = await svc.get_job(db, mine.id, u["member"].id)
        assert theirs is not None and theirs.cron_expr == "0 9 * * 1-5" and theirs.trigger_inherited

        # scope=everyone: the default moves; the inherited subscriber follows in Tokyo time.
        await svc.update_job(db, job.id, ScheduledJobUpdate(cron_expr="0 10 * * 1-5", scope="everyone"), u["owner"])
        theirs = await svc.get_job(db, mine.id, u["member"].id)
        assert theirs is not None and theirs.cron_expr == "0 10 * * 1-5"
        assert theirs.next_run_at.astimezone(ZoneInfo("Asia/Tokyo")).hour == 10
        definition = await shared["repo"].get_definition(db, job.definition_id)
        assert definition is not None and definition["revision"] == 2  # one definition edit so far

        # A reader cannot move everyone's default.
        with pytest.raises(SchedulerAccessError):
            await svc.update_job(db, mine.id, ScheduledJobUpdate(cron_expr="0 7 * * *", scope="everyone"), u["member"])

        # "Everyone" includes the editor: the owner's own override from above is gone.
        owners = await svc.get_job(db, job.id, u["owner"].id)
        assert owners is not None and owners.trigger_inherited is True and owners.cron_expr == "0 10 * * 1-5"

    @pytest.mark.asyncio
    async def test_an_unchanged_echo_of_the_form_is_not_an_edit(self, shared):
        """The console resends every field prefilled; a reader saving only their own
        delivery must not be refused for 'editing' a name they did not change, and an
        owner's save must not turn into a private override or a revision bump."""
        svc, db, u, job, mine = shared["service"], shared["db"], shared["users"], shared["job"], shared["mine"]

        saved = await svc.update_job(
            db,
            mine.id,
            ScheduledJobUpdate(enabled=False, cron_expr=mine.cron_expr, schedule_kind=mine.schedule_kind, max_failures=3),
            u["member"],
            name=mine.name,
            prompt=mine.prompt,
        )
        assert saved is not None and saved.enabled is False and saved.trigger_inherited is True

        before = await shared["repo"].get_definition(db, job.definition_id)
        saved = await svc.update_job(
            db, job.id, ScheduledJobUpdate(cron_expr=job.cron_expr, schedule_kind=job.schedule_kind), u["owner"], name=job.name
        )
        after = await shared["repo"].get_definition(db, job.definition_id)
        assert saved is not None and saved.trigger_inherited is True
        assert before is not None and after is not None and after["revision"] == before["revision"]

    @pytest.mark.asyncio
    async def test_fixed_binds_the_sole_subscriber_too(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(trigger_policy=TriggerPolicy.FIXED), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
        )
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        # The owner leaves; the reader is now the only subscriber — still bound.
        assert await svc.unsubscribe(db, job.definition_id, u["owner"])
        with pytest.raises(ValueError, match="fixed"):
            await svc.update_job(db, mine.id, ScheduledJobUpdate(interval_seconds=60, schedule_kind=ScheduleKind.INTERVAL), u["member"])
        # …while the owner, unsubscribed, can still delete the definition by its own id.
        await svc.delete_definition(db, job.definition_id, u["owner"])
        assert await svc.get_job(db, mine.id, u["member"].id) is None
        with pytest.raises(LookupError):
            await svc.delete_definition(db, job.definition_id, u["member"])

    @pytest.mark.asyncio
    async def test_fixed_policy_resets_overrides_and_tells_the_subscriber(self, shared):
        svc, db, u, job, mine = shared["service"], shared["db"], shared["users"], shared["job"], shared["mine"]
        await svc.update_job(db, mine.id, ScheduledJobUpdate(cron_expr="0 12 * * *"), u["member"])
        assert (await svc.get_job(db, mine.id, u["member"].id)).trigger_inherited is False  # type: ignore[union-attr]

        await svc.update_job(db, job.id, ScheduledJobUpdate(trigger_policy=TriggerPolicy.FIXED), u["owner"])

        theirs = await svc.get_job(db, mine.id, u["member"].id)
        assert theirs is not None and theirs.trigger_inherited is True and theirs.cron_expr == "0 9 * * 1-5"
        assert NotificationType.JOB_SUBSCRIPTION_RESET.value in await _notifications(db, u["member"].id)
        with pytest.raises(ValueError, match="fixed"):
            await svc.update_job(db, mine.id, ScheduledJobUpdate(cron_expr="0 12 * * *"), u["member"])


class TestSuspendHoldsEverySubscriptionOutOfTheClaim:
    @pytest.mark.asyncio
    async def test_claim_skips_a_suspended_definition_and_resumes_after(self, world):
        svc, db, u, repo = world["service"], world["db"], world["users"], world["repo"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read", "write"]}], u["owner"]
        )
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        await db.execute(
            text("UPDATE scheduled_job_subscriptions SET next_run_at = NOW() - INTERVAL '1 minute' WHERE id = ANY(:ids)"),
            {"ids": [job.id, mine.id]},
        )
        await db.commit()

        claimed = await repo.claim_due_jobs(db)
        await db.rollback()  # release the claim without stamping
        assert {c.job.id for c in claimed} == {job.id, mine.id}
        assert {c.job.user_id for c in claimed} == {u["owner"].id, u["member"].id}

        # The writer (not the owner) may suspend; a reader may not.
        with pytest.raises(SchedulerAccessError):
            await svc.suspend(db, job.definition_id, u["member"], reason="no")
        await svc.suspend(db, job.definition_id, u["writer"], reason="broken upstream")

        assert await repo.claim_due_jobs(db) == []
        await db.rollback()
        owners = await svc.get_job(db, job.id, u["owner"].id)
        assert owners is not None and owners.suspended_at is not None and owners.enabled is True
        assert NotificationType.JOB_SUSPENDED.value in await _notifications(db, u["owner"].id)

        await svc.unsuspend(db, job.definition_id, u["writer"])
        # Nothing fires for the time it was suspended: the occurrence was recomputed forward.
        assert await repo.claim_due_jobs(db) == []
        await db.rollback()
        owners = await svc.get_job(db, job.id, u["owner"].id)
        assert owners is not None and owners.suspended_at is None and owners.next_run_at > datetime.now(timezone.utc)


class TestDeleteAndCopy:
    @pytest.mark.asyncio
    async def test_subscriber_delete_is_unsubscribe_owner_delete_cascades(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
        )
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        theirs = await svc.subscribe(db, job.definition_id, u["writer"])

        assert await svc.delete_job(db, mine.id, u["member"]) is True
        assert await svc.get_job(db, mine.id, u["member"].id) is None
        assert (await svc.get_job(db, job.id, u["owner"].id)).subscriber_count == 2  # type: ignore[union-attr]

        assert await svc.delete_job(db, job.id, u["owner"]) is True
        assert await svc.get_job(db, theirs.id, u["writer"].id) is None
        assert NotificationType.JOB_DELETED.value in await _notifications(db, u["writer"].id)
        assert await svc.list_available_definitions(db, u["member"].id) == []

    @pytest.mark.asyncio
    async def test_copy_is_independent_and_owned_by_the_copier(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
        )

        copy = await svc.copy_definition(db, job.definition_id, u["member"])
        assert copy.definition_id != job.definition_id
        assert copy.owner_user_id == u["member"].id and copy.effective_permission == "owner"
        assert copy.cron_expr == "0 9 * * 1-5" and copy.check_tool == "ping_tool"

        await svc.update_job(db, job.id, ScheduledJobUpdate(), u["owner"], prompt="changed at the source")
        assert (await svc.get_job(db, copy.id, u["member"].id)).prompt != "changed at the source"  # type: ignore[union-attr]


class TestGroupDefaultsFollowMembership:
    @pytest.mark.asyncio
    async def test_default_activates_members_enabled_and_leave_withdraws(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        job = await svc.create_job(db, _watch_create(), u["owner"])

        # A default needs the grant first.
        with pytest.raises(ValueError, match="not shared"):
            await svc.set_group_default_jobs(db, gid, [job.definition_id], u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"]
        )
        await svc.set_group_default_jobs(db, gid, [job.definition_id], u["owner"])
        await db.commit()

        members = {j.user_id: j for j in await svc.repo.list_subscriptions(db, job.definition_id)}
        assert set(members) == {u["owner"].id, u["member"].id, u["writer"].id}
        theirs = members[u["member"].id]
        assert theirs.enabled is True and theirs.activated_by == "group" and theirs.activated_by_groups == [gid]
        assert theirs.next_run_at.astimezone(ZoneInfo("Asia/Tokyo")).hour == 9
        assert NotificationType.JOB_SUBSCRIPTION_ACTIVATED.value in await _notifications(db, u["member"].id)
        listing = await svc.list_group_definitions(db, gid)
        assert [(d["id"], d["is_default"]) for d in listing] == [(job.definition_id, True)]

        # A new member joins: activated too.
        await db.execute(
            text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, :u, 'read')"),
            {"g": gid, "u": u["outsider"].id},
        )
        await svc.on_members_added(db, u["owner"], gid, [u["outsider"].id])
        await db.commit()
        assert await svc.repo.get_subscription_for(db, job.definition_id, u["outsider"].id) is not None

        # The member leaves: the group-default subscription goes with the membership.
        await db.execute(
            text("DELETE FROM user_group_members WHERE user_group_id = :g AND user_id = :u"),
            {"g": gid, "u": u["member"].id},
        )
        await svc.on_members_removed(db, u["owner"], gid, [u["member"].id])
        await db.commit()
        assert await svc.repo.get_subscription_for(db, job.definition_id, u["member"].id) is None
        # Removing the default takes the remaining group-activated subscriptions with it,
        # but never the owner's self-made one.
        await svc.set_group_default_jobs(db, gid, [], u["owner"])
        await db.commit()
        assert {j.user_id for j in await svc.repo.list_subscriptions(db, job.definition_id)} == {u["owner"].id}
        actions = await _audit_actions(db, "scheduled_job")
        assert "assign" in actions and "unassign" in actions
        assert "delete" in await _audit_actions(db, "scheduled_job_subscription")

    @pytest.mark.asyncio
    async def test_a_returning_member_gets_their_stopped_subscription_back(self, world):
        """Self-subscribed, removed (access revoked → disabled), re-added: the group
        default switches the kept row back on and tells them."""
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"])
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        await svc.update_job(db, mine.id, ScheduledJobUpdate(cron_expr="0 12 * * *"), u["member"])
        await svc.set_group_default_jobs(db, gid, [job.definition_id], u["owner"])
        await db.commit()

        await db.execute(
            text("DELETE FROM user_group_members WHERE user_group_id = :g AND user_id = :u"),
            {"g": gid, "u": u["member"].id},
        )
        await svc.on_members_removed(db, u["owner"], gid, [u["member"].id])
        await db.commit()
        theirs = await svc.get_job(db, mine.id, u["member"].id)
        assert theirs is not None and theirs.enabled is False

        await db.execute(
            text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, :u, 'read')"),
            {"g": gid, "u": u["member"].id},
        )
        before = len(await _notifications(db, u["member"].id))
        await svc.on_members_added(db, u["owner"], gid, [u["member"].id])
        await db.commit()
        theirs = await svc.get_job(db, mine.id, u["member"].id)
        assert theirs is not None and theirs.enabled is True and theirs.paused_reason is None
        assert theirs.cron_expr == "0 12 * * *"  # their customisation survived
        assert theirs.activated_by_groups == [gid]
        assert len(await _notifications(db, u["member"].id)) == before + 1

    @pytest.mark.asyncio
    async def test_deleting_the_group_ends_every_grant_through_it(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"])
        await svc.set_group_default_jobs(db, gid, [job.definition_id], u["owner"])
        await db.commit()
        assert len(await svc.repo.list_subscriptions(db, job.definition_id)) == 3

        await svc.on_group_deleted(db, u["owner"], gid)
        await db.execute(text("UPDATE user_groups SET deleted_at = NOW() WHERE id = :g"), {"g": gid})
        await db.commit()

        assert {j.user_id for j in await svc.repo.list_subscriptions(db, job.definition_id)} == {u["owner"].id}
        assert await svc.repo.get_group_default_definition_ids(db, gid) == []
        assert await svc.list_available_definitions(db, u["member"].id) == []
        assert await svc.repo.user_permission(db, job.definition_id, u["member"].id) is None

    @pytest.mark.asyncio
    async def test_revoking_the_grant_disables_a_self_made_subscription(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"])
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        await svc.update_job(db, mine.id, ScheduledJobUpdate(cron_expr="0 12 * * *"), u["member"])

        await svc.update_permissions(db, job.definition_id, [], u["owner"])

        theirs = await svc.get_job(db, mine.id, u["member"].id)
        # Kept, with their customisation, but stopped — re-granting lets them enable it again.
        assert theirs is not None and theirs.enabled is False and "revoked" in (theirs.paused_reason or "")
        assert theirs.cron_expr == "0 12 * * *"
        assert NotificationType.JOB_ACCESS_REVOKED.value in await _notifications(db, u["member"].id)
        assert await svc.repo.claim_due_jobs(db) == []
        await db.rollback()
        # And they can no longer see or re-subscribe.
        assert await svc.list_available_definitions(db, u["member"].id) == []


class TestActivationIsAnnouncedWhereTheResultsLand:
    """A group default switches a job on under someone else's identity. The console
    notification is the durable record; the DM is what reaches a person who never opens
    the console (ADR-0010)."""

    @pytest.mark.asyncio
    async def test_every_activated_member_is_dmd_once_the_subscription_is_durable(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        sent: list[tuple[int, str, bool]] = []

        async def notice_sender(job, text_body, *, what="notice", with_provenance=True):
            # The subscription must be committed by the time this runs: a DM saying a job
            # now runs for you, sent for a row a rollback removed, is the one failure mode
            # worth pinning.
            assert (
                await svc.repo.get_subscription_for(db, job.definition_id, job.user_id)
            ) is not None
            sent.append((job.id, text_body, with_provenance))
            return True

        svc.set_notice_sender(notice_sender)
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"]
        )

        await svc.add_group_default_job(db, gid, job.definition_id, u["owner"])

        # Every member the default created a subscription for, the owner included —
        # the owner already had one, so they are not activated again.
        assert {sid for sid, _, _ in sent} == {
            s.id
            for s in await svc.repo.list_subscriptions(db, job.definition_id)
            if s.user_id in (u["member"].id, u["writer"].id)
        }
        body = sent[0][1]
        assert "default job of one of your groups" in body
        # The text already says why; the delivered-run provenance footer would repeat it.
        assert all(with_provenance is False for _, _, with_provenance in sent)

    @pytest.mark.asyncio
    async def test_a_default_that_activates_nobody_sends_nothing(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        sent: list[int] = []
        svc.set_notice_sender(lambda job, text_body, **kw: sent.append(job.id) or True)
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"]
        )
        await svc.add_group_default_job(db, gid, job.definition_id, u["owner"])
        sent.clear()

        # Already a default: nothing is created, so nobody is told again.
        await svc.add_group_default_job(db, gid, job.definition_id, u["owner"])
        assert sent == []


class TestTheJobViewNamesItsOwner:
    @pytest.mark.asyncio
    async def test_a_subscriber_sees_who_shared_it(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
        )
        mine = await svc.subscribe(db, job.definition_id, u["member"])

        assert mine.owner_email == u["owner"].email
        # Their own job names them: the console reads "is this mine" off the row.
        assert job.owner_email == u["owner"].email and job.owner_user_id == job.user_id


class TestRunsAreTheSubscribers:
    @pytest.mark.asyncio
    async def test_runs_hang_off_the_subscription_and_stay_user_scoped(self, world):
        svc, db, u, repo = world["service"], world["db"], world["users"], world["repo"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"])
        mine = await svc.subscribe(db, job.definition_id, u["member"])

        run_id = await repo.create_run(db, mine.id)
        await db.commit()
        run = await svc.get_run(db, mine.id, run_id, u["member"].id)
        assert run is not None and run.job_id == mine.id
        # The owner cannot reach the member's run through their own job id, nor vice versa.
        assert await svc.get_run(db, job.id, run_id, u["owner"].id) is None
        assert await svc.get_run(db, mine.id, run_id, u["owner"].id) is None
        assert (await svc.list_runs(db, job.id, u["owner"].id)) == []
        assert timedelta(0) <= datetime.now(timezone.utc) - run.started_at < timedelta(minutes=1)


class TestSubscriberAgentAccessQuery:
    @pytest.mark.asyncio
    async def test_inline_automated_agent_travels_with_the_definition(self, world):
        svc, db, u, repo = world["service"], world["db"], world["users"], world["repo"]
        agent_id = (
            await db.execute(
                text(
                    "INSERT INTO sub_agents (name, owner_user_id, type) VALUES ('inline-auto', :o, 'automated') RETURNING id"
                ),
                {"o": u["owner"].id},
            )
        ).scalar_one()
        job = await svc.create_job(
            db, _watch_create(sub_agent_id=None), u["owner"]
        )
        await db.execute(
            text("UPDATE scheduled_job_definitions SET sub_agent_id = :a WHERE id = :d"),
            {"a": agent_id, "d": job.definition_id},
        )
        await svc.update_permissions(db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"])
        await db.commit()

        assert await repo.subscriber_can_run_agent(db, u["owner"].id, agent_id) is True
        assert await repo.subscriber_can_run_agent(db, u["member"].id, agent_id) is False
        await svc.subscribe(db, job.definition_id, u["member"])
        assert await repo.subscriber_can_run_agent(db, u["member"].id, agent_id) is True
        assert await repo.subscriber_can_run_agent(db, u["outsider"].id, agent_id) is False


class TestASubscriberCanGoBackToInheriting:
    """The subscriber's half of "reset to defaults" (ADR-0010). Without it an override is
    a one-way door: the owner's later changes to the default silently stop arriving, and
    only the owner could undo it, for everybody at once."""

    async def _subscribed_with_own_schedule(self, svc, db, u, gid):
        """A job the owner shares, that `member` subscribes to and then overrides."""
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.update_permissions(
            db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"]
        )
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        mine = await svc.update_job(
            db,
            job_id=mine.id,
            data=ScheduledJobUpdate(schedule_kind=ScheduleKind.CRON, cron_expr="30 6 * * *"),
            actor=u["member"],
        )
        assert mine.trigger_inherited is False
        return job, mine

    @pytest.mark.asyncio
    async def test_it_clears_only_the_callers_own_override(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        job, mine = await self._subscribed_with_own_schedule(svc, db, u, gid)
        # The writer diverges too, so we can see they are left alone.
        theirs = await svc.subscribe(db, job.definition_id, u["writer"])
        theirs = await svc.update_job(
            db,
            job_id=theirs.id,
            data=ScheduledJobUpdate(schedule_kind=ScheduleKind.CRON, cron_expr="45 7 * * *"),
            actor=u["writer"],
        )

        back = await svc.follow_default_schedule(db, mine.id, u["member"])

        assert back is not None
        assert back.trigger_inherited is True
        assert back.cron_expr == "0 9 * * 1-5", "the default is what they now run on"
        # Nobody else moved, and the job's own default is untouched.
        assert (await svc.get_job(db, theirs.id, u["writer"].id)).cron_expr == "45 7 * * *"
        definition = await svc.repo.get_definition(db, job.definition_id)
        assert definition["cron_expr"] == "0 9 * * 1-5"
        assert definition["revision"] == 1, "a subscriber's own schedule is not a definition edit"

    @pytest.mark.asyncio
    async def test_the_first_run_is_recomputed_in_the_subscribers_timezone(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        _job, mine = await self._subscribed_with_own_schedule(svc, db, u, gid)

        back = await svc.follow_default_schedule(db, mine.id, u["member"])

        # 09:00 of the definition's cron, read in the member's own zone — not the owner's.
        assert back.next_run_at is not None
        assert back.next_run_at.astimezone(ZoneInfo(TZ["member"])).hour == 9

    @pytest.mark.asyncio
    async def test_it_needs_no_write_permission(self, world):
        """A reader owns their own schedule; giving it up is theirs to do."""
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        _job, mine = await self._subscribed_with_own_schedule(svc, db, u, gid)
        assert mine.effective_permission == "read"

        assert (await svc.follow_default_schedule(db, mine.id, u["member"])).trigger_inherited is True

    @pytest.mark.asyncio
    async def test_it_is_idempotent(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        _job, mine = await self._subscribed_with_own_schedule(svc, db, u, gid)

        first = await svc.follow_default_schedule(db, mine.id, u["member"])
        again = await svc.follow_default_schedule(db, mine.id, u["member"])

        assert again.trigger_inherited is True
        assert again.next_run_at == first.next_run_at, "a second call is not a reschedule"

    @pytest.mark.asyncio
    async def test_somebody_elses_subscription_is_not_found(self, world):
        """Not a 403: the job view is viewer-relative, so another person's subscription
        simply is not one of yours."""
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        _job, mine = await self._subscribed_with_own_schedule(svc, db, u, gid)

        assert await svc.follow_default_schedule(db, mine.id, u["writer"]) is None
        assert (await svc.get_job(db, mine.id, u["member"].id)).trigger_inherited is False

    @pytest.mark.asyncio
    async def test_it_is_audited_as_a_reset(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        _job, mine = await self._subscribed_with_own_schedule(svc, db, u, gid)

        await svc.follow_default_schedule(db, mine.id, u["member"])

        # "Who stopped following the default, and when" must not depend on which door
        # the reset came through.
        assert "reset_overrides" in await _audit_actions(db, "scheduled_job_subscription")
