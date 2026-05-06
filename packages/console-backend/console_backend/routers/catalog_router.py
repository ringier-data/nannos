"""Router for catalog management endpoints."""

import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from ..catalog.token_service import CatalogTokenService
from ..config import config
from ..db.session import DbSession
from ..dependencies import (
    is_admin_mode,
    require_auth,
    require_auth_or_bearer_token,
)
from ..models.catalog import (
    AddSourceRequest,
    Catalog,
    CatalogCreate,
    CatalogFile,
    CatalogFileListResponse,
    CatalogListResponse,
    CatalogPage,
    CatalogPageListResponse,
    CatalogPermission,
    CatalogPermissionUpdate,
    CatalogSource,
    CatalogSyncJob,
    CatalogUpdate,
    UpdateFileIndexing,
    UpdateSourceRequest,
)
from ..models.user import User
from ..services.catalog_service import CatalogService

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/api/v1/catalogs", tags=["catalogs"])

# Google OAuth scopes for catalog connections
_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
]
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def get_catalog_service(request: Request) -> CatalogService:
    """Get catalog service from app state."""
    return request.app.state.catalog_service


@router.get("", response_model=CatalogListResponse, operation_id="list_catalogs")
async def list_catalogs(
    request: Request,
    db: DbSession,
    user: User = Depends(
        require_auth_or_bearer_token
    ),  # called from agents with bearer token, so allow both auth methods
) -> CatalogListResponse:
    """List catalogs accessible to the current user."""
    service = get_catalog_service(request)
    catalogs = await service.get_accessible_catalogs(db, user, is_admin=is_admin_mode(request, user))
    return CatalogListResponse(items=catalogs, total=len(catalogs))


@router.post("", response_model=Catalog, status_code=201, operation_id="create_catalog")
async def create_catalog(
    request: Request,
    db: DbSession,
    data: CatalogCreate,
    user: User = Depends(require_auth),
) -> Catalog:
    """Create a new catalog."""
    service = get_catalog_service(request)
    catalog = await service.create_catalog(db, data, actor=user)
    await db.commit()
    return catalog


# --- Google OAuth Connect ---
# These static routes MUST be defined before /{catalog_id} to avoid
# "connect" and "shared-drives" being captured as a catalog_id parameter.


def _get_token_service(request: Request) -> CatalogTokenService:
    """Get catalog token service from app state."""
    return request.app.state.catalog_token_service


@router.get("/connect", operation_id="catalog_connect_google")
async def connect_google(
    request: Request,
    catalog_id: str = Query(..., description="Catalog ID to connect"),
    user: User = Depends(require_auth),
) -> RedirectResponse:
    """Initiate Google OAuth flow for connecting a catalog to Google Drive.

    Redirects the user to Google's consent screen.
    """
    catalog_config = config.catalog
    if not catalog_config.is_configured:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    # Store state in session for CSRF protection
    state = secrets.token_urlsafe(32)
    request.session["catalog_oauth_state"] = state
    request.session["catalog_oauth_catalog_id"] = catalog_id

    params = {
        "client_id": catalog_config.google_oauth_client_id,
        "redirect_uri": catalog_config.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(_GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_GOOGLE_AUTH_URL}?{query_string}"
    return RedirectResponse(url=url)


@router.get("/connect/callback", operation_id="catalog_connect_callback")
async def connect_callback(
    request: Request,
    db: DbSession,
    code: str = Query(...),
    state: str = Query(...),
    user: User = Depends(require_auth),
) -> RedirectResponse:
    """Handle Google OAuth callback, exchange code for tokens, store encrypted refresh token."""
    import httpx

    # Verify CSRF state
    expected_state = request.session.get("catalog_oauth_state")
    catalog_id = request.session.get("catalog_oauth_catalog_id")
    if not expected_state or state != expected_state or not catalog_id:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    # Clean up session
    request.session.pop("catalog_oauth_state", None)
    request.session.pop("catalog_oauth_catalog_id", None)

    catalog_config = config.catalog

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": catalog_config.google_oauth_client_id,
                "client_secret": catalog_config.google_oauth_client_secret.get_secret_value(),
                "redirect_uri": catalog_config.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code != 200:
            logger.error("Google token exchange failed: %s", resp.text)
            raise HTTPException(status_code=502, detail="Failed to exchange authorization code")
        token_data = resp.json()

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=502, detail="No refresh token received from Google")

    # Store encrypted connection
    token_service = _get_token_service(request)
    await token_service.store_connection(
        db=db,
        catalog_id=catalog_id,
        connector_user_id=user.id,
        refresh_token=refresh_token,
        scopes=_GOOGLE_SCOPES,
    )
    await db.commit()

    logger.info("Google Drive connected for catalog %s by user %s", catalog_id, user.id)

    # Redirect back to the frontend catalog detail page
    frontend_base = os.environ.get("FRONTEND_URL", "http://localhost:5173")
    return RedirectResponse(url=f"{frontend_base}/app/catalogs/{catalog_id}", status_code=302)


