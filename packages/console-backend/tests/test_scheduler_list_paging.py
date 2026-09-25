"""Opt-in paging and server-side search on the scheduler list endpoints.

Both `scheduler_list_jobs` and `scheduler_list_shared_jobs` are MCP tools whose
bodies must stay bare arrays, so the total travels in `X-Total-Count` and these
tests assert on the (rows, total) tuple the service returns.
"""

import pytest
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def repo() -> ScheduledJobRepository:
    return ScheduledJobRepository()


async def _seed_user(pg_session: AsyncSession, user_id: str) -> str:
    await pg_session.execute(
        text(
            "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) "
            "VALUES (:id, :sub, :email, 'P', 'G', false, 'member', 'active')"
        ),
        {"id": user_id, "sub": f"sub-{user_id}", "email": f"{user_id}@test.com"},
    )
    return user_id


async def _seed_job(pg_session: AsyncSession, user_id: str, name: str, prompt: str = "do the thing") -> int:
    definition_id = (
        await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_definitions
                    (owner_user_id, name, prompt, job_type, schedule_kind, interval_seconds,
                     max_failures, destroy_after_trigger, trigger_policy, check_tool, cel_expr)
                VALUES
                    (:uid, :name, :prompt, 'watch', 'interval', 3600,
                     3, true, 'fixed', 'ping_tool', 'result != null')
                RETURNING id
            """),
            {"uid": user_id, "name": name, "prompt": prompt},
        )
    ).scalar_one()
    return (
        await pg_session.execute(
            text("""
                INSERT INTO scheduled_job_subscriptions
                    (definition_id, user_id, next_run_at, enabled, consecutive_failures)
                VALUES (:definition_id, :uid, NOW() + INTERVAL '1 hour', true, 0)
                RETURNING id
            """),
            {"definition_id": definition_id, "uid": user_id},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_list_jobs_unbounded_by_default(repo, pg_session: AsyncSession):
    """No limit returns every subscription — the MCP callers depend on it."""
    user_id = await _seed_user(pg_session, "paging-user-a")
    for i in range(5):
        await _seed_job(pg_session, user_id, f"Job {i}")

    jobs, total = await repo.list_jobs(pg_session, user_id)
    assert len(jobs) == 5
    assert total == 5


@pytest.mark.asyncio
async def test_list_jobs_pages_with_accurate_total(repo, pg_session: AsyncSession):
    user_id = await _seed_user(pg_session, "paging-user-b")
    for i in range(5):
        await _seed_job(pg_session, user_id, f"Job {i}")

    page1, total = await repo.list_jobs(pg_session, user_id, page=1, limit=2)
    assert len(page1) == 2
    assert total == 5

    page3, total = await repo.list_jobs(pg_session, user_id, page=3, limit=2)
    assert len(page3) == 1
    assert total == 5
    assert {j.id for j in page1}.isdisjoint({j.id for j in page3})


@pytest.mark.asyncio
async def test_list_jobs_search_matches_name_and_prompt(repo, pg_session: AsyncSession):
    user_id = await _seed_user(pg_session, "paging-user-c")
    await _seed_job(pg_session, user_id, "Invoice sweep", prompt="reconcile invoices")
    await _seed_job(pg_session, user_id, "Standup nudge", prompt="post the reminder")

    by_name, total = await repo.list_jobs(pg_session, user_id, search="invoice")
    assert [j.name for j in by_name] == ["Invoice sweep"]
    assert total == 1

    by_prompt, total = await repo.list_jobs(pg_session, user_id, search="reminder")
    assert [j.name for j in by_prompt] == ["Standup nudge"]
    assert total == 1

    missing, total = await repo.list_jobs(pg_session, user_id, search="nothing-here")
    assert missing == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_jobs_is_scoped_to_the_caller_under_paging(repo, pg_session: AsyncSession):
    """Another user's jobs must not leak in, and must not inflate the total."""
    mine = await _seed_user(pg_session, "paging-user-d")
    theirs = await _seed_user(pg_session, "paging-user-e")
    for i in range(2):
        await _seed_job(pg_session, mine, f"Mine {i}")
    for i in range(4):
        await _seed_job(pg_session, theirs, f"Theirs {i}")

    jobs, total = await repo.list_jobs(pg_session, mine, page=1, limit=10)
    assert total == 2
    assert {j.name for j in jobs} == {"Mine 0", "Mine 1"}


@pytest.mark.asyncio
async def test_available_definitions_page_and_search(repo, pg_session: AsyncSession):
    """Own definitions are readable; paging and search narrow rows and total together."""
    user_id = await _seed_user(pg_session, "paging-user-f")
    for i in range(4):
        await _seed_job(pg_session, user_id, f"Shared {i}")
    await _seed_job(pg_session, user_id, "Quarterly digest")

    every, total = await repo.list_available_definitions(pg_session, user_id)
    assert len(every) == 5
    assert total == 5

    page1, total = await repo.list_available_definitions(pg_session, user_id, page=1, limit=2)
    assert len(page1) == 2
    assert total == 5

    found, total = await repo.list_available_definitions(pg_session, user_id, search="quarterly")
    assert [d.name for d in found] == ["Quarterly digest"]
    assert total == 1


@pytest.mark.asyncio
async def test_available_definitions_subscribed_filter(repo, pg_session: AsyncSession):
    """"Shared with you" hides what the viewer already activated, in SQL.

    Filtering that in the browser would drop rows from whichever page arrived, so
    the section could look empty while unactivated definitions remained.
    """
    owner = await _seed_user(pg_session, "sub-filter-owner")
    viewer = await _seed_user(pg_session, "sub-filter-viewer")

    # Public definitions are readable by anyone, which is enough to list them.
    ids = []
    for i in range(3):
        job_id = await _seed_job(pg_session, owner, f"Public {i}")
        definition_id = (
            await pg_session.execute(
                text(
                    "UPDATE scheduled_job_definitions SET is_public = TRUE "
                    "WHERE id = (SELECT definition_id FROM scheduled_job_subscriptions WHERE id = :j) "
                    "RETURNING id"
                ),
                {"j": job_id},
            )
        ).scalar_one()
        ids.append(definition_id)

    # The viewer activates one of them.
    await pg_session.execute(
        text("""
            INSERT INTO scheduled_job_subscriptions
                (definition_id, user_id, next_run_at, enabled, consecutive_failures)
            VALUES (:definition_id, :uid, NOW() + INTERVAL '1 hour', true, 0)
        """),
        {"definition_id": ids[0], "uid": viewer},
    )

    everything, total = await repo.list_available_definitions(pg_session, viewer)
    assert total == 3

    unactivated, total = await repo.list_available_definitions(
        pg_session, viewer, subscribed=False
    )
    assert total == 2
    assert ids[0] not in {d.id for d in unactivated}

    activated, total = await repo.list_available_definitions(pg_session, viewer, subscribed=True)
    assert total == 1
    assert [d.id for d in activated] == [ids[0]]


@pytest.mark.asyncio
async def test_run_history_pages_past_the_old_fifty_cap(repo, pg_session: AsyncSession):
    """Run 51 has to be reachable.

    The listing used to take a bare `LIMIT 50` with no offset, so a job's 51st
    run simply could not be retrieved through the list at all — and run history
    is the one table here that only ever grows.
    """
    user_id = await _seed_user(pg_session, "runs-user")
    job_id = await _seed_job(pg_session, user_id, "Busy job")
    for _ in range(55):
        await repo.create_run(pg_session, job_id)
    await pg_session.commit()

    first, total = await repo.list_runs(pg_session, job_id)
    assert len(first) == 50, "the historic default page size is unchanged"
    assert total == 55, "but the total no longer stops at the page size"

    second, total = await repo.list_runs(pg_session, job_id, limit=50, page=2)
    assert len(second) == 5
    assert total == 55
    assert {r.id for r in first}.isdisjoint({r.id for r in second})


@pytest.mark.asyncio
async def test_run_history_status_filter(repo, pg_session: AsyncSession):
    """The status facet narrows rows and total together."""
    user_id = await _seed_user(pg_session, "runs-status-user")
    job_id = await _seed_job(pg_session, user_id, "Mixed job")
    run_ids = [await repo.create_run(pg_session, job_id) for _ in range(4)]
    await pg_session.execute(
        text("UPDATE scheduled_job_runs SET status = 'failed' WHERE id = ANY(:ids)"),
        {"ids": run_ids[:1]},
    )
    await pg_session.commit()

    failed, total = await repo.list_runs(pg_session, job_id, status="failed")
    assert total == 1
    assert [r.id for r in failed] == run_ids[:1]

    everything, total = await repo.list_runs(pg_session, job_id)
    assert total == 4
