"""A scheduled job's notification reaches its subscriber only on a channel whose client holds
a sign-in of theirs in that channel's workspace (#192, ADR-0011 amendments 1 and 2). These
tests pin:

- the rule: reachable, unreachable, unknown, derived from broker bindings and each
  channel's workspace;
- what the user starts themselves on an unreachable channel is refused with the way to
  activate it, before anything is written; ``unknown`` is never refused;
- an inherited subscription keeps the owner's channel and is held, visibly, until the
  member signs in from there;
- a client's report that it found no recipient marks the run undelivered and holds the
  subscription (#191);
- the onboarding issues counting subscriptions that cannot reach a user.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from console_backend.models.delivery_channel import UndeliveredReport
from console_backend.models.notification import NotificationType
from console_backend.models.scheduled_job import JobRunStatus, PauseCode, ScheduledJobUpdate, render_pause
from console_backend.models.sub_agent import SubAgentType
from console_backend.models.user import UserSettings
from console_backend.repositories.delivery_reachability_repository import (
    DeliveryReachabilityRepository,
)
from console_backend.repositories.onboarding_issues import onboarding_column_sql
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from console_backend.services.audit_service import AuditService
from console_backend.services.notification_service import NotificationService
from console_backend.services.scheduler_service import (
    DeliveryUnreachableError,
    SchedulerService,
)
from tests.test_scheduler_sharing import TZ, _seed_group, _seed_users, _watch_create

SLACK = "slack-client"
GCHAT = "google-chat-client"


async def _channel(db, client_id: str, installation_id: str, name: str, workspace_id: str | None) -> int:
    return (
        await db.execute(
            text("""
                INSERT INTO delivery_channels
                    (name, webhook_url, secret, client_id, registered_by, installation_id, workspace_id)
                VALUES (:name, 'https://client.test/cb', 's', :client_id, 'sa', :installation_id, :workspace_id)
                RETURNING id
            """),
            {"name": name, "client_id": client_id, "installation_id": installation_id, "workspace_id": workspace_id},
        )
    ).scalar_one()


async def _bind(db, user_id: str, client_id: str, workspace_id: str) -> None:
    await db.execute(
        text("""
            INSERT INTO broker_bindings (client_id, account_key, user_id, workspace_id, secret_hash)
            VALUES (:c, :k, :u, :w, :h)
        """),
        {"c": client_id, "k": f"{workspace_id}:{user_id}", "u": user_id, "w": workspace_id, "h": f"h-{user_id}-{workspace_id}"},
    )


async def _undelivered_jobs(db, user_id: str) -> int:
    """How many jobs the user's onboarding issues say don't reach them (#311)."""
    onboarding = (
        await db.execute(text(f"SELECT {onboarding_column_sql('u.id')} FROM users u WHERE u.id = :id"), {"id": user_id})
    ).scalar_one()
    return sum(len(i["jobs"]) for i in onboarding["issues"] if i["kind"] in ("unreachable", "undelivered"))


@pytest_asyncio.fixture
async def world(pg_session):
    """Slack workspaces T1 (app A1) and T2 (app B1); a Google Chat channel whose client
    never said which workspace it is in. The owner signed in from T1, the writer from T2, the member only
    the old way (no binding at all)."""
    db = pg_session
    users = await _seed_users(db)
    gid = await _seed_group(db, "team", {users["member"].id: "read", users["writer"].id: "write"})
    for client_id in (SLACK, GCHAT):
        await db.execute(
            text("INSERT INTO broker_clients (client_id, name, created_by) VALUES (:c, :c, :u)"),
            {"c": client_id, "u": users["owner"].id},
        )
    channels = {
        "A1": await _channel(db, SLACK, "A1", "Slack Nannos (T1)", "T1"),
        "B1": await _channel(db, SLACK, "B1", "Slack Nannos (T2)", "T2"),
        "P1": await _channel(db, GCHAT, "projects/p1", "Google Chat", None),
    }
    await _bind(db, users["owner"].id, SLACK, "T1")
    await _bind(db, users["writer"].id, SLACK, "T2")
    await db.commit()

    repo = ScheduledJobRepository()
    repo.set_audit_service(AuditService())
    settings = AsyncMock()
    settings.get_settings.side_effect = lambda db, uid: UserSettings(user_id=uid, timezone=TZ[uid.split("-", 1)[1]])
    channel_repo = AsyncMock()
    channel_repo.get_channel_by_id.return_value = object()
    sub_agents = AsyncMock()
    sub_agents.get_sub_agent_by_id.return_value = MagicMock(type=SubAgentType.AUTOMATED, name="inline-auto")
    ready = {u.id for u in users.values()}
    tokens = MagicMock()
    tokens.has_consent = AsyncMock(side_effect=lambda db, uid: uid in ready)
    tokens.users_with_consent = AsyncMock(side_effect=lambda db, ids: {i for i in ids if i in ready})

    reachability = DeliveryReachabilityRepository()
    service = SchedulerService(repository=repo, sub_agent_service=sub_agents)
    service.set_user_settings_service(settings)
    service.set_delivery_channel_repository(channel_repo)
    service.set_reachability_repository(reachability)
    service.set_notification_service(NotificationService())
    service.set_token_service(tokens)
    return {
        "db": db,
        "users": users,
        "group": gid,
        "service": service,
        "repo": repo,
        "reachability": reachability,
        "channels": channels,
        "ready": ready,
    }


async def _shared_on(world, channel: str):
    """A definition owned by the owner, delivering on *channel*, shared (read) with the team."""
    svc, db, u = world["service"], world["db"], world["users"]
    job = await svc.create_job(db, _watch_create(delivery_channel_id=world["channels"][channel]), u["owner"])
    await svc.update_permissions(
        db, job.definition_id, [{"user_group_id": world["group"], "permissions": ["read"]}], u["owner"]
    )
    await db.commit()
    return job


async def _count(db, table: str) -> int:
    return (await db.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


class TestTheRule:
    @pytest.mark.asyncio
    async def test_a_binding_in_the_installations_workspace_reaches(self, world):
        u, ch = world["users"], world["channels"]
        states = await world["reachability"].for_channel(world["db"], ch["A1"], [u["owner"].id, u["writer"].id])
        assert states == {u["owner"].id: "reachable", u["writer"].id: "unreachable"}

    @pytest.mark.asyncio
    async def test_no_binding_with_the_client_is_unknown_not_unreachable(self, world):
        """An old local sign-in leaves no binding, and looks the same as none."""
        u, ch = world["users"], world["channels"]
        assert await world["reachability"].for_channel(world["db"], ch["A1"], [u["member"].id]) == {
            u["member"].id: "unknown"
        }

    @pytest.mark.asyncio
    async def test_a_channel_with_no_known_workspace_is_unknown(self, world):
        db, u, ch = world["db"], world["users"], world["channels"]
        await _bind(db, u["owner"].id, GCHAT, "p1")  # a binding, but the channel names no workspace

        assert await world["reachability"].for_channel(db, ch["P1"], [u["owner"].id]) == {u["owner"].id: "unknown"}

    @pytest.mark.asyncio
    async def test_a_binding_in_another_workspace_does_not_reach_the_channel(self, world):
        db, u, ch = world["db"], world["users"], world["channels"]
        await _bind(db, u["outsider"].id, SLACK, "T9")

        assert await world["reachability"].for_channel(db, ch["A1"], [u["outsider"].id]) == {
            u["outsider"].id: "unreachable"
        }

    @pytest.mark.asyncio
    async def test_the_channel_list_carries_the_callers_own_answer_in_one_query(self, world):
        from console_backend.repositories.delivery_channel_repository import DeliveryChannelRepository

        u, ch = world["users"], world["channels"]
        channels, _ = await DeliveryChannelRepository().list_all_channels(
            world["db"], reachability_for=u["writer"].id
        )
        by_id = {c.id: c.reachability for c in channels}
        assert by_id == {ch["A1"]: "unreachable", ch["B1"]: "reachable", ch["P1"]: "unknown"}

    @pytest.mark.asyncio
    async def test_the_job_view_carries_the_subscribers_own_answer(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        owners = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        members = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["member"])
        nowhere = await svc.create_job(db, _watch_create(), u["owner"])

        assert owners.delivery_reachability == "reachable"
        assert members.delivery_reachability == "unknown"
        assert nowhere.delivery_reachability is None


class TestWhatTheUserStartsIsRefused:
    @pytest.mark.asyncio
    async def test_create_on_an_unreachable_channel_is_refused_before_anything_is_written(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]

        with pytest.raises(DeliveryUnreachableError) as exc:
            await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["writer"])

        assert "Message Nannos on 'Slack Nannos (T1)' once to activate it" in str(exc.value)
        assert await _count(db, "scheduled_job_definitions") == 0

    @pytest.mark.asyncio
    async def test_unknown_is_never_refused(self, world):
        job = await world["service"].create_job(
            world["db"], _watch_create(delivery_channel_id=world["channels"]["A1"]), world["users"]["member"]
        )
        assert job.enabled is True

    @pytest.mark.asyncio
    async def test_changing_to_an_unreachable_channel_is_refused(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["B1"]), u["writer"])

        with pytest.raises(DeliveryUnreachableError):
            await svc.update_job(db, job.id, ScheduledJobUpdate(), u["writer"], delivery_channel_id=ch["A1"])
        assert (await svc.get_job(db, job.id, u["writer"].id)).delivery_channel_id == ch["B1"]

    @pytest.mark.asyncio
    async def test_a_channel_that_became_unreachable_does_not_block_other_edits(self, world):
        """The console resends the whole form, channel included: only a change is checked."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        # A1 is re-registered in a workspace the owner never signed in to.
        await db.execute(text("UPDATE delivery_channels SET workspace_id = 'T3' WHERE id = :id"), {"id": ch["A1"]})

        updated = await svc.update_job(
            db, job.id, ScheduledJobUpdate(), u["owner"], name="Renamed", delivery_channel_id=ch["A1"]
        )
        assert updated.name == "Renamed"
        assert updated.delivery_reachability == "unreachable"

    @pytest.mark.asyncio
    async def test_subscribe_and_copy_hold_an_owners_channel_the_caller_cannot_receive_on(self, world):
        """They take the owner's channel, which the caller may never be able to sign in
        from (another team): held, not refused, so they still have a way in."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")

        mine = await svc.subscribe(db, job.definition_id, u["writer"])
        assert (mine.enabled, mine.pause_code, mine.delivery_channel_id) == (False, PauseCode.UNREACHABLE, ch["A1"])
        copy = await svc.copy_definition(db, job.definition_id, u["writer"])
        assert (copy.enabled, copy.pause_code) == (False, PauseCode.UNREACHABLE)

        # The member's old sign-in is unknown: they subscribe on the owner's channel as before.
        members = await svc.subscribe(db, job.definition_id, u["member"])
        assert (members.enabled, members.delivery_channel_id) == (True, ch["A1"])
        assert members.delivery_reachability == "unknown"

    @pytest.mark.asyncio
    async def test_switching_on_a_job_whose_channel_cannot_reach_is_refused(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        await svc.pause_job(db, job.id, u["owner"])
        await db.execute(text("DELETE FROM broker_bindings WHERE user_id = :u"), {"u": u["owner"].id})
        await _bind(db, u["owner"].id, SLACK, "T2")

        with pytest.raises(DeliveryUnreachableError):
            await svc.resume_job(db, job.id, u["owner"])
        with pytest.raises(DeliveryUnreachableError):
            await svc.update_job(db, job.id, ScheduledJobUpdate(enabled=True), u["owner"])


class TestAnInheritedSubscriptionIsHeldNotMoved:
    @pytest.mark.asyncio
    async def test_a_group_default_keeps_the_owners_channel_and_holds_an_unreachable_member(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")
        sender = AsyncMock()
        svc.set_notice_sender(sender)

        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])

        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)
        members = await svc.repo.get_subscription_for(db, job.definition_id, u["member"].id)
        assert writers.delivery_channel_id == ch["A1"], "never switched for them"
        assert (writers.enabled, writers.pause_code) == (False, PauseCode.UNREACHABLE)
        assert (members.enabled, members.pause_code) == (True, None), "unknown is not held"
        # The chat notice goes only to the member it can reach.
        assert [c.args[0].user_id for c in sender.await_args_list] == [u["member"].id]
        note = (
            await db.execute(
                text("SELECT message FROM user_notifications WHERE user_id = :u AND type = :t"),
                {"u": u["writer"].id, "t": NotificationType.JOB_SUBSCRIPTION_ACTIVATED.value},
            )
        ).scalar_one()
        assert "can't reach you on its delivery channel ('Slack Nannos (T1)')" in note

    @pytest.mark.asyncio
    async def test_a_sign_in_from_that_workspace_switches_it_on(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])

        # A sign-in elsewhere is not news about this channel.
        assert await svc.release_reachability_holds(db, u["writer"], SLACK, "T2") == 0
        await _bind(db, u["writer"].id, SLACK, "T1")
        assert await svc.release_reachability_holds(db, u["writer"], SLACK, "T1") == 1

        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)
        assert (writers.enabled, writers.paused_reason) == (True, None)

    @pytest.mark.asyncio
    async def test_a_sign_in_that_does_not_reach_the_channel_re_holds_it_for_that(self, world):
        """A member waiting for their first sign-in, who then signs in somewhere else,
        stays off, now for the reason that is true."""
        svc, db, u = world["service"], world["db"], world["users"]
        world["ready"].discard(u["writer"].id)
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])
        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)
        assert writers.pause_code == PauseCode.AWAITING_SIGN_IN

        world["ready"].add(u["writer"].id)
        assert await svc.release_sign_in_holds(db, u["writer"]) == 0

        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)
        assert (writers.enabled, writers.pause_code) == (False, PauseCode.UNREACHABLE)


