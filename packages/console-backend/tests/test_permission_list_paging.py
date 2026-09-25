"""Opt-in paging and group-name search on the permission lists, plus search on
catalog pages and scheduled-job run history.

The three permission endpoints return bare arrays with the total in
`X-Total-Count`; these tests assert on the (rows, total) tuple underneath. Each
permissions dialog replaces the whole grant set on save, so the unbounded default
is load-bearing and tested explicitly.
"""

import pytest
from console_backend.repositories.catalog_repository import CatalogRepository
from console_backend.repositories.scheduled_job_repository import ScheduledJobRepository
from console_backend.services.secrets_service import SecretsService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Sorted by name: "50% off" < "Alpha team" < "Beta team" < "Gamma_ops" < "Gammaxops".
GROUP_NAMES = ["Alpha team", "Beta team", "Gamma_ops", "Gammaxops", "50% off"]


async def _seed_user(db: AsyncSession, user_id: str) -> str:
    await db.execute(
        text(
            "INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status) "
            "VALUES (:id, :sub, :email, 'P', 'G', false, 'member', 'active')"
        ),
        {"id": user_id, "sub": f"sub-{user_id}", "email": f"{user_id}@test.com"},
    )
    return user_id


async def _seed_groups(db: AsyncSession, prefix: str) -> list[int]:
    ids = []
    for name in GROUP_NAMES:
        result = await db.execute(
            text("INSERT INTO user_groups (name) VALUES (:name) RETURNING id"), {"name": f"{prefix} {name}"}
        )
        ids.append(result.scalar_one())
    return ids


async def _seed_catalog(db: AsyncSession, owner: str) -> str:
    result = await db.execute(
        text(
            "INSERT INTO catalogs (name, owner_user_id, source_type) VALUES ('Deck', :o, 'google_drive') RETURNING id"
        ),
        {"o": owner},
    )
    return str(result.scalar_one())


async def _seed_definition(db: AsyncSession, owner: str) -> tuple[int, int]:
    definition_id = (
        await db.execute(
            text("""
                INSERT INTO scheduled_job_definitions
                    (owner_user_id, name, prompt, job_type, schedule_kind, interval_seconds,
                     max_failures, destroy_after_trigger, trigger_policy, check_tool, cel_expr)
                VALUES
                    (:uid, 'Job', 'do it', 'watch', 'interval', 3600,
                     3, true, 'fixed', 'ping_tool', 'result != null')
                RETURNING id
            """),
            {"uid": owner},
        )
    ).scalar_one()
    job_id = (
        await db.execute(
            text("""
                INSERT INTO scheduled_job_subscriptions
                    (definition_id, user_id, next_run_at, enabled, consecutive_failures)
                VALUES (:d, :uid, NOW() + INTERVAL '1 hour', true, 0)
                RETURNING id
            """),
            {"d": definition_id, "uid": owner},
        )
    ).scalar_one()
    return definition_id, job_id


async def _grant_all(db: AsyncSession, table: str, fk: str, target, group_ids: list[int]) -> None:
    for gid in group_ids:
        await db.execute(
            text(f"INSERT INTO {table} ({fk}, user_group_id, permissions) VALUES (:t, :g, ARRAY['read'])"),
            {"t": target, "g": gid},
        )


async def _assert_permission_listing(list_fn, prefix: str, name_key: str) -> None:
    """Shared contract for the three permission lists: `list_fn(**kw) -> (rows, total)`."""
    every, total = await list_fn()
    assert total == 5
    assert [r[name_key] for r in every] == sorted(f"{prefix} {n}" for n in GROUP_NAMES)

    page1, total = await list_fn(page=1, limit=2)
    page3, total3 = await list_fn(page=3, limit=2)
    assert (len(page1), total) == (2, 5)
    assert (len(page3), total3) == (1, 5)
    assert {r["user_group_id"] for r in page1}.isdisjoint({r["user_group_id"] for r in page3})

    found, total = await list_fn(search="team")
    assert total == 2
    assert {r[name_key] for r in found} == {f"{prefix} Alpha team", f"{prefix} Beta team"}

    # `_` and `%` are literal, not LIKE wildcards.
    underscore, total = await list_fn(search="Gamma_")
    assert [r[name_key] for r in underscore] == [f"{prefix} Gamma_ops"]
    assert total == 1
    percent, total = await list_fn(search="50%")
    assert [r[name_key] for r in percent] == [f"{prefix} 50% off"]
    assert total == 1

    searched_page, total = await list_fn(search="team", page=2, limit=1)
    assert len(searched_page) == 1
    assert total == 2


@pytest.mark.asyncio
async def test_catalog_permissions_page_and_search(pg_session: AsyncSession):
    owner = await _seed_user(pg_session, "perm-catalog-owner")
    catalog_id = await _seed_catalog(pg_session, owner)
    groups = await _seed_groups(pg_session, "cat")
    await _grant_all(pg_session, "catalog_permissions", "catalog_id", catalog_id, groups)
    repo = CatalogRepository()

    async def list_fn(**kw):
        return await repo.list_permissions(pg_session, catalog_id, **kw)

    await _assert_permission_listing(list_fn, "cat", "user_group_name")
    # The unpaged accessor the set/replace flow uses still returns everything.
    assert len(await repo.get_permissions(pg_session, catalog_id)) == 5