@router.get("/shared-drives", operation_id="list_shared_drives")
async def list_shared_drives(
    request: Request,
    db: DbSession,
    catalog_id: str = Query(..., description="Catalog ID with active connection"),
    user: User = Depends(require_auth),
) -> list[dict]:
    """List shared drives accessible via the catalog's connection."""
    from ..catalog.adapters.google_drive import GoogleDriveAdapter

    token_service = _get_token_service(request)
    credentials = await token_service.get_credentials(db, catalog_id)
    if not credentials:
        raise HTTPException(status_code=400, detail="No active Google connection for this catalog")

    adapter = GoogleDriveAdapter()
    return await adapter.list_shared_drives(credentials)


@router.get("/folders", operation_id="list_drive_folders")
async def list_drive_folders(
    request: Request,
    db: DbSession,
    catalog_id: str = Query(..., description="Catalog ID with active connection"),
    shared_drive_id: str | None = Query(None, description="Shared Drive ID (omit for user-shared folders)"),
    parent_id: str | None = Query(None, description="Parent folder ID (omit for root)"),
    user: User = Depends(require_auth),
) -> list[dict]:
    """List child folders within a Shared Drive or under a shared folder."""
    from ..catalog.adapters.google_drive import GoogleDriveAdapter

    if not shared_drive_id and not parent_id:
        raise HTTPException(status_code=400, detail="Either shared_drive_id or parent_id must be provided")

    token_service = _get_token_service(request)
    credentials = await token_service.get_credentials(db, catalog_id)
    if not credentials:
        raise HTTPException(status_code=400, detail="No active Google connection for this catalog")

    adapter = GoogleDriveAdapter()
    return await adapter.list_folders(credentials, shared_drive_id, parent_id)


@router.get("/shared-folders", operation_id="list_user_shared_folders")
async def list_user_shared_folders(
    request: Request,
    db: DbSession,
    catalog_id: str = Query(..., description="Catalog ID with active connection"),
    user: User = Depends(require_auth),
) -> list[dict]:
    """List folders shared with the user (from 'Shared with me' in Google Drive)."""
    from ..catalog.adapters.google_drive import GoogleDriveAdapter

    token_service = _get_token_service(request)
    credentials = await token_service.get_credentials(db, catalog_id)
    if not credentials:
        raise HTTPException(status_code=400, detail="No active Google connection for this catalog")

    adapter = GoogleDriveAdapter()
    return await adapter.list_user_shared_folders(credentials)


# --- CRUD (dynamic /{catalog_id} routes must come after static routes) ---