class TestAClientReportsWhatReachedNobody:
    async def _run(self, world, channel="A1"):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=world["channels"][channel]), u["owner"])
        run_id = await svc.repo.create_run(db, job.id, delivery_channel_id=job.delivery_channel_id)
        await db.commit()
        return job, run_id

    @pytest.mark.asyncio
    async def test_only_the_channels_own_client_and_installation_may_report(self, world):
        svc, db = world["service"], world["db"]
        _, run_id = await self._run(world)

        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="no_recipient")
        assert await svc.report_undelivered(db, GCHAT, report) is False
        assert await svc.report_undelivered(db, SLACK, report.model_copy(update={"installation_id": "B1"})) is False
        assert await svc.report_undelivered(db, SLACK, report.model_copy(update={"run_id": run_id + 999})) is False

    @pytest.mark.asyncio
    async def test_no_recipient_marks_the_run_undelivered_and_holds_the_subscription(self, world, caplog):
        caplog.set_level("INFO", logger="console_backend.services.scheduler_service")
        svc, db, u = world["service"], world["db"], world["users"]
        job, run_id = await self._run(world)

        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="no_recipient")
        assert await svc.report_undelivered(db, SLACK, report) is True
        # The dispatch returns after the push went out, and must not overwrite the report.
        await svc.repo.complete_run(db, run_id, JobRunStatus.SUCCESS, delivered=True)
        await db.commit()

        run = await svc.repo.get_run(db, job.id, run_id)
        assert run.status == JobRunStatus.SUCCESS, "the work itself succeeded"
        assert run.delivered is False
        assert run.delivery_failure == "no_recipient"
        [line] = [r for r in caplog.records if f"Run {run_id} of job {job.id}" in r.message]
        assert line.levelname == "INFO", "the subscriber's to fix, and held and notified: no alert"
        held = await svc.get_job(db, job.id, u["owner"].id)
        assert (held.enabled, held.pause_code) == (False, PauseCode.UNREACHABLE)
        assert held.delivery_channel_id == world["channels"]["A1"]
        assert (
            await db.execute(
                text("SELECT count(*) FROM user_notifications WHERE user_id = :u AND type = :t"),
                {"u": u["owner"].id, "t": NotificationType.SCHEDULED_JOB_PAUSED.value},
            )
        ).scalar_one() == 1
        assert await _count(db, "broker_bindings") == 2, "the binding is left alone"

        # Re-signing in from that workspace switches it back on.
        assert await svc.release_reachability_holds(db, u["owner"], SLACK, "T1") == 1

    @pytest.mark.asyncio
    async def test_a_subscriber_nannos_cannot_judge_is_asked_to_switch_it_back_on(self, world):
        """No binding there (an old local sign-in): no sign-in the backend sees will
        release the hold, so it says so, and switching the job on is not refused."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["member"])
        run_id = await svc.repo.create_run(db, job.id, delivery_channel_id=job.delivery_channel_id)
        await db.commit()

        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="no_recipient")
        assert await svc.report_undelivered(db, SLACK, report) is True

        held = await svc.get_job(db, job.id, u["member"].id)
        assert (held.enabled, held.pause_code) == (False, PauseCode.UNDELIVERED)
        assert await _undelivered_jobs(db, u["member"].id) == 1, "the onboarding badge counts it"
        assert await svc.resume_job(db, job.id, u["member"]) is True

    def test_an_overlong_detail_is_cut_not_refused(self):
        """A 422 over a diagnostic string would drop the report, and the run would stay delivered."""
        report = UndeliveredReport(run_id=1, installation_id="A1", reason="send_failed", detail="x" * 2000)
        assert report.detail == "x" * 500

    @pytest.mark.asyncio
    async def test_a_failed_send_marks_the_run_but_keeps_the_job_running(self, world, caplog):
        svc, db, u = world["service"], world["db"], world["users"]
        job, run_id = await self._run(world)

        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="send_failed", detail="rate limited")
        assert await svc.report_undelivered(db, SLACK, report) is True

        run = await svc.repo.get_run(db, job.id, run_id)
        assert (run.delivered, run.delivery_failure) == (False, "send_failed")
        # What failed in detail is logged with the run's ids, never stored.
        [line] = [r for r in caplog.records if f"Run {run_id} of job {job.id}" in r.message]
        assert "rate limited" in line.message and line.levelname == "ERROR", "a failed send is an operational error"
        assert (await svc.get_job(db, job.id, u["owner"].id)).enabled is True


class TestOnboardingCountsSubscriptionsThatCannotReach:
    @pytest.mark.asyncio
    async def test_unreachable_and_held_subscriptions_count_unknown_ones_do_not(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])

        async def count(user) -> int:
            return await _undelivered_jobs(db, user.id)

        assert await count(u["writer"]) == 1
        assert await count(u["member"]) == 0
        assert await count(u["owner"]) == 0


class TestAStopIsACodeAndItsSentenceIsRendered:
    """Releases, counts and the claim read ``pause_code``; ``paused_reason`` is rendered
    from it on every read and stored nowhere, so its wording can change freely."""

    @pytest.mark.asyncio
    async def test_the_reason_a_person_reads_is_rendered_from_the_code(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])
        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)

        assert writers.paused_reason == render_pause(PauseCode.UNREACHABLE)
        columns = await db.execute(
            text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'scheduled_job_subscriptions' AND column_name IN ('paused_reason', 'hold')
            """)
        )
        assert columns.all() == [], "no stored sentence to drift from the code"

    @pytest.mark.asyncio
    async def test_any_other_stop_replaces_the_hold(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])
        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)

        await svc.pause_job(db, writers.id, u["writer"])
        paused = await svc.repo.get_job(db, writers.id)
        assert (paused.pause_code, paused.paused_reason) == (PauseCode.MANUALLY_PAUSED, "Manually paused")
        await _bind(db, u["writer"].id, SLACK, "T1")
        assert await svc.release_reachability_holds(db, u["writer"], SLACK, "T1") == 0, "no longer releasable"

    @pytest.mark.asyncio
    async def test_switching_on_clears_the_code(self, world):
        svc, db, u = world["service"], world["db"], world["users"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=world["channels"]["A1"]), u["owner"])
        await svc.pause_job(db, job.id, u["owner"])

        await svc.repo.update_subscription(db, u["owner"], job.id, {"enabled": True})

        back = await svc.repo.get_job(db, job.id)
        assert (back.enabled, back.pause_code, back.paused_reason) == (True, None, None)

    @pytest.mark.asyncio
    async def test_a_stopped_subscription_cannot_be_switched_on_with_its_code(self, world):
        """The database backs the rule up: a write that forgets the code fails loudly."""
        from sqlalchemy.exc import IntegrityError

        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])
        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)

        with pytest.raises(IntegrityError):
            await db.execute(
                text("UPDATE scheduled_job_subscriptions SET enabled = TRUE WHERE id = :id"), {"id": writers.id}
            )
        await db.rollback()

    @pytest.mark.asyncio
    async def test_an_override_reset_records_its_own_stop(self, world):
        """Found in review, when the code and the sentence were two columns: a reset wrote
        the elapsed one-shot's sentence past the hold. One column cannot disagree."""
        svc, db, u = world["service"], world["db"], world["users"]
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])
        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)
        elapsed = {"enabled": False, "pause_code": PauseCode.ELAPSED_ON_INHERIT, "retry_at": None}

        await svc.repo.clear_trigger_override(db, u["writer"], writers.id, elapsed)
        assert (await svc.repo.get_job(db, writers.id)).pause_code == PauseCode.ELAPSED_ON_INHERIT


