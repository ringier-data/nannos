"""Service for managing bug reports."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.bug_report import BugReportResponse, BugReportStatus
from ..models.user import User
from ..repositories.bug_report_repository import BugReportRepository

logger = logging.getLogger(__name__)


class BugReportService:
    def __init__(self) -> None:
        self._repository: BugReportRepository | None = None

    def set_repository(self, repository: BugReportRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> BugReportRepository:
        if self._repository is None:
            raise RuntimeError("BugReportRepository not injected. Call set_repository() during initialization.")
        return self._repository

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
        report = await self.repository.create_bug_report(
            db=db,
            actor=actor,
            conversation_id=conversation_id,
            source=source,
            message_id=message_id,
            task_id=task_id,
            description=description,
        )
        await db.commit()
        logger.info(f"Bug report {report.id} created by {actor.sub} for conversation {conversation_id}")
        return report

    async def get_bug_report(
        self,
        db: AsyncSession,
        report_id: str,
    ) -> BugReportResponse | None:
        return await self.repository.get_bug_report(db=db, report_id=report_id)

    async def list_bug_reports(
        self,
        db: AsyncSession,
        user_id: str | None = None,
        status: BugReportStatus | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[BugReportResponse], int]:
        return await self.repository.list_bug_reports(
            db=db,
            user_id=user_id,
            status=status,
            page=page,
            limit=limit,
        )

    async def update_status(
        self,
        db: AsyncSession,
        actor: User,
        report_id: str,
        new_status: BugReportStatus,
    ) -> BugReportResponse | None:
        report = await self.repository.update_status(
            db=db,
            actor=actor,
            report_id=report_id,
            new_status=new_status,
        )
        await db.commit()
        if report:
            logger.info(f"Bug report {report_id} status updated to {new_status.value} by {actor.sub}")
        return report

    async def update_external_link(
        self,
        db: AsyncSession,
        actor: User,
        report_id: str,
        external_link: str,
    ) -> BugReportResponse | None:
        report = await self.repository.update_external_link(
            db=db,
            actor=actor,
            report_id=report_id,
            external_link=external_link,
        )
        await db.commit()
        if report:
            logger.info(f"Bug report {report_id} external_link set by {actor.sub}")
        return report
