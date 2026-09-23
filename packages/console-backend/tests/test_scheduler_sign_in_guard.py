"""A subscription runs under its subscriber's vaulted offline token (ADR-0010), and only a
sign-in stores one. These tests pin what happens for a user who has none yet:

- what they start themselves (create, subscribe, copy, resume, switch on) is refused
  with the sign-in link, before anything is written;
- what a group default starts for them is created switched off, with a reason and a
  console notification carrying the link, and no chat notice they could not receive;
- their first sign-in switches those held subscriptions on.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from console_backend.config import config
from console_backend.models.scheduled_job import ScheduledJobUpdate, ScheduleKind
from console_backend.models.sub_agent import SubAgentType
from console_backend.models.user import UserSettings
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.notification_service import NotificationService
from console_backend.services.scheduler_service import (
    _AWAITING_SIGN_IN_REASON,
    _ELAPSED_ONCE_ON_INHERIT,
    SchedulerNotReadyError,
    SchedulerService,
)
from tests.test_scheduler_sharing import TZ, _seed_group, _seed_users, _task_create, _watch_create


@pytest_asyncio.fixture
async def world(pg_session):
    """The sharing tests' world, plus a token service whose "who has a vaulted token"
    answer is the mutable ``ready`` set — a test signs someone in by adding to it."""
    users = await _seed_users(pg_session)
    gid = await _seed_group(pg_session, "team", {users["member"].id: "read", users["writer"].id: "write"})
    await pg_session.commit()

    repo = ScheduledJobRepository()
    repo.set_audit_service(AuditService())
    settings = AsyncMock()
    settings.get_settings.side_effect = lambda db, uid: UserSettings(user_id=uid, timezone=TZ[uid.split("-", 1)[1]])
    channels = AsyncMock()
    channels.get_channel_by_id.return_value = object()
    sub_agents = AsyncMock()
    sub_agents.get_sub_agent_by_id.return_value = MagicMock(type=SubAgentType.AUTOMATED, name="inline-auto")

    ready = {users["owner"].id, users["writer"].id}
    tokens = MagicMock()
    tokens.has_consent = AsyncMock(side_effect=lambda db, uid: uid in ready)
    tokens.users_with_consent = AsyncMock(side_effect=lambda db, ids: {i for i in ids if i in ready})

    service = SchedulerService(repository=repo, sub_agent_service=sub_agents)
    service.set_user_settings_service(settings)
    service.set_delivery_channel_repository(channels)
    service.set_notification_service(NotificationService())
    service.set_token_service(tokens)
    return {
        "db": pg_session,
        "users": users,
        "group": gid,
        "service": service,
        "repo": repo,
        "ready": ready,
        "sub_agents": sub_agents,
    }


async def _shared(world):
    """A definition owned by the owner and shared (read) with the team."""
    svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
    job = await svc.create_job(db, _watch_create(), u["owner"])
    await svc.update_permissions(db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"])
    await db.commit()
    return job


async def _count(db, table: str) -> int:
    return (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


class TestWhatTheUserStartsIsRefusedWithTheSignInLink:
    @pytest.mark.asyncio
    async def test_create_is_refused_before_anything_is_written(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        inline = _task_create(
            sub_agent_parameters={
                "name": "inline",
                "description": "x",
                "model": "claude-sonnet-4.5",
                "system_prompt": "y",
            },
        )

        with pytest.raises(SchedulerNotReadyError) as exc:
            await svc.create_job(db, inline, u["member"])

        assert config.console_sign_in_url in str(exc.value)
        assert await _count(db, "scheduled_job_definitions") == 0
        world["sub_agents"].create_sub_agent.assert_not_awaited()  # no orphaned inline agent

    @pytest.mark.asyncio
    async def test_a_ready_user_creates_as_before(self, world):
        job = await world["service"].create_job(world["db"], _watch_create(), world["users"]["owner"])
        assert job.enabled is True

    @pytest.mark.asyncio
    async def test_subscribe_and_copy_are_refused(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared(world)

        with pytest.raises(SchedulerNotReadyError):
            await svc.subscribe(db, job.definition_id, u["member"])
        with pytest.raises(SchedulerNotReadyError):
            await svc.copy_definition(db, job.definition_id, u["member"])
        assert await svc.repo.get_subscription_for(db, job.definition_id, u["member"].id) is None

    @pytest.mark.asyncio
    async def test_an_existing_subscription_is_returned_without_asking(self, world):
        """Subscribe is idempotent; the check guards creation, not a no-op."""
        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared(world)
        world["ready"].add(u["member"].id)
        mine = await svc.subscribe(db, job.definition_id, u["member"])
        world["ready"].discard(u["member"].id)

        assert (await svc.subscribe(db, job.definition_id, u["member"])).id == mine.id

    @pytest.mark.asyncio
    async def test_switching_a_job_on_is_refused_but_editing_an_enabled_one_is_not(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(), u["owner"])
        await svc.pause_job(db, job.id, u["owner"])
        world["ready"].discard(u["owner"].id)  # e.g. their vaulted token was revoked

        with pytest.raises(SchedulerNotReadyError):
            await svc.resume_job(db, job.id, u["owner"])
        with pytest.raises(SchedulerNotReadyError):
            await svc.update_job(db, job.id, ScheduledJobUpdate(enabled=True), u["owner"])

        # The console resends `enabled` with every save; on an enabled job it is no switch.
        world["ready"].add(u["owner"].id)
        await svc.resume_job(db, job.id, u["owner"])
        world["ready"].discard(u["owner"].id)
        # Definition fields travel as keyword arguments, as the router passes them.
        renamed = await svc.update_job(db, job.id, ScheduledJobUpdate(enabled=True), u["owner"], name="Renamed")
        assert renamed is not None and renamed.name == "Renamed" and renamed.enabled is True


class TestAGroupDefaultHoldsBackMembersWhoHaveNotSignedIn:
    @pytest.mark.asyncio
    async def test_held_off_with_a_reason_and_told_how_to_start_it(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        sent: list[str] = []
        svc.set_notice_sender(AsyncMock(side_effect=lambda job, body, **kw: sent.append(job.user_id) or True))
        job = await _shared(world)

        await svc.add_group_default_job(db, gid, job.definition_id, u["owner"])

        member = await svc.repo.get_subscription_for(db, job.definition_id, u["member"].id)
        writer = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)
        assert (member.enabled, member.paused_reason) == (False, _AWAITING_SIGN_IN_REASON)
        assert writer.enabled is True and writer.paused_reason is None

        messages = dict(
            (
                await db.execute(
                    text("SELECT user_id, message FROM user_notifications WHERE type = 'job_subscription_activated'")
                )
            ).all()
        )
        assert config.console_sign_in_url in messages[u["member"].id]
        assert config.console_sign_in_url not in messages[u["writer"].id]
        # The chat notice is sent under the subscriber's own token: only the ready member gets one.
        assert sent == [u["writer"].id]

    @pytest.mark.asyncio
    async def test_a_returning_member_who_is_not_ready_stays_held(self, world):
        """A kept row that access withdrawal stopped is switched back on by the group
        default — unless its subscriber still cannot run it."""
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        job = await _shared(world)
        world["ready"].add(u["member"].id)
        await svc.subscribe(db, job.definition_id, u["member"])
        await svc.set_group_default_jobs(db, gid, [job.definition_id], u["owner"])
        await db.commit()
        await db.execute(
            text("DELETE FROM user_group_members WHERE user_group_id = :g AND user_id = :u"),
            {"g": gid, "u": u["member"].id},
        )
        await svc.on_members_removed(db, u["owner"], gid, [u["member"].id])
        await db.commit()
        world["ready"].discard(u["member"].id)

        await db.execute(
            text("INSERT INTO user_group_members (user_group_id, user_id, group_role) VALUES (:g, :u, 'read')"),
            {"g": gid, "u": u["member"].id},
        )
        await svc.on_members_added(db, u["owner"], gid, [u["member"].id])
        await db.commit()

        back = await svc.repo.get_subscription_for(db, job.definition_id, u["member"].id)
        assert (back.enabled, back.paused_reason) == (False, _AWAITING_SIGN_IN_REASON)


class TestTheFirstSignInReleasesTheHold:
    @pytest.mark.asyncio
    async def test_held_subscriptions_start_and_others_are_left_alone(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        job = await _shared(world)
        await svc.add_group_default_job(db, gid, job.definition_id, u["owner"])
        await db.commit()
        # A job the member switched off themselves must stay off.
        own = await _shared(world)
        world["ready"].add(u["member"].id)
        mine = await svc.subscribe(db, own.definition_id, u["member"])
        await svc.pause_job(db, mine.id, u["member"])

        released = await svc.release_sign_in_holds(db, u["member"])

        assert released == 1
        held = await svc.repo.get_subscription_for(db, job.definition_id, u["member"].id)
        assert held.enabled is True and held.paused_reason is None
        assert held.next_run_at > datetime.now(timezone.utc)
        paused = await svc.repo.get_job(db, mine.id)
        assert paused.enabled is False and paused.paused_reason == "Manually paused"
        assert await svc.release_sign_in_holds(db, u["member"]) == 0  # idempotent

    @pytest.mark.asyncio
    async def test_a_one_shot_that_elapsed_while_it_waited_stays_off_and_says_why(self, world):
        svc, db, u, gid = world["service"], world["db"], world["users"], world["group"]
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        job = await svc.create_job(
            db,
            _watch_create(schedule_kind=ScheduleKind.ONCE, cron_expr=None, run_at=soon),
            u["owner"],
        )
        await svc.update_permissions(db, job.definition_id, [{"user_group_id": gid, "permissions": ["read"]}], u["owner"])
        await svc.add_group_default_job(db, gid, job.definition_id, u["owner"])
        await db.commit()
        # Its moment passes before the member ever signs in.
        await db.execute(
            text("UPDATE scheduled_job_definitions SET run_at = :past WHERE id = :d"),
            {"past": datetime.now(timezone.utc) - timedelta(minutes=5), "d": job.definition_id},
        )
        world["ready"].add(u["member"].id)

        assert await svc.release_sign_in_holds(db, u["member"]) == 0

        held = await svc.repo.get_subscription_for(db, job.definition_id, u["member"].id)
        assert (held.enabled, held.paused_reason) == (False, _ELAPSED_ONCE_ON_INHERIT)