@router.get("/{catalog_id}", response_model=Catalog, operation_id="get_catalog")
async def get_catalog(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> Catalog:
    """Get a single catalog."""
    service = get_catalog_service(request)
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    return catalog


@router.patch("/{catalog_id}", response_model=Catalog, operation_id="update_catalog")
async def update_catalog(
    request: Request,
    db: DbSession,
    catalog_id: str,
    data: CatalogUpdate,
    user: User = Depends(require_auth),
) -> Catalog:
    """Update a catalog."""
    service = get_catalog_service(request)
    try:
        catalog = await service.update_catalog(db, catalog_id, data, actor=user, is_admin=is_admin_mode(request, user))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    await db.commit()
    return catalog


@router.delete("/{catalog_id}", status_code=204, operation_id="delete_catalog")
async def delete_catalog(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> None:
    """Delete a catalog."""
    service = get_catalog_service(request)
    try:
        deleted = await service.delete_catalog(db, catalog_id, actor=user, is_admin=is_admin_mode(request, user))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Catalog not found")
    await db.commit()


# --- Permissions ---


@router.get("/{catalog_id}/permissions", response_model=list[CatalogPermission], operation_id="get_catalog_permissions")
async def get_catalog_permissions(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> list[CatalogPermission]:
    """Get permissions for a catalog."""
    service = get_catalog_service(request)
    # Verify access
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    rows = await service.get_permissions(db, catalog_id)
    return [CatalogPermission(**r) for r in rows]


@router.put("/{catalog_id}/permissions", response_model=list[CatalogPermission], operation_id="set_catalog_permissions")
async def set_catalog_permissions(
    request: Request,
    db: DbSession,
    catalog_id: str,
    data: CatalogPermissionUpdate,
    user: User = Depends(require_auth),
) -> list[CatalogPermission]:
    """Set permissions for a catalog (replaces existing)."""
    service = get_catalog_service(request)
    try:
        rows = await service.set_permissions(
            db=db,
            catalog_id=catalog_id,
            permissions=data.permissions,
            actor=user,
            is_admin=is_admin_mode(request, user),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    await db.commit()
    return [CatalogPermission(**r) for r in rows]


# --- Files & Pages ---


@router.get("/{catalog_id}/files", response_model=CatalogFileListResponse, operation_id="list_catalog_files")
async def list_catalog_files(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, max_length=200),
    status: str | None = Query(None, pattern="^(pending|syncing|synced|failed|skipped)$"),
) -> CatalogFileListResponse:
    """List files in a catalog (paginated)."""
    service = get_catalog_service(request)
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    files, total = await service.get_catalog_files(db, catalog_id, limit, offset, search, status)
    return CatalogFileListResponse(items=files, total=total)


@router.get("/{catalog_id}/pages", response_model=CatalogPageListResponse, operation_id="list_catalog_pages")
async def list_catalog_pages(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> CatalogPageListResponse:
    """List pages across all files in a catalog (paginated)."""
    service = get_catalog_service(request)
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    pages, total = await service.get_catalog_pages(db, catalog_id, limit, offset)
    return CatalogPageListResponse(items=pages, total=total)


@router.get("/{catalog_id}/files/{file_id}/pages", response_model=list[CatalogPage], operation_id="list_file_pages")
async def list_file_pages(
    request: Request,
    db: DbSession,
    catalog_id: str,
    file_id: str,
    user: User = Depends(require_auth),
) -> list[CatalogPage]:
    """List pages for a specific file."""
    service = get_catalog_service(request)
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    return await service.get_file_pages(db, file_id)


@router.patch("/{catalog_id}/files/{file_id}/indexing", response_model=CatalogFile, operation_id="update_file_indexing")
async def update_file_indexing(
    request: Request,
    db: DbSession,
    catalog_id: str,
    file_id: str,
    body: UpdateFileIndexing,
    user: User = Depends(require_auth),
) -> CatalogFile:
    """Toggle indexing exclusion for a file. When excluded, removes pages from vector store."""
    service = get_catalog_service(request)
    try:
        return await service.update_file_indexing(
            db,
            catalog_id,
            file_id,
            body.indexing_excluded,
            actor=user,
            is_admin=is_admin_mode(request, user),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


# --- Sources ---


@router.get("/{catalog_id}/sources", response_model=list[CatalogSource], operation_id="list_catalog_sources")
async def list_catalog_sources(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> list[CatalogSource]:
    """List configured sources for a catalog."""
    service = get_catalog_service(request)
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    return await service.get_sources(db, catalog_id)


@router.post("/{catalog_id}/sources", response_model=CatalogSource, status_code=201, operation_id="add_catalog_source")
async def add_catalog_source(
    request: Request,
    db: DbSession,
    catalog_id: str,
    body: AddSourceRequest,
    user: User = Depends(require_auth),
) -> CatalogSource:
    """Add a new source (shared drive, drive folder, or shared folder) to a catalog."""
    service = get_catalog_service(request)
    try:
        return await service.add_source(
            db,
            catalog_id,
            body,
            actor=user,
            is_admin=is_admin_mode(request, user),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete("/{catalog_id}/sources/{source_id}", status_code=204, operation_id="remove_catalog_source")
async def remove_catalog_source(
    request: Request,
    db: DbSession,
    catalog_id: str,
    source_id: str,
    user: User = Depends(require_auth),
) -> None:
    """Remove a source from a catalog."""
    service = get_catalog_service(request)
    try:
        await service.remove_source(
            db,
            catalog_id,
            source_id,
            actor=user,
            is_admin=is_admin_mode(request, user),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.patch("/{catalog_id}/sources/{source_id}", response_model=CatalogSource, operation_id="update_catalog_source")
async def update_catalog_source(
    request: Request,
    db: DbSession,
    catalog_id: str,
    source_id: str,
    body: UpdateSourceRequest,
    user: User = Depends(require_auth),
) -> CatalogSource:
    """Update a source's settings (e.g. folder exclusion patterns) and trigger a sync."""
    service = get_catalog_service(request)
    try:
        source = await service.update_source(
            db,
            catalog_id,
            source_id,
            body,
            actor=user,
            is_admin=is_admin_mode(request, user),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # Auto-trigger a sync so that exclusion changes take effect immediately.
    try:
        await service.trigger_sync(db, catalog_id, actor=user, is_admin=is_admin_mode(request, user))
        await db.commit()
    except ValueError:
        # A sync is already in progress or no sources — skip silently.
        pass

    return source


# --- Sync ---


@router.post("/{catalog_id}/reindex", response_model=CatalogSyncJob, status_code=202, operation_id="reindex_catalog")
async def reindex_catalog(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> CatalogSyncJob:
    """Trigger re-indexing of pages with indexed_at = NULL. Runs in background."""
    service = get_catalog_service(request)

    try:
        job = await service.reindex_catalog(
            db,
            catalog_id,
            actor=user,
            is_admin=is_admin_mode(request, user),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return job


@router.post("/{catalog_id}/sync", response_model=CatalogSyncJob, status_code=202, operation_id="trigger_catalog_sync")
async def trigger_catalog_sync(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> CatalogSyncJob:
    """Trigger a sync for a catalog."""
    service = get_catalog_service(request)
    try:
        job = await service.trigger_sync(db, catalog_id, actor=user, is_admin=is_admin_mode(request, user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    await db.commit()
    return job


@router.get("/{catalog_id}/sync/status", response_model=CatalogSyncJob | None, operation_id="get_catalog_sync_status")
async def get_catalog_sync_status(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> CatalogSyncJob | None:
    """Get the latest sync status for a catalog."""
    service = get_catalog_service(request)
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")
    return await service.get_sync_status(db, catalog_id)


@router.post("/{catalog_id}/sync/pause", response_model=CatalogSyncJob, operation_id="pause_catalog_sync")
async def pause_catalog_sync(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> CatalogSyncJob:
    """Pause a running sync. The pipeline stops after the current file completes."""
    service = get_catalog_service(request)
    try:
        return await service.pause_sync(db, catalog_id, actor=user, is_admin=is_admin_mode(request, user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/{catalog_id}/sync/resume", response_model=CatalogSyncJob, operation_id="resume_catalog_sync")
async def resume_catalog_sync(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> CatalogSyncJob:
    """Resume a paused sync."""
    service = get_catalog_service(request)
    try:
        return await service.resume_sync(db, catalog_id, actor=user, is_admin=is_admin_mode(request, user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post("/{catalog_id}/sync/cancel", response_model=CatalogSyncJob, operation_id="cancel_catalog_sync")
async def cancel_catalog_sync(
    request: Request,
    db: DbSession,
    catalog_id: str,
    user: User = Depends(require_auth),
) -> CatalogSyncJob:
    """Cancel a running or paused sync."""
    service = get_catalog_service(request)
    try:
        return await service.cancel_sync(db, catalog_id, actor=user, is_admin=is_admin_mode(request, user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/sync/queue/diagnostics", operation_id="get_sync_queue_diagnostics")
async def get_sync_queue_diagnostics(
    request: Request,
    user: User = Depends(require_auth),
) -> dict:
    """Return task queue diagnostics (admin-only debug endpoint)."""
    queue = getattr(request.app.state, "sync_task_queue", None)
    if queue is None:
        return {"error": "Task queue not configured"}
    return queue.diagnostics()


# --- Thumbnails ---


@router.get("/{catalog_id}/pages/{page_id}/thumbnail", operation_id="get_page_thumbnail")
async def get_page_thumbnail(
    request: Request,
    db: DbSession,
    catalog_id: str,
    page_id: str,
    user: User = Depends(require_auth),
):
    """Stream a page thumbnail image from S3 with browser caching."""
    import aiobotocore.session

    service = get_catalog_service(request)
    catalog = await service.get_catalog(db, catalog_id, user, is_admin=is_admin_mode(request, user))
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found")

    from sqlalchemy import text as sql_text

    result = await db.execute(
        sql_text("SELECT thumbnail_s3_key FROM catalog_pages WHERE id = :page_id AND catalog_id = :catalog_id"),
        {"page_id": page_id, "catalog_id": catalog_id},
    )
    row = result.mappings().first()
    if not row or not row["thumbnail_s3_key"]:
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    bucket = config.catalog.thumbnails_s3_bucket
    key = row["thumbnail_s3_key"]
    region = os.environ.get("AWS_REGION", "eu-central-1")

    session = aiobotocore.session.get_session()
    async with session.create_client("s3", region_name=region) as s3:
        resp = await s3.get_object(Bucket=bucket, Key=key)
        content_type = resp.get("ContentType", "image/png")
        body = await resp["Body"].read()

    return StreamingResponse(
        iter([body]),
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "Content-Length": str(len(body)),
        },
    )
