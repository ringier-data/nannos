"""Service for managing bug reports."""

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization import SYSTEM_ROLE_CAPABILITIES
from ..models.bug_report import BugReportResponse, BugReportStatus
from ..models.notification import NotificationData, NotificationType
from ..models.user import User
from ..repositories.bug_report_repository import BugReportRepository
from .notification_service import NotificationService

logger = logging.getLogger(__name__)

#: Roles whose capabilities include triaging bug reports. Derived from the capability
#: table rather than listed by hand, so a role that gains or loses triage is not left
#: silently in (or out of) the notification audience.
_TRIAGE_ROLES = sorted(
    role
    for role, capabilities in SYSTEM_ROLE_CAPABILITIES.items()
    if {"triage", "triage.admin"} & capabilities.get("bug_reports", set())
)

#: The seeded service account that owns auto-provisioned agents (migration 041). It has
#: role 'admin' but no person behind it, so it is never a notification recipient.
_SYSTEM_USER_ID = "system"

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

        # Notify triagers after the commit, so a failing notification cannot lose the
        # report itself — the same ordering the other notification producers use.
        await self._notify_triagers(db=db, actor=actor, report=report)

        return report

    async def _notify_triagers(
        self,
        db: AsyncSession,
        actor: User,
        report: BugReportResponse,
    ) -> None:
        """Put a newly filed report in the inbox of everyone who can triage it.

        Best-effort by construction: the report is already committed, and a bug report
        that exists but was not announced is a far better outcome than a filing that
        failed because an inbox insert did.

        The reporter is excluded even when they can triage: they know what they just
        filed, and the notification is there to tell someone who does not.

        So is the seeded `system` user (migration 041), which carries role 'admin' to own
        auto-provisioned agents and has no person behind it. This is the first
        notification audience selected by *role* rather than by group membership, so it
        is the first one that would reach it — an inbox nobody opens, growing one row per
        bug report forever.
        """
        if self.notification_service is None:
            return

        try:
            recipients = await db.execute(
                text("""
                    SELECT id FROM users
                    WHERE (role = ANY(:triage_roles) OR is_administrator IS TRUE)
                      AND status = 'active'
                      AND deleted_at IS NULL
                      AND id != :actor_id
                      AND id != :system_user_id
                """),
                {
                    "triage_roles": _TRIAGE_ROLES,
                    "actor_id": actor.id,
                    "system_user_id": _SYSTEM_USER_ID,
                },
            )
            recipient_ids = [row[0] for row in recipients.fetchall()]
            if not recipient_ids:
                logger.info("Bug report %s: no triagers to notify", report.id)
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
            logger.error("Bug report %s: failed to notify triagers: %s", report.id, exc)
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

    async def list_bug_reports(
        self,
        db: AsyncSession,
        user_id: str | None = None,
        status: BugReportStatus | None = None,
        created_after: datetime | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[BugReportResponse], int]:
        return await self.repository.list_bug_reports(
            db=db,
            user_id=user_id,
            status=status,
            created_after=created_after,
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
