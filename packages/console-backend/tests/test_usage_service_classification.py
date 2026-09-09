"""How usage rows are classified by service.

The breakdown used to *derive* the service from whichever id happened to be set, which
left console-backend's own utility calls — naming a conversation, drafting a scheduled
job — indistinguishable from an agent run. A caller can now say what it is; the
derivation stays as the fallback so historical rows and every agent-path row (the
orchestrator declares nothing) classify exactly as they did.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from console_backend.repositories.usage_repository import UsageRepository


async def _a_user(pg_session, user_id="u-classify") -> str:
    await pg_session.execute(
        text(
            "INSERT INTO users (id, sub, email, first_name, last_name) "
            "VALUES (:id, :sub, :email, 'T', 'U') ON CONFLICT (id) DO NOTHING"
        ),
        {"id": user_id, "sub": f"sub-of-{user_id}", "email": f"{user_id}@example.com"},
    )
    return user_id


async def _log(pg_session, user_id, **kwargs) -> None:
    await UsageRepository().create_usage_log(
        db=pg_session,
        user_id=user_id,
        provider="bedrock",
        model_name="chat-low",
        total_cost_usd=kwargs.pop("cost", 1),
        billing_unit_breakdown={"input_tokens": 10},
        invoked_at=datetime.now(timezone.utc),
        **kwargs,
    )


async def _buckets(pg_session, user_id) -> dict[str, int]:
    rows = await UsageRepository().get_usage_by_service(pg_session, user_id)
    return {row["service"]: row["total_requests"] for row in rows}


@pytest.mark.asyncio
async def test_a_declared_service_is_what_it_says(pg_session):
    user_id = await _a_user(pg_session)
    await _log(pg_session, user_id, service="console")

    assert await _buckets(pg_session, user_id) == {"console": 1}


@pytest.mark.asyncio
async def test_console_work_used_to_read_as_an_agent_run(pg_session):
    """The regression this closes: a titling call carries no scheduled_job_id and no
    catalog_id, so the derivation could only call it 'orchestrator'."""
    user_id = await _a_user(pg_session)
    await _log(pg_session, user_id, conversation_id=None, service=None)  # as it was written before
    await _log(pg_session, user_id, service="console")  # as it is written now

    assert await _buckets(pg_session, user_id) == {"orchestrator": 1, "console": 1}


@pytest.mark.asyncio
async def test_the_declared_service_survives_the_whole_read_path(pg_session):
    """The row is written, listed, and rendered as an API model. `/my-logs` builds
    `UsageLog` field by field, so a column the query returns is still dropped unless it is
    named there — which is exactly how the detail table kept saying 'Orchestrator' for
    console work after the dimension existed everywhere else."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from console_backend.routers.usage_router import get_my_usage_logs

    user_id = await _a_user(pg_session, "u-readpath")
    await _log(pg_session, user_id, service="console")

    from console_backend.repositories.usage_repository import UsageRepository
    from console_backend.services.usage_service import UsageService

    request = MagicMock()
    request.app.state.usage_service = UsageService(usage_repository=UsageRepository())
    # Query(...) defaults are not applied when the endpoint is called directly.
    result = await get_my_usage_logs(
        request=request,
        db=pg_session,
        current_user=SimpleNamespace(id=user_id, sub=f"sub-of-{user_id}"),
        page=1,
        limit=50,
        days=30,
        conversation_id=None,
        sub_agent_id=None,
    )

    assert [log.service for log in result.logs] == ["console"]


@pytest.mark.asyncio
async def test_rows_that_declare_nothing_still_derive_as_before(pg_session):
    """Every row written before the column existed, and every agent-path row."""
    user_id = await _a_user(pg_session)
    await _log(pg_session, user_id)  # nothing set at all
    await _log(pg_session, user_id, catalog_id=None, conversation_id="c1")

    assert await _buckets(pg_session, user_id) == {"orchestrator": 2}
