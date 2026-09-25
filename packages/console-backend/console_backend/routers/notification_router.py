"""API routes for user notifications."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

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
    # Bounded rather than clamped, like the bug report list: `page=0` reached the
    # service as a negative OFFSET and answered 500.
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    unread_only: bool = False,
    search: str | None = Query(None, description="Match against notification title and message"),
    user: User = Depends(require_auth),
) -> NotificationListResponse:
    """Get notifications for the current user with pagination.

    `total` counts the notifications matching the filters; `unread_count` is the
    user's overall unread count, independent of `search` and `unread_only`, so the
    inbox badge does not change while the user types.
    """
    notification_service = get_notification_service(request)

    notifications, total = await notification_service.get_user_notifications(
        db=db,
        user_id=user.id,
        page=page,
        limit=limit,
        unread_only=unread_only,
        search=search,
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