async def _paused_notices(db, user_id: str) -> int:
    return (
        await db.execute(
            text("SELECT count(*) FROM user_notifications WHERE user_id = :u AND type = :t"),
            {"u": user_id, "t": NotificationType.SCHEDULED_JOB_PAUSED.value},
        )
    ).scalar_one()


class TestMovingAHeldJobReleasesIt:
    """The hold's notice says "or change its delivery"; no sign-in releases a job moved to
    a channel the subscriber is already bound on."""

    @pytest.mark.asyncio
    async def test_a_move_to_a_channel_that_reaches_them_switches_it_on(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")
        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])
        writers = await svc.repo.get_subscription_for(db, job.definition_id, u["writer"].id)
        assert writers.pause_code == PauseCode.UNREACHABLE

        # As "Save, keep paused" and the MCP tool send it: no ``enabled``.
        moved = await svc.update_job(db, writers.id, ScheduledJobUpdate(), u["writer"], delivery_channel_id=ch["B1"])

        assert (moved.enabled, moved.pause_code, moved.delivery_channel_id) == (True, None, ch["B1"])

    @pytest.mark.asyncio
    async def test_a_move_where_nannos_cannot_tell_switches_it_on_too(self, world):
        """``unknown`` is never refused; if nothing arrives there, the client's report holds it."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")
        writers = await svc.subscribe(db, job.definition_id, u["writer"])

        moved = await svc.update_job(db, writers.id, ScheduledJobUpdate(), u["writer"], delivery_channel_id=ch["P1"])

        assert (moved.enabled, moved.pause_code) == (True, None)

    @pytest.mark.asyncio
    async def test_an_explicit_enabled_decides_for_itself(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")
        writers = await svc.subscribe(db, job.definition_id, u["writer"])

        moved = await svc.update_job(
            db, writers.id, ScheduledJobUpdate(enabled=False), u["writer"], delivery_channel_id=ch["B1"]
        )

        assert (moved.enabled, moved.pause_code) == (False, PauseCode.DISABLED_BY_USER)

    @pytest.mark.asyncio
    async def test_a_move_that_cannot_switch_it_on_keeps_the_hold_it_has(self, world):
        """A one-shot whose moment passed while it was held: the move stands, the job stays
        off with the reason that is now true (never a false "already ran"), and resuming it
        says why."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")
        writers = await svc.subscribe(db, job.definition_id, u["writer"])
        await db.execute(
            text("""
                UPDATE scheduled_job_subscriptions
                SET schedule_kind = 'once', run_at = NOW() - interval '1 day', next_run_at = NOW() - interval '1 day'
                WHERE id = :id
            """),
            {"id": writers.id},
        )
        await db.commit()

        moved = await svc.update_job(db, writers.id, ScheduledJobUpdate(), u["writer"], delivery_channel_id=ch["B1"])

        assert (moved.enabled, moved.pause_code, moved.delivery_channel_id) == (
            False, PauseCode.ELAPSED_WHILE_HELD, ch["B1"]
        )
        with pytest.raises(ValueError, match="did not run; set a new time"):
            await svc.resume_job(db, writers.id, u["writer"])

    @pytest.mark.asyncio
    async def test_a_move_by_someone_whose_sign_in_lapsed_keeps_the_hold(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")
        writers = await svc.subscribe(db, job.definition_id, u["writer"])
        world["ready"].discard(u["writer"].id)

        moved = await svc.update_job(db, writers.id, ScheduledJobUpdate(), u["writer"], delivery_channel_id=ch["B1"])

        assert (moved.enabled, moved.pause_code, moved.delivery_channel_id) == (False, PauseCode.SIGN_IN_EXPIRED, ch["B1"])
        # Now held for the sign-in, the sign-in releases it.
        world["ready"].add(u["writer"].id)
        assert await svc.release_sign_in_holds(db, u["writer"]) == 1

    @pytest.mark.asyncio
    async def test_any_other_stop_is_not_lifted_by_a_move(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["B1"]), u["writer"])
        await svc.pause_job(db, job.id, u["writer"])

        moved = await svc.update_job(db, job.id, ScheduledJobUpdate(), u["writer"], delivery_channel_id=ch["P1"])

        assert (moved.enabled, moved.pause_code) == (False, PauseCode.MANUALLY_PAUSED)


