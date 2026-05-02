"""API routes for user notifications."""

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..db.session import DbSession
from ..dependencies import User, require_auth
from ..models.notification import NotificationListResponse, UnreadCountResponse
from ..services.notification_service import NotificationService

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


def get_notification_service(request: Request) -> NotificationService:
    """Get the notification service from the request state."""
    return request.app.state.notification_service


@router.get("")
async def get_notifications(
    request: Request,
    db: DbSession,
    page: int = 1,
    limit: int = 50,
    unread_only: bool = False,
    user: User = Depends(require_auth),
) -> NotificationListResponse:
    """Get notifications for the current user with pagination.

    Query Parameters:
    - page: Page number (default: 1)
    - limit: Items per page (default: 50, max: 100)
    - unread_only: If true, only return unread notifications (default: false)
    """
    notification_service = get_notification_service(request)

    if limit > 100:
        limit = 100

    notifications, total = await notification_service.get_user_notifications(
        db=db,
        user_id=user.id,
        page=page,
        limit=limit,
        unread_only=unread_only,
    )

    return NotificationListResponse(
        items=notifications,
        total=total,
        unread_count=await notification_service.get_unread_count(db=db, user_id=user.id),
    )


@router.get("/unread-count")
async def get_unread_count(
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> UnreadCountResponse:
    """Get count of unread notifications for the current user."""
    notification_service = get_notification_service(request)

    count = await notification_service.get_unread_count(db=db, user_id=user.id)

    return UnreadCountResponse(count=count)


@router.put("/mark-read")
async def mark_notifications_as_read(
    request: Request,
    db: DbSession,
    request_body: dict,
    user: User = Depends(require_auth),
) -> None:
    """Mark multiple notifications as read.

    Body: {"notification_ids": [1, 2, 3]}
    """
    notification_service = get_notification_service(request)

    notification_ids = request_body.get("notification_ids", [])
    if not notification_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="notification_ids must be a non-empty list",
        )

    count = await notification_service.mark_as_read(
        db=db,
        user_id=user.id,
        notification_ids=notification_ids,
    )
    if count != len(set(notification_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Some notifications could not be marked as read",
        )
    await db.commit()


@router.put("/mark-all-read")
async def mark_all_notifications_as_read(
    request: Request,
    db: DbSession,
    user: User = Depends(require_auth),
) -> None:
    """Mark all notifications as read for the current user."""
    notification_service = get_notification_service(request)

    await notification_service.mark_all_as_read(db=db, user_id=user.id)
    await db.commit()