@pytest.mark.asyncio
async def test_secret_permissions_page_and_search(pg_session: AsyncSession):
    owner = await _seed_user(pg_session, "perm-secret-owner")
    secret_id = (
        await pg_session.execute(
            text(
                "INSERT INTO secrets (owner_user_id, name, secret_type, ssm_parameter_name) "
                "VALUES (:o, 's', 'foundry_client_secret', '/test/perm-secret') RETURNING id"
            ),
            {"o": owner},
        )
    ).scalar_one()
    groups = await _seed_groups(pg_session, "sec")
    await _grant_all(pg_session, "secret_permissions", "secret_id", secret_id, groups)
    service = SecretsService()

    async def list_fn(**kw):
        return await service.get_permissions(pg_session, secret_id, **kw)

    await _assert_permission_listing(list_fn, "sec", "user_group_name")


@pytest.mark.asyncio
async def test_definition_permissions_page_and_search(pg_session: AsyncSession):
    owner = await _seed_user(pg_session, "perm-def-owner")
    definition_id, _ = await _seed_definition(pg_session, owner)
    groups = await _seed_groups(pg_session, "def")
    await _grant_all(pg_session, "scheduled_job_definition_permissions", "definition_id", definition_id, groups)
    repo = ScheduledJobRepository()

    async def list_fn(**kw):
        return await repo.list_permissions(pg_session, definition_id, **kw)

    await _assert_permission_listing(list_fn, "def", "user_group_name")
    assert len(await repo.get_permissions(pg_session, definition_id)) == 5


@pytest.mark.asyncio
async def test_catalog_pages_search_matches_title_and_file_name(pg_session: AsyncSession):
    owner = await _seed_user(pg_session, "pages-owner")
    catalog_id = await _seed_catalog(pg_session, owner)
    file_ids = {}
    for name in ("Q3 roadmap.pptx", "Pricing_2026.pdf"):
        file_ids[name] = (
            await pg_session.execute(
                text(
                    "INSERT INTO catalog_files (catalog_id, source_file_id, source_file_name) "
                    "VALUES (:c, :sid, :n) RETURNING id"
                ),
                {"c": catalog_id, "sid": f"src-{name}", "n": name},
            )
        ).scalar_one()
    pages = [
        ("Q3 roadmap.pptx", 1, "Intro", "roadmap body"),
        ("Q3 roadmap.pptx", 2, "Milestones", "nothing special"),
        ("Pricing_2026.pdf", 1, "Enterprise tier", "Milestones mentioned only in the body"),
    ]
    for file_name, number, title, body in pages:
        await pg_session.execute(
            text(
                "INSERT INTO catalog_pages (catalog_id, file_id, page_number, title, text_content) "
                "VALUES (:c, :f, :n, :t, :b)"
            ),
            {"c": catalog_id, "f": file_ids[file_name], "n": number, "t": title, "b": body},
        )
    repo = CatalogRepository()

    everything, total = await repo.get_catalog_pages(pg_session, catalog_id)
    assert (len(everything), total) == (3, 3)

    by_title, total = await repo.get_catalog_pages(pg_session, catalog_id, search="milestones")
    assert [p.title for p in by_title] == ["Milestones"], "body text is not searched"
    assert total == 1

    by_file, total = await repo.get_catalog_pages(pg_session, catalog_id, search="roadmap")
    assert {p.title for p in by_file} == {"Intro", "Milestones"}
    assert total == 2

    # `_` is literal: as a wildcard, "Q3_" would match the space in "Q3 roadmap".
    wildcard, total = await repo.get_catalog_pages(pg_session, catalog_id, search="Q3_")
    assert (wildcard, total) == ([], 0)
    literal, total = await repo.get_catalog_pages(pg_session, catalog_id, search="Pricing_")
    assert [p.title for p in literal] == ["Enterprise tier"]
    assert total == 1

    paged, total = await repo.get_catalog_pages(pg_session, catalog_id, limit=1, offset=1, search="roadmap")
    assert len(paged) == 1
    assert total == 2


@pytest.mark.asyncio
async def test_run_history_search_matches_summary_and_error(pg_session: AsyncSession):
    owner = await _seed_user(pg_session, "runs-search-owner")
    _, job_id = await _seed_definition(pg_session, owner)
    repo = ScheduledJobRepository()
    run_ids = [await repo.create_run(pg_session, job_id) for _ in range(4)]
    updates = [
        ("Posted the 100% digest", None),
        ("Nothing new", None),
        (None, "Tool call_timeout after 30s"),
        (None, "calltimeout without underscore"),
    ]
    for run_id, (summary, error) in zip(run_ids, updates):
        await pg_session.execute(
            text("UPDATE scheduled_job_runs SET result_summary = :s, error_message = :e WHERE id = :id"),
            {"s": summary, "e": error, "id": run_id},
        )
    await pg_session.commit()

    by_summary, total = await repo.list_runs(pg_session, job_id, search="digest")
    assert [r.id for r in by_summary] == [run_ids[0]]
    assert total == 1

    by_error, total = await repo.list_runs(pg_session, job_id, search="call_timeout")
    assert [r.id for r in by_error] == [run_ids[2]], "`_` must not act as a wildcard"
    assert total == 1

    percent, total = await repo.list_runs(pg_session, job_id, search="100%")
    assert [r.id for r in percent] == [run_ids[0]]
    assert total == 1

    everything, total = await repo.list_runs(pg_session, job_id)
    assert total == 4
