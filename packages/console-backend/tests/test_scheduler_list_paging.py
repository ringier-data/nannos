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
