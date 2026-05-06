"""Repository for bug reports with automatic audit logging."""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditEntityType
from ..models.bug_report import BugReportResponse, BugReportStatus
from ..models.user import User
from .base import AuditedRepository

logger = logging.getLogger(__name__)


def _row_to_response(row: Any) -> BugReportResponse:
    return BugReportResponse(
        id=str(row["id"]),
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        task_id=row["task_id"],
        user_id=row["user_id"],
        source=row["source"],
        description=row["description"],
        status=row["status"],
        external_link=row["external_link"],
        debug_conversation_id=row["debug_conversation_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class BugReportRepository(AuditedRepository):
    def __init__(self) -> None:
        super().__init__(
            entity_type=AuditEntityType.BUG_REPORT,
            table_name="bug_reports",
        )

    async def create_bug_report(
        self,
        db: AsyncSession,
        actor: User,
        conversation_id: str,
        source: str,
        message_id: str | None = None,
        task_id: str | None = None,
        description: str | None = None,
    ) -> BugReportResponse:
        fields: dict[str, Any] = {
            "conversation_id": conversation_id,
            "user_id": actor.id,
            "source": source,
            "description": description,
        }
        if message_id is not None:
            fields["message_id"] = message_id
        if task_id is not None:
            fields["task_id"] = task_id

        report_id = await self.create(
            db=db,
            actor=actor,
            fields=fields,
            returning="id",
        )

        row = await self._get_row(db, report_id)
        assert row is not None
        return _row_to_response(row)

    async def get_bug_report(
        self,
        db: AsyncSession,
        report_id: str,
    ) -> BugReportResponse | None:
        row = await self._get_row(db, report_id)
        if row is None:
            return None
        return _row_to_response(row)

    async def list_bug_reports(
        self,
        db: AsyncSession,
        user_id: str | None = None,
        status: BugReportStatus | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[BugReportResponse], int]:
        conditions = []
        params: dict[str, Any] = {}

        if user_id is not None:
            conditions.append("user_id = :user_id")
            params["user_id"] = user_id

        if status is not None:
            conditions.append("status = :status")
            params["status"] = status.value

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        count_query = text(f"SELECT COUNT(*) FROM bug_reports {where_clause}")
        count_result = await db.execute(count_query, params)
        total = count_result.scalar() or 0

        data_query = text(f"""
            SELECT * FROM bug_reports {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)
        params["limit"] = limit
        params["offset"] = (page - 1) * limit
        data_result = await db.execute(data_query, params)
        rows = data_result.mappings().all()

        return [_row_to_response(row) for row in rows], total

    async def update_status(
        self,
        db: AsyncSession,
        actor: User,
        report_id: str,
        new_status: BugReportStatus,
    ) -> BugReportResponse | None:
        await self.update(
            db=db,
            actor=actor,
            entity_id=report_id,
            fields={"status": new_status.value},
        )
        row = await self._get_row(db, report_id)
        if row is None:
            return None
        return _row_to_response(row)

    async def update_external_link(
        self,
        db: AsyncSession,
        actor: User,
        report_id: str,
        external_link: str,
    ) -> BugReportResponse | None:
        await self.update(
            db=db,
            actor=actor,
            entity_id=report_id,
            fields={"external_link": external_link},
        )
        row = await self._get_row(db, report_id)
        if row is None:
            return None
        return _row_to_response(row)

    async def _get_row(self, db: AsyncSession, report_id: Any) -> Any | None:
        query = text("SELECT * FROM bug_reports WHERE id = :id")
        result = await db.execute(query, {"id": report_id})
        return result.mappings().first()