class TestARowThatComesBackIsJudgedOnItsOwnChannel:
    @pytest.mark.asyncio
    async def test_a_revived_row_on_a_channel_that_reaches_them_is_not_held(self, world):
        """Revoked on leave, restored on rejoin: it kept the member's own channel (B1, which
        reaches them), so the owner's (A1, which does not) says nothing about it."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await _shared_on(world, "A1")
        writers = await svc.subscribe(db, job.definition_id, u["writer"])
        writers = await svc.update_job(db, writers.id, ScheduledJobUpdate(), u["writer"], delivery_channel_id=ch["B1"])
        assert writers.enabled is True
        await db.execute(
            text("UPDATE scheduled_job_subscriptions SET enabled = FALSE, pause_code = 'access_revoked' WHERE id = :id"),
            {"id": writers.id},
        )
        await db.commit()

        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])

        back = await svc.repo.get_job(db, writers.id)
        assert (back.enabled, back.pause_code, back.delivery_channel_id) == (True, None, ch["B1"])

    @pytest.mark.asyncio
    async def test_a_revived_row_on_a_channel_that_cannot_reach_them_is_held_for_it(self, world):
        """The reverse: the owner's channel reaches them, the row's own does not."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        await _bind(db, u["owner"].id, SLACK, "T2")
        job = await _shared_on(world, "B1")  # the owner's channel is in T2, where the writer is bound
        writers = await svc.subscribe(db, job.definition_id, u["writer"])
        await db.execute(
            text("""
                UPDATE scheduled_job_subscriptions
                SET enabled = FALSE, pause_code = 'access_revoked', delivery_channel_id = :a1 WHERE id = :id
            """),
            {"id": writers.id, "a1": ch["A1"]},
        )
        await db.commit()

        await svc.add_group_default_job(db, world["group"], job.definition_id, u["owner"])

        back = await svc.repo.get_job(db, writers.id)
        assert (back.enabled, back.pause_code) == (False, PauseCode.UNREACHABLE)
        note = (
            await db.execute(
                text("SELECT message FROM user_notifications WHERE user_id = :u AND type = :t"),
                {"u": u["writer"].id, "t": NotificationType.JOB_SUBSCRIPTION_ACTIVATED.value},
            )
        ).scalar_one()
        assert "('Slack Nannos (T1)')" in note, "the notice names the row's channel"


