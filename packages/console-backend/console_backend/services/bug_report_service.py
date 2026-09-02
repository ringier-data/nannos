"""Service for managing bug reports."""

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.bug_report import BugReportListResponse, BugReportResponse, BugReportStatus
from ..models.notification import NotificationData, NotificationType
from ..models.user import PaginationMeta, User
from ..repositories.bug_report_repository import BugReportRepository
from .notification_service import NotificationService

logger = logging.getLogger(__name__)

#: How much of a report's description goes into the notification body. The inbox shows a
#: one-line message; the report itself holds the full text.
_MESSAGE_DESCRIPTION_CHARS = 200


class BugReportService:
    def __init__(self) -> None:
        self._repository: BugReportRepository | None = None
        self.notification_service: NotificationService | None = None

    def set_repository(self, repository: BugReportRepository) -> None:
        self._repository = repository

    def set_notification_service(self, notification_service: NotificationService) -> None:
        self.notification_service = notification_service

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

        # Notify after the commit, so a failing notification cannot lose the report
        # itself — the same ordering the other notification producers use.
        await self._notify_administrators(db=db, actor=actor, report=report)

        return report

    async def _notify_administrators(
        self,
        db: AsyncSession,
        actor: User,
        report: BugReportResponse,
    ) -> None:
        """Put a newly filed report in the inbox of everyone who can read it.

        Administrators, and only administrators: the audience is matched to *read*
        visibility rather than to the `triage` capability. An approver holds `triage` and
        so may change a report's status, but every read path is administrator-or-owner —
        `get_bug_report` 404s for them, both list surfaces filter to their own reports,
        and the console page is admin-gated. Notifying them would hand out a description
        snippet for a report they cannot open, and point them at something they can only
        act on blind.

        `is_administrator` is also the more durable key: `triage` is a capability whose
        future is undecided, and an audience keyed on it would have to be revisited if it
        ever changes or goes away.

        Best-effort by construction: the report is already committed, and a bug report
        that exists but was not announced is a far better outcome than a filing that
        failed because an inbox insert did.

        The reporter is excluded even when they are an administrator: they know what they
        just filed, and the notification is there to tell someone who does not.

        Note `is_administrator`, not `role = 'admin'`. The seeded `system` user (migration
        041) carries role 'admin' to own auto-provisioned agents, with no person behind it
        and `is_administrator` left FALSE — so selecting on the flag leaves it out, while
        selecting on the role would fill an inbox nobody opens, one row per bug report
        forever. An audience that must select by role needs to exclude it explicitly.
        """
        if self.notification_service is None:
            return

        try:
            recipients = await db.execute(
                text("""
                    SELECT id FROM users
                    WHERE is_administrator IS TRUE
                      AND status = 'active'
                      AND deleted_at IS NULL
                      AND id != :actor_id
                """),
                {"actor_id": actor.id},
            )
            recipient_ids = [row[0] for row in recipients.fetchall()]
            if not recipient_ids:
                logger.info("Bug report %s: no administrators to notify", report.id)
                return

            description = (report.description or "").strip()
            summary = description[:_MESSAGE_DESCRIPTION_CHARS] or "No description was given."
            if len(description) > _MESSAGE_DESCRIPTION_CHARS:
                summary += "…"

            await self.notification_service.bulk_create_notifications(
                db,
                [
                    NotificationData(
                        user_id=recipient_id,
                        notification_type=NotificationType.BUG_REPORT_FILED,
                        title="New bug report filed",
                        message=summary,
                        metadata={
                            "bug_report_id": report.id,
                            "conversation_id": report.conversation_id,
                            "source": report.source.value,
                            "reported_by": actor.id,
                        },
                    )
                    for recipient_id in recipient_ids
                ],
            )
            # Committed separately from the report, which is already durable.
            await db.commit()
        except Exception as exc:
            logger.error("Bug report %s: failed to notify administrators: %s", report.id, exc)
            # Deliberately not re-raised — see the docstring. Rolled back so a half-done
            # insert cannot leave the session in a failed transaction for whatever the
            # request does next; the report itself is already committed.
            try:
                await db.rollback()
            except Exception:
                logger.exception("Bug report %s: rollback after a failed notification also failed", report.id)

    async def get_bug_report(
        self,
        db: AsyncSession,
        report_id: str,
    ) -> BugReportResponse | None:
        return await self.repository.get_bug_report(db=db, report_id=report_id)

    async def list_for_user(
        self,
        db: AsyncSession,
        user: User,
        status: BugReportStatus | None = None,
        created_after: datetime | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> BugReportListResponse:
        """List the reports *this* user may see, newest first.

        The one place that decides which reports a caller sees: administrators see every
        report, everyone else only their own. Both the REST route and the MCP tool go
        through here, because two copies of a visibility rule are two things to change
        when the rule changes — and only one of them would be.

        Triage capability deliberately does not widen the rule. It governs acting on a
        report (the PATCH routes), not reading every user's.
        """
        user_id_filter = None if user.is_administrator else user.id

        reports, total = await self.repository.list_bug_reports(
            db=db,
            user_id=user_id_filter,
            status=status,
            created_after=created_after,
            page=page,
            limit=limit,
        )
        return BugReportListResponse(
            data=reports,
            meta=PaginationMeta(page=page, limit=limit, total=total),
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
