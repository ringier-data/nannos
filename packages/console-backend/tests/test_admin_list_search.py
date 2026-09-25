"""Search and opt-in paging for the admin SCIM-token, outbound-SCIM and audit-log lists."""

import os

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

import pytest
import pytest_asyncio
from sqlalchemy import text

from console_backend.models.audit import AuditAction, AuditEntityType
from console_backend.models.user import User
from console_backend.services.audit_service import AuditService
from console_backend.services.outbound_scim_endpoint_service import OutboundScimEndpointService
from console_backend.services.scim_token_service import ScimTokenService

ADMIN_ID = "list-search-admin"


@pytest_asyncio.fixture
async def admin_row(pg_session):
    await pg_session.execute(
        text("""
            INSERT INTO users (id, sub, email, first_name, last_name, is_administrator, role, status)
            VALUES (:id, :id, 'ada@example.com', 'Ada', 'Lovelace', true, 'admin', 'active')
        """),
        {"id": ADMIN_ID},
    )
    await pg_session.commit()


async def _insert_token(session, name: str, description: str | None, minutes_ago: int) -> None:
    await session.execute(
        text("""
            INSERT INTO scim_tokens (name, description, token, created_by, created_at)
            VALUES (:name, :description, :token, :created_by, NOW() - make_interval(mins => :minutes_ago))
        """),
        {
            "name": name,
            "description": description,
            "token": f"tok-{name}",
            "created_by": ADMIN_ID,
            "minutes_ago": minutes_ago,
        },
    )


async def _insert_endpoint(session, name: str, url: str, minutes_ago: int, deleted: bool = False) -> None:
    await session.execute(
        text("""
            INSERT INTO outbound_scim_endpoints (name, endpoint_url, bearer_token, created_by, created_at, deleted_at)
            VALUES (:name, :url, 'bearer-xyz1', :created_by, NOW() - make_interval(mins => :minutes_ago),
                    CASE WHEN :deleted THEN NOW() END)
        """),
        {"name": name, "url": url, "created_by": ADMIN_ID, "minutes_ago": minutes_ago, "deleted": deleted},
    )


@pytest.mark.asyncio
class TestScimTokenListing:
    @pytest_asyncio.fixture
    async def tokens(self, pg_session, admin_row):
        await _insert_token(pg_session, "okta-prod", "Okta production", 1)
        await _insert_token(pg_session, "okta_stage", None, 2)
        await _insert_token(pg_session, "oktaXstage", "Entra fallback", 3)
        await _insert_token(pg_session, "azure", "100% of groups", 4)
        await pg_session.commit()

    async def test_default_is_unbounded(self, pg_session, tokens):
        rows, total = await ScimTokenService().list_tokens(pg_session)
        assert [t.name for t in rows] == ["okta-prod", "okta_stage", "oktaXstage", "azure"]
        assert total == 4

    async def test_page_and_total(self, pg_session, tokens):
        rows, total = await ScimTokenService().list_tokens(pg_session, page=2, limit=3)
        assert [t.name for t in rows] == ["azure"]
        assert total == 4

    async def test_search_matches_name_and_description(self, pg_session, tokens):
        rows, total = await ScimTokenService().list_tokens(pg_session, search="ENTRA")
        assert [t.name for t in rows] == ["oktaXstage"]
        assert total == 1

    async def test_search_escapes_like_metacharacters(self, pg_session, tokens):
        rows, _ = await ScimTokenService().list_tokens(pg_session, search="okta_")
        assert [t.name for t in rows] == ["okta_stage"]
        rows, _ = await ScimTokenService().list_tokens(pg_session, search="100%")
        assert [t.name for t in rows] == ["azure"]

    async def test_search_total_counts_all_matches_not_the_page(self, pg_session, tokens):
        rows, total = await ScimTokenService().list_tokens(pg_session, search="okta", page=1, limit=2)
        assert [t.name for t in rows] == ["okta-prod", "okta_stage"]
        assert total == 3


@pytest.mark.asyncio
class TestOutboundScimEndpointListing:
    @pytest_asyncio.fixture
    async def endpoints(self, pg_session, admin_row):
        await _insert_endpoint(pg_session, "gateway", "https://gw.example.com/scim", 1)
        await _insert_endpoint(pg_session, "hr_sync", "https://hr.example.com/scim", 2)
        await _insert_endpoint(pg_session, "hrXsync", "https://other.example.org/scim", 3)
        await _insert_endpoint(pg_session, "gone", "https://gw.example.com/old", 4, deleted=True)
        await pg_session.commit()

    async def test_default_is_unbounded_and_skips_deleted(self, pg_session, endpoints):
        rows, total = await OutboundScimEndpointService().list_endpoints(pg_session)
        assert [e.name for e in rows] == ["gateway", "hr_sync", "hrXsync"]
        assert total == 3

    async def test_page_and_total(self, pg_session, endpoints):
        rows, total = await OutboundScimEndpointService().list_endpoints(pg_session, page=2, limit=2)
        assert [e.name for e in rows] == ["hrXsync"]
        assert total == 3

    async def test_search_matches_url_but_not_deleted(self, pg_session, endpoints):
        rows, total = await OutboundScimEndpointService().list_endpoints(pg_session, search="gw.example")
        assert [e.name for e in rows] == ["gateway"]
        assert total == 1

    async def test_search_escapes_underscore(self, pg_session, endpoints):
        rows, _ = await OutboundScimEndpointService().list_endpoints(pg_session, search="hr_")
        assert [e.name for e in rows] == ["hr_sync"]


@pytest.mark.asyncio
class TestAuditLogSearch:
    @pytest_asyncio.fixture
    async def logs(self, pg_session, admin_row):
        service = AuditService()
        ada = User(id=ADMIN_ID, sub=ADMIN_ID, email="ada@example.com", first_name="Ada", last_name="Lovelace")
        # A service account whose sub has no users row.
        robot = User(id="svc-robot", sub="svc-robot", email="robot@example.com", first_name="Svc", last_name="Bot")
        await service.log_action(pg_session, ada, AuditEntityType.USER, "user_42", AuditAction.UPDATE)
        await service.log_action(pg_session, ada, AuditEntityType.GROUP, "userX42", AuditAction.CREATE)
        await service.log_action(
            pg_session, robot, AuditEntityType.USER, "other", AuditAction.DELETE, changes={"email": "ada@x"}
        )
        await pg_session.commit()

    async def test_search_by_entity_id_escapes_underscore(self, pg_session, logs):
        rows, total = await AuditService().list_logs(pg_session, search="user_")
        assert [log.entity_id for log in rows] == ["user_42"]
        assert total == 1

    async def test_search_by_actor_name_and_email(self, pg_session, logs):
        _, total = await AuditService().list_logs(pg_session, search="ada lovelace")
        assert total == 2
        _, total = await AuditService().list_logs(pg_session, search="ADA@EXAMPLE")
        assert total == 2

    async def test_search_by_actor_sub_without_user_row(self, pg_session, logs):
        rows, total = await AuditService().list_logs(pg_session, search="robot")
        assert [log.entity_id for log in rows] == ["other"]
        assert total == 1

    async def test_search_combines_with_filters_and_paging(self, pg_session, logs):
        rows, total = await AuditService().list_logs(
            pg_session, search="lovelace", entity_type=AuditEntityType.USER, page=1, limit=1
        )
        assert [log.entity_id for log in rows] == ["user_42"]
        assert total == 1