class TestAReportIsJudgedAgainstTheChannelTheRunUsed:
    @pytest.mark.asyncio
    async def test_a_report_for_a_run_sent_before_a_move_is_heard_but_holds_nothing(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        run_id = await svc.repo.create_run(db, job.id, delivery_channel_id=job.delivery_channel_id)
        await db.commit()
        # Moved while the run is in flight, to a channel of another client.
        await svc.update_job(db, job.id, ScheduledJobUpdate(), u["owner"], delivery_channel_id=ch["P1"])

        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="no_recipient")
        assert await svc.report_undelivered(db, SLACK, report) is True

        run = await svc.repo.get_run(db, job.id, run_id)
        assert (run.delivered, run.delivery_failure) == (False, "no_recipient"), "not recorded as delivered"
        still = await svc.get_job(db, job.id, u["owner"].id)
        assert (still.enabled, still.delivery_channel_id) == (True, ch["P1"]), "its new channel is not held"
        assert await _paused_notices(db, u["owner"].id) == 0


class TestARunRecordsTheChannelItsPushUses:
    @pytest.mark.asyncio
    async def test_the_channel_given_is_the_one_recorded(self, world):
        """The dispatch passes its own snapshot's channel: a move committed between the claim
        and the run's insert must not be what the report is judged against."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        await db.execute(text("UPDATE scheduled_job_subscriptions SET delivery_channel_id = :p WHERE id = :id"), {"p": ch["P1"], "id": job.id})
        run_id = await svc.repo.create_run(db, job.id, delivery_channel_id=ch["A1"])
        await db.commit()

        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="no_recipient")
        assert await svc.report_undelivered(db, SLACK, report) is True


class TestAReportAndTheRunsOutcomeStopTheJobOnce:
    @pytest.mark.asyncio
    async def test_a_report_on_a_job_already_off_keeps_its_stop_and_says_nothing(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        run_id = await svc.repo.create_run(db, job.id, delivery_channel_id=job.delivery_channel_id)
        await svc.pause_job(db, job.id, u["owner"])

        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="no_recipient")
        assert await svc.report_undelivered(db, SLACK, report) is True

        assert (await svc.repo.get_job(db, job.id)).pause_code == PauseCode.MANUALLY_PAUSED
        assert await _paused_notices(db, u["owner"].id) == 0

    @pytest.mark.asyncio
    async def test_a_run_that_fails_past_the_limit_on_a_held_job_keeps_the_hold(self, world):
        """The report landed first; the failed run that follows must not turn the hold
        into an auto-pause, which no sign-in releases."""
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"], max_failures=1), u["owner"])
        run_id = await svc.repo.create_run(db, job.id, delivery_channel_id=job.delivery_channel_id)
        await db.commit()
        report = UndeliveredReport(run_id=run_id, installation_id="A1", reason="no_recipient")
        assert await svc.report_undelivered(db, SLACK, report) is True

        enabled, reason = await svc.repo.complete_job(db, job.id, JobRunStatus.FAILED, next_run_at=None, leave_schedule=True)
        await db.commit()

        assert (enabled, reason) == (False, render_pause(PauseCode.UNREACHABLE))
        assert await svc.release_reachability_holds(db, u["owner"], SLACK, "T1") == 1


class TestTheCountIsOfJobsThatWouldRunAndDoNotArrive:
    @pytest.mark.asyncio
    async def test_a_job_off_for_another_reason_is_not_counted(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        paused = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])
        await svc.pause_job(db, paused.id, u["owner"])
        # A1 moves to a workspace the owner never signed in to: both now read unreachable.
        await db.execute(text("UPDATE delivery_channels SET workspace_id = 'T3' WHERE id = :id"), {"id": ch["A1"]})
        # The world mocks consent in Python; the onboarding reads the vault, and a switched-on
        # job under no live token is a sign-in issue, not a delivery one.
        await db.execute(
            text("INSERT INTO user_offline_tokens (user_id, encrypted_token) VALUES (:u, '\\x00')"),
            {"u": u["owner"].id},
        )

        assert await _undelivered_jobs(db, u["owner"].id) == 1, "the one that runs and reaches nobody, not the one they paused"


class TestARehold:
    @pytest.mark.asyncio
    async def test_a_rehold_never_overwrites_a_row_switched_on_meanwhile(self, world):
        svc, db, u, ch = world["service"], world["db"], world["users"], world["channels"]
        job = await svc.create_job(db, _watch_create(delivery_channel_id=ch["A1"]), u["owner"])

        assert await svc.repo.recode_stop(db, u["owner"], job.id, PauseCode.UNREACHABLE) is False
        assert (await svc.repo.get_job(db, job.id)).enabled is True
